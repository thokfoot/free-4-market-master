"""
FREE 3-Market v5.10 — LIVE P&L UPDATER
=======================================
Runs every 5 min during market hours (via GitHub Actions).
Reads open positions, fetches live 1m data, checks SL/TP intraday,
updates realized P&L on exit, sends Telegram alerts.

Purpose: Real-time P&L + instant SL/TP exit, NOT new entries.
Entries are handled by bot.py (runs 3x/day after market close).

Designed to work alongside bot.py without conflicts.
"""

import os, sys, json, math, time, traceback
from datetime import datetime, timedelta
import pandas as pd
import requests
import pytz

from config import (
    CAPITAL, CAPITAL_BY_MARKET, TOTAL_CAPITAL,
    CHARGES_PER_MARKET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    MAX_HOLD_DAYS, INTRADAY_MAX_HOLD_HOURS,
    INTRADAY_CAPITAL,
    SLIPPAGE_PCT, INTRADAY_SLIPPAGE_PCT,
    CIRCUIT_BREAKER_ENABLED, CIRCUIT_BREAKER_MAX_CONSEC_LOSSES,
    CIRCUIT_BREAKER_COOLDOWN_DAYS,
    market_active_for_mode, tg_safe,
)
from paper_trader import initialize_system, rebuild_portfolio_from_csv, _session_live_until, _session_minutes_until
from logger import log_error
from strategy_report import generate_strategy_report
from integrity_check import validate_all

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

PAPER_FILE = os.path.join(LOG_DIR, "paper_trades.csv")
PORTFOLIO_FILE = os.path.join(LOG_DIR, "portfolio.json")
STRATEGY_STATS_FILE = os.path.join(LOG_DIR, "strategy_stats.json")
AUDIT_FILE = os.path.join(LOG_DIR, "trade_audit.json")
LIVE_PNL_LOG = os.path.join(LOG_DIR, "live_pnl_snapshots.csv")

# ── Persistent state (survives across GitHub Actions runs) ──
LIVE_STATE_FILE = os.path.join(LOG_DIR, "live_pnl_state.json")
TG_COOLDOWN = timedelta(minutes=25)  # Don't send update msg more than once per 25 min
PNL_CHANGE_THRESHOLD_PCT = 0.5  # Send update if unrealized P&L % changes by >0.5% (absolute)


import market_data
def _load_live_state() -> dict:
    """Load persistent state: last TG send time + last P&L per ticker."""
    if os.path.exists(LIVE_STATE_FILE):
        try:
            with open(LIVE_STATE_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {"last_tg": {}, "last_pnl": {}}  # {ticker: iso_timestamp}, {ticker: pnl_rupees}


def _save_live_state(state: dict):
    """Save persistent state."""
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(LIVE_STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


# ── Helpers (standalone copies so no import cycles) ─────────────
def _price_slip_multiplier(price: float, mode: str = "INDIAN") -> float:
    """Price/volatility-adaptive slippage multiplier (v5.22) — mirrors
    paper_trader._price_slip_multiplier. Only INDIAN small-caps and low-priced
    altcoins trade on wide relative spreads; US large-caps/ETFs stay at 1.0x."""
    if mode == "US" or price <= 0:
        return 1.0
    if price < 20:
        return 3.0
    if price < 100:
        return 2.0
    if price < 500:
        return 1.5
    return 1.0


def _apply_slippage(price: float, direction: str, action: str, mode: str, tf: str) -> float:
    """
    Apply realistic fill slippage to a price (mirrors paper_trader._apply_slippage).

    Slippage always makes the fill WORSE than the signal price:
    - LONG entry: buy slightly higher (pay more)
    - SHORT entry: sell slightly lower (receive less)
    - LONG exit (SL/TP hit): sell slightly lower (receive less)
    - SHORT exit (SL/TP hit): buy slightly higher (pay more)

    Args:
        price: The ideal signal price (entry/exit)
        direction: "LONG" or "SHORT"
        action: "ENTRY" or "EXIT"
        mode: "INDIAN" | "US" | "CRYPTO"
        tf: "SWING_1d" or "INTRADAY_1h"

    Returns:
        Slipped price (always worse for the trader).
        When slippage is 0 (default), returns price unchanged.
    """
    # GAP_DOWN_1m / FADE_1h are intraday — use intraday slippage rates
    is_intraday = (tf in ("INTRADAY_1h", "FADE_1h", "US_FADE_5m", "GAP_DOWN_1m"))
    slip_pct = (
        INTRADAY_SLIPPAGE_PCT.get(mode, 0.0)
        if is_intraday else
        SLIPPAGE_PCT.get(mode, 0.0)
    )
    # Price-adaptive: cheap thin small-caps pay wider relative spread (v5.22)
    slip_pct *= _price_slip_multiplier(price, mode)
    if slip_pct <= 0:
        return price

    if action == "ENTRY":
        # Entry: pay more (LONG) or receive less (SHORT)
        if direction == "LONG":
            return price * (1 + slip_pct)
        else:
            return price * (1 - slip_pct)
    else:  # EXIT
        # Exit: receive less (LONG sells) or pay more (SHORT covers)
        if direction == "LONG":
            return price * (1 - slip_pct)
        else:
            return price * (1 + slip_pct)


def _safe_float(val) -> float:
    if val is None:
        return 0.0
    try:
        v = float(val)
        return 0.0 if v != v else v
    except (ValueError, TypeError):
        return 0.0


def round_price(price):
    p = float(price)
    if p >= 1000:
        return round(p, 2)
    if p >= 100:
        return round(p, 2)
    if p >= 1:
        return round(p, 2)
    if p >= 0.1:
        return round(p, 4)
    if p >= 0.01:
        return round(p, 6)
    return round(p, 8)


def _load_audit() -> list:
    if os.path.exists(AUDIT_FILE):
        try:
            with open(AUDIT_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []


def _save_audit(audit: list):
    with open(AUDIT_FILE, "w") as f:
        json.dump(audit, f, indent=2)


def _format_hold(seconds) -> str:
    """Format a duration in seconds as a compact hold string (e.g. '1d 2h 3m')."""
    try:
        secs = max(0, int(float(seconds)))
    except (TypeError, ValueError):
        return ""
    days, rem = divmod(secs, 86400)
    hours, rem = divmod(rem, 3600)
    mins, secs = divmod(rem, 60)
    if days > 0:
        return f"{days}d {hours}h {mins}m"
    if hours > 0:
        return f"{hours}h {mins}m"
    if mins > 0:
        return f"{mins}m {secs}s"
    return f"{secs}s"


def _log_audit_exit(trade_row: dict):
    audit = _load_audit()
    exit_time = str(trade_row.get("Exit_Time", "")).strip()
    # Ensure full datetime (%Y-%m-%d %H:%M:%S IST) — time-only entries get today's date
    if exit_time and "-" not in exit_time:
        exit_time = f"{datetime.now(IST).strftime('%Y-%m-%d')} {exit_time}"
    audit.append({
        "event": "EXIT (Live)",
        "datetime": exit_time,
        "entry_datetime": trade_row.get("Entry_Time", ""),
        "mode": trade_row.get("Mode", ""),
        "ticker": trade_row.get("Ticker", ""),
        "direction": trade_row.get("Direction", ""),
        "entry_price": trade_row.get("Entry_Price", ""),
        "exit_price": trade_row.get("Exit_Price", ""),
        "qty": trade_row.get("Qty", 0),
        "pnl": trade_row.get("P&L", ""),
        "pnl_pct": trade_row.get("P&L_%", ""),
        "hold": trade_row.get("Hold", ""),
        "pattern_rank": trade_row.get("Pattern_Rank", ""),
        "expected_win_rate": trade_row.get("Expected_WinRate", ""),
        "pattern_factors": trade_row.get("Pattern_Factors", ""),
        "reason": trade_row.get("Reason", ""),
    })
    _save_audit(audit)
    print(f"[Audit] EXIT (Live): {trade_row.get('Direction','?')} {trade_row.get('Ticker','?')} P&L={trade_row.get('P&L','?')} Expected WR={trade_row.get('Expected_WinRate','?')}%")


def _extract_rank(reason: str) -> int:
    if not reason:
        return 0
    import re
    match = re.match(r"#?(\d+)", reason.strip())
    return int(match.group(1)) if match else 0


def _load_strategy_stats() -> dict:
    if os.path.exists(STRATEGY_STATS_FILE):
        try:
            with open(STRATEGY_STATS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def _save_strategy_stats(stats: dict):
    """Save per-strategy tracking atomically (temp file + rename).

    bot.py and live_pnl_updater.py run as separate near-concurrent workflows
    and both write this shared file (which now also holds circuit-breaker
    pause state), so a plain write could corrupt it. Mirrors
    paper_trader._atomic_write_json.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    tmp = STRATEGY_STATS_FILE + ".tmp." + str(os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(stats, f, indent=2)
        os.replace(tmp, STRATEGY_STATS_FILE)
        print(f"[Atomic] Written {os.path.basename(STRATEGY_STATS_FILE)}")
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except Exception:
                pass
        print(f"[Atomic] WRITE FAILED for {os.path.basename(STRATEGY_STATS_FILE)}: {e}")
        raise


def update_strategy_stats(reason: str, pnl: float):
    """
    Update win/loss tracking for the pattern that generated this trade.
    Called when a trade is closed.

    Mirrors paper_trader.update_strategy_stats (engine parity): maintains
    the circuit-breaker state per strategy - WIN resets the losing streak
    and lifts a pause; LOSS increments it and auto-pauses the strategy at
    CIRCUIT_BREAKER_MAX_CONSEC_LOSSES.
    """
    rank = _extract_rank(reason)
    if rank == 0:
        return

    stats = _load_strategy_stats()
    key = str(rank)

    if key not in stats:
        stats[key] = {
            "rank": rank,
            "factors": reason[:80],
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
            "consec_losses": 0,
            "paused_since": None,
        }
    else:
        stats[key].setdefault("consec_losses", 0)
        stats[key].setdefault("paused_since", None)

    stats[key]["total_pnl"] += pnl
    if pnl > 0:
        stats[key]["wins"] += 1
        stats[key]["consec_losses"] = 0
        if stats[key].get("paused_since"):
            stats[key]["paused_since"] = None
            print(f"[CircuitBreaker] Rank #{rank} resumed on WIN")
    elif pnl < 0:
        stats[key]["losses"] += 1
        stats[key]["consec_losses"] = stats[key].get("consec_losses", 0) + 1
        if (CIRCUIT_BREAKER_ENABLED
                and stats[key]["consec_losses"] >= CIRCUIT_BREAKER_MAX_CONSEC_LOSSES
                and not stats[key].get("paused_since")):
            stats[key]["paused_since"] = datetime.now(IST).strftime("%Y-%m-%d")
            print(f"[CircuitBreaker] Rank #{rank} PAUSED after "
                  f"{stats[key]['consec_losses']} consecutive losses")

    # Update the reason/factors in case it was truncated
    if len(reason) > len(stats[key]["factors"]):
        stats[key]["factors"] = reason[:80]

    _save_strategy_stats(stats)
    total = stats[key]["wins"] + stats[key]["losses"]
    wr = round(stats[key]["wins"] / total * 100, 1) if total > 0 else 0
    print(f"[Strategy] Rank #{rank} updated: {stats[key]['wins']}W/{stats[key]['losses']}L ({wr}%) PnL Rs {pnl:+.0f}")


def load_portfolio() -> dict:
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "capital_by_market": {"INDIAN": 100000, "US": 100000, "CRYPTO": 100000, "INTRADAY": 100000, "FADE": 100000},
        "open_positions": [],
        "closed_count": 0, "total_wins": 0, "total_losses": 0, "total_pnl": 0,
        "total_pnl_by_market": {"INDIAN": 0, "US": 0, "CRYPTO": 0, "INTRADAY": 0, "FADE": 0},
    }


def save_portfolio(port: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
    # Recalculate total_capital from actual capital_by_market (mirrors paper_trader.py fix)
    # Prevents stale total_capital after live exits update capital_by_market.
    port["total_capital"] = sum(port.get("capital_by_market", {}).values())
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(port, f, indent=2)


# ── Telegram ──────────────────────────────────────────────────
def send_telegram(msg: str) -> str:
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return "NoToken"
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    for attempt in range(3):
        try:
            r = requests.post(url, data=data, timeout=15)
            resp = r.json() if r.text else {}
            if r.status_code == 200 and resp.get("ok"):
                print(f"[TG] Sent OK ({len(msg)} chars)")
                return "Sent"
            else:
                print(f"[TG] Attempt {attempt+1} failed: {resp.get('description', r.text[:200])}")
                time.sleep(2)
        except Exception as e:
            print(f"[TG] Attempt {attempt+1} exception: {e}")
            time.sleep(2)
    return "Failed"


# ── Core: fetch live 1m data ─────────────────────────────────
def fetch_live_ohlc(ticker: str, entry_dt=None) -> dict:
    """
    Fetch recent OHLC data using 1m interval (5d window so the PREVIOUS
    session close is available for the split/adjustment guard).
    If entry_dt (IST-aware datetime) is provided, only bars at/after the entry
    time are considered, so pre-entry price action (e.g., a prior session's low
    while the market is still closed) can never trigger a false SL/TP exit.
    Returns {close, high, low, date, prev_close, has_post_entry} or None.
    """
    for attempt in range(3):
        try:
            df = market_data.download(ticker, interval="1m", period="5d")
            if df is not None and len(df) > 0:
                # Handle multi-index columns
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)

                latest_date = str(df.index[-1].date())

                # Previous session close (last close strictly BEFORE the
                # latest bar's date) — reference for split_suspected().
                prev_close = None
                try:
                    older = df[df.index.date < df.index[-1].date()]
                    if len(older) > 0 and pd.notna(older["Close"].iloc[-1]):
                        prev_close = float(older["Close"].iloc[-1])
                except Exception:
                    prev_close = None

                # Filter to bars at/after entry time (prevents false pre-entry exits)
                has_post_entry = True
                if entry_dt is not None:
                    try:
                        entry_utc = entry_dt.astimezone(pytz.utc)
                        idx_utc = df.index
                        if idx_utc.tz is None:
                            idx_utc = idx_utc.tz_localize("UTC")
                        else:
                            idx_utc = idx_utc.tz_convert("UTC")
                        post = df[idx_utc >= entry_utc]
                        if len(post) > 0:
                            df = post
                        else:
                            has_post_entry = False
                    except Exception as e:
                        print(f"[Live] {ticker}: entry-time filter error: {e}")

                last = df.iloc[-1]
                current_close = float(last["Close"])
                daily_high = float(df["High"].max())
                daily_low = float(df["Low"].min())

                return {
                    "close": current_close,
                    "high": daily_high,
                    "low": daily_low,
                    "date": latest_date,
                    "prev_close": prev_close,
                    "has_post_entry": has_post_entry,
                }
            print(f"[Live] {ticker}: No 1m data ({len(df) if df is not None else 0} rows)")
            if attempt < 2:
                time.sleep(1)
        except Exception as e:
            print(f"[Live] {ticker} 1m fetch error: {e}")
            if attempt < 2:
                time.sleep(2)
    return None


# ── Core: process open trades ────────────────────────────────
def process_open_trades() -> tuple:
    """
    Iterate all open positions, fetch live data, check SL/TP.
    
    Returns:
        (closed_msgs, update_msgs)
        closed_msgs: list of exit messages (for Telegram)
        update_msgs: list of unrealized P&L update messages (for Telegram, throttled)
    """
    portfolio = load_portfolio()
    open_positions = portfolio.get("open_positions", [])
    
    if not open_positions:
        print("[Live] No open positions to check")
        return [], []
    
    # Read paper_trades.csv for full trade data
    if not os.path.exists(PAPER_FILE):
        print("[Live] paper_trades.csv not found")
        return [], []
    
    df = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
    
    # ── Ensure string columns are object dtype (same fix as paper_trader.py) ──
    str_cols_pnl = ["Exit_Price", "Exit_Time", "P&L", "P&L_%", "Status", "Reason", "Date", "Time_IST", "Mode", "Ticker", "Direction", "TimeFrame", "Pattern_Rank", "Expected_WinRate", "Pattern_Factors"]
    for col in str_cols_pnl:
        if col in df.columns:
            df[col] = df[col].astype(object)
    
    closed_msgs = []
    update_msgs = []
    skipped_closed_mkt = 0
    now = datetime.now(IST)
    exit_dt_str = now.strftime("%Y-%m-%d %H:%M:%S IST")
    portfolio_updated = False
    
    for idx, row in df.iterrows():
        if row["Status"] != "OPEN":
            continue
        
        ticker = row["Ticker"]
        direction = str(row.get("Direction", "LONG"))
        entry = _safe_float(row["Entry_Price"])
        sl = _safe_float(row["SL"])
        target = _safe_float(row["Target"])
        qty = int(_safe_float(row["Qty"]))
        
        if entry <= 0 or qty <= 0:
            continue

        # ── PER-MARKET PROCESSING GATE ────────────────────────────────
        # Only touch a position while its market can actually trade.
        # Fixes night-time Indian alerts/exits caused by the 24/7 crypto
        # cron + keep-alive dispatch chain: an NSE fill at 2 AM IST is a
        # stale-price fiction. Exits for INDIAN/US positions now defer to
        # their next session window (config.market_active_for_mode);
        # crypto stays 24/7.
        _gate_mode = str(row.get("Mode", "")).upper()
        if _gate_mode == "INDIA":
            _gate_mode = "INDIAN"
        _gate_active, _gate_why = market_active_for_mode(_gate_mode, now)
        if not _gate_active:
            print(f"[Live] {direction} {ticker}: skipped — {_gate_why}")
            skipped_closed_mkt += 1
            continue
        
        # Parse entry datetime FIRST (needed to filter post-entry bars so a prior
        # session's low can't falsely stop out a position entered at the prior
        # session's close while the market is still closed)
        entry_dt = None
        try:
            entry_dt_str = f"{row.get('Date', '')} {str(row.get('Time_IST', ''))[:8]}"
            entry_dt = IST.localize(datetime.strptime(entry_dt_str.strip(), "%Y-%m-%d %H:%M:%S"))
        except Exception as e:
            print(f"[Live] Entry time parse error {ticker}: {e}")
        
        # Fetch live 1m data (only bars at/after entry considered for SL/TP)
        ohlc = fetch_live_ohlc(ticker, entry_dt)
        if not ohlc:
            continue
        
        cmp = ohlc["close"]
        daily_high = ohlc["high"]
        daily_low = ohlc["low"]
        has_post_entry = ohlc.get("has_post_entry", True)
        
        # ── OHLC DATA VALIDATION: Prevent false exits from corrupt data ──
        _invalid_ohlc = (
            daily_low is None or daily_high is None or cmp is None
            or not math.isfinite(daily_low)
            or not math.isfinite(daily_high)
            or not math.isfinite(cmp)
            or daily_low <= 0 or daily_high <= 0 or cmp <= 0
        )
        if _invalid_ohlc:
            print(f"[Live] WARNING: Invalid OHLC data for {ticker}: "
                  f"close={cmp}, high={daily_high}, low={daily_low} — SKIPPING SL/TP check")
            continue  # Skip this ticker — don't exit based on bad data

        # Log OHLC values for debugging
        print(f"[Live] SL/TP check {direction} {ticker}: "
              f"close={cmp:.2f} high={daily_high:.2f} low={daily_low:.2f} | "
              f"entry={entry:.2f} sl={sl:.2f} target={target:.2f}")

        # ── MaxHold expiry check FIRST (time-based exit, independent of SL/TP) ──
        # Mirrors paper_trader.update_trades so expired positions are force-closed
        # promptly even between scheduled bot scans (which run only a few times/day).
        _TOLERANCE = 0.9999
        exit_price = None
        exit_reason = None
        is_expired = False
        try:
            trade_tf_live = str(row.get("TimeFrame", "SWING_1d"))
            mh_live = row.get("MaxHold")
            if pd.notna(mh_live):
                hold_limit = int(mh_live)
            elif trade_tf_live == "INTRADAY_1h":
                hold_limit = INTRADAY_MAX_HOLD_HOURS.get(str(row.get("Mode", "")).upper(), 6)
            else:
                hold_limit = MAX_HOLD_DAYS
            # ── INDIA SAME-DAY SQUARE OFF (15:30 IST market close) ──
            # Parity with paper_trader.update_trades: Indian intraday positions
            # square off at market close, not a wall-clock MaxHold past close.
            _mode_live = str(row.get("Mode", "")).upper()
            _is_india_intraday = (
                _mode_live == "INDIAN"
                and trade_tf_live in ("GAP_DOWN_1m", "INTRADAY_1h", "FADE_1h", "LONG_BOUNCE_5m", "US_FADE_5m")
            )
            if _is_india_intraday and now.time() >= datetime.strptime("15:30", "%H:%M").time():
                exit_price = cmp
                exit_reason = "Expiry Square Off 15:30 IST"
                is_expired = True
            elif trade_tf_live == "GAP_DOWN_1m":
                mins_held = (now - entry_dt).total_seconds() / 60
                if mins_held >= hold_limit:
                    exit_price = cmp
                    exit_reason = f"Expiry {int(mins_held)}m"
                    is_expired = True
            elif trade_tf_live in ("INTRADAY_1h", "FADE_1h", "US_FADE_5m", "LONG_BOUNCE_5m"):
                if str(row.get("Mode", "")).upper() == "US":
                    # Session-time MaxHold (parity with paper_trader): only US
                    # session minutes (13:30-20:00 UTC weekdays) consume the
                    # budget; overnight/weekend gaps don't expire the position.
                    entry_utc = entry_dt.astimezone(pytz.utc)
                    now_utc = now.astimezone(pytz.utc)
                    if now_utc >= _session_live_until(entry_utc, hold_limit):
                        exit_price = cmp
                        sess_h = round(_session_minutes_until(entry_utc, now_utc) / 60.0, 1)
                        exit_reason = f"Expiry {int(sess_h)}h"
                        is_expired = True
                else:
                    hrs_held = (now - entry_dt).total_seconds() / 3600
                    if hrs_held >= hold_limit:
                        exit_price = cmp
                        exit_reason = f"Expiry {int(hrs_held)}h"
                        is_expired = True
            else:
                days_held = (now - entry_dt).days
                if days_held >= hold_limit:
                    exit_price = cmp
                    exit_reason = f"Expiry {days_held}d"
                    is_expired = True
        except Exception as e:
            print(f"[Live] MaxHold check error {ticker}: {e}")

        # ── SL/TP Check (intraday High/Low priority, with tolerance guard) ──
        # Use 0.01% tolerance to prevent 1-cent data noise from triggering exit
        # Only SL/TP-check when the market has actually traded since entry
        # (has_post_entry) — pre-entry lows must never stop out a position.
        if not is_expired and has_post_entry:
            _prev_close = ohlc.get("prev_close")
            def _split_blocks(direction_, trigger_):
                """True when an SL 'hit' is actually a split/adjustment artifact."""
                from paper_trader import split_suspected
                blocked = bool(_prev_close and split_suspected(direction_, trigger_, _prev_close))
                if blocked:
                    print(f"[Live] SPLIT GUARD: {direction_} {ticker} SL "
                          f"trigger {trigger_} vs prev close {_prev_close} — "
                          f"phantom SL skipped, position held for review")
                return blocked
            if direction == "LONG":
                if daily_low <= sl * _TOLERANCE:
                    if not _split_blocks("LONG", sl):
                        exit_price = sl
                        exit_reason = "🎯 SL Hit (live)"
                elif daily_high >= target / _TOLERANCE:
                    exit_price = target
                    exit_reason = "🎯 Target Hit (live)"
            else:  # SHORT
                if daily_high >= sl / _TOLERANCE:
                    if not _split_blocks("SHORT", sl):
                        exit_price = sl
                        exit_reason = "🎯 SL Hit (live)"
                elif daily_low <= target * _TOLERANCE:
                    exit_price = target
                    exit_reason = "🎯 Target Hit (live)"
        
        if exit_price:
            # ── CLOSE THE TRADE ──
            # Apply exit slippage for realistic fills (worse than trigger price)
            # Mirrors paper_trader.update_trades — keeps both engines in parity.
            trade_mode_exit = str(row.get("Mode", "US"))
            trade_tf_exit = str(row.get("TimeFrame", "SWING_1d"))
            exit_price = _apply_slippage(exit_price, direction, "EXIT", trade_mode_exit, trade_tf_exit)

            # Gross P&L (entry already includes entry slippage from paper_trader)
            if direction == "LONG":
                pnl = (exit_price - entry) * qty
                pnl_pct = ((exit_price - entry) / entry) * 100
            else:
                pnl = (entry - exit_price) * qty
                pnl_pct = ((entry - exit_price) / entry) * 100
            
            # Charges deduction
            trade_mode = str(row.get("Mode", "US"))
            mode_norm = trade_mode.upper()
            if mode_norm == "INDIA":
                mode_norm = "INDIAN"
            charge_rate = CHARGES_PER_MARKET.get(mode_norm, 0.001)
            charges = round(entry * qty * charge_rate, 2)
            pnl -= charges
            pnl_pct -= charge_rate * 100

            # ── NaN/Inf Guard: prevent NaN/Inf P&L from corrupting CSV ──
            if not math.isfinite(pnl):
                print(f"[Live] CRITICAL: NaN/Inf P&L for {ticker}! pnl={pnl}, entry={entry}, exit={exit_price}, qty={qty}")
                pnl = 0.0
                pnl_pct = 0.0

            # Update CSV
            df.at[idx, "Exit_Price"] = round_price(exit_price)
            df.at[idx, "Exit_Time"] = exit_dt_str
            df.at[idx, "P&L"] = round(pnl, 2)
            df.at[idx, "P&L_%"] = round(pnl_pct, 2)
            df.at[idx, "Status"] = "CLOSED"
            df.at[idx, "Reason"] = str(row["Reason"]) + f" | {exit_reason}"
            
            # ── CRITICAL: This EXIT is also observed by bot.py's update_trades()
            # on its next scheduled run. To prevent the two workflows from
            # double-counting or clobbering portfolio.json, we do NOT mutate
            # portfolio here — the portfolio is rebuilt from the CSV below
            # (single source of truth, same as paper_trader.update_trades).
            
            # Hold duration (wall-clock time between entry and exit)
            _entry_dt_full = IST.localize(datetime.strptime(str(row.get("Date", "")), "%Y-%m-%d"))
            try:
                _et = str(row.get("Time_IST", ""))[:8]
                if len(_et) == 8:
                    _entry_dt_full = IST.localize(datetime.strptime(f"{row.get('Date', '')} {_et}", "%Y-%m-%d %H:%M:%S"))
            except Exception:
                pass
            _hold_secs = max(0, (now - _entry_dt_full).total_seconds())
            _hold_str = _format_hold(_hold_secs)
            
            # Audit log
            _log_audit_exit({
                "Exit_Time": exit_dt_str,
                "Entry_Time": f"{row.get('Date', '')} {row.get('Time_IST', '')}",
                "Hold": _hold_str,
                "Mode": row.get("Mode", ""),
                "Ticker": ticker, "Direction": direction,
                "Entry_Price": entry, "Exit_Price": round_price(exit_price),
                "Qty": qty, "P&L": round(pnl, 2), "P&L_%": round(pnl_pct, 2),
                "Pattern_Rank": row.get("Pattern_Rank", ""),
                "Expected_WinRate": row.get("Expected_WinRate", ""),
                "Pattern_Factors": row.get("Pattern_Factors", ""),
                "Reason": f"{row.get('Reason','')} | {exit_reason}",
            })
            
            # Strategy stats (pass full reason with exit context, rounded PnL)
            full_reason = str(row["Reason"]) + f" | {exit_reason}"
            update_strategy_stats(full_reason, round(pnl, 2))
            
            # ── Detailed exit alert (mirrors paper_trader._send_sl_tp_alert) ──
            # Includes OHLC telemetry so LIVE EXIT messages show the exact data
            # that triggered the close (SL/TP/MaxHold).
            _rank_live = row.get("Pattern_Rank", "")
            _rank_tag = f" #{_rank_live}" if pd.notna(_rank_live) and str(_rank_live) else ""
            if "SL" in exit_reason:
                _icon, _label = "\U0001F6A8", "SL HIT"
            elif "Target" in exit_reason:
                _icon, _label = "\U0001F3AF", "TP HIT"
            elif "Expiry" in exit_reason:
                _icon, _label = "\u23F0", "EXPIRY"
            else:
                _icon, _label = "\U0001F4CA", "EXIT"
            _dir_arrow = "\U0001F7E2" if direction == "LONG" else "\U0001F534"
            _pnl_icon = "\U0001F7E2" if pnl > 0 else ("\U0001F534" if pnl < 0 else "\u26AA")
            closed_msgs.append(
                f"{_icon} *{_label}:* {_dir_arrow} `{ticker}`{_rank_tag} {direction}\n"
                f"\u2523 Entry: {round_price(entry)} | Exit: {round_price(exit_price)}\n"
                f"\u2523 SL: {round_price(sl)} | TP: {round_price(target)}\n"
                f"\u2523 Qty: {qty} | {_pnl_icon} P&L: Rs {pnl:+,.0f} ({pnl_pct:+.2f}%)\n"
                f"\u2523 *OHLC:* Close={cmp:.2f} High={daily_high:.2f} Low={daily_low:.2f}\n"
                f"\u2517 Reason: {tg_safe(exit_reason)}"
            )
            portfolio_updated = True
            print(f"[Live] EXIT {direction} {ticker} @ {round_price(exit_price)} | P&L {pnl:+.0f} | {exit_reason}")
        
        else:
            # ── NO EXIT → Update unrealized P&L ──
            if direction == "LONG":
                upnl = (cmp - entry) * qty
                upnl_pct = ((cmp - entry) / entry) * 100
            else:
                upnl = (entry - cmp) * qty
                upnl_pct = ((entry - cmp) / entry) * 100
            
            # Load persistent state for this ticker
            live_state = _load_live_state()
            prev_pnl_map = live_state.get("last_pnl", {})
            prev_tg_map = live_state.get("last_tg", {})
            
            # Check if P&L % changed significantly (compare current % to previous %)
            prev_upnl_pct = prev_pnl_map.get(ticker, None)
            should_send = False
            
            if prev_upnl_pct is not None:
                # Both are percentages — correct comparison
                pnl_change = abs(upnl_pct - prev_upnl_pct)
                if pnl_change >= PNL_CHANGE_THRESHOLD_PCT:
                    should_send = True
            else:
                # First time seeing this ticker — send initial update
                should_send = True
            
            # Save current P&L % to state (not rupee value)
            prev_pnl_map[ticker] = round(upnl_pct, 2)
            
            # Check cooldown (25 min since last TG for this ticker)
            last_tg_str = prev_tg_map.get(ticker, "")
            cooldown_ok = True
            if last_tg_str:
                try:
                    last_tg = datetime.fromisoformat(last_tg_str)
                    cooldown_ok = (now - last_tg) >= TG_COOLDOWN
                except:
                    pass
            
            should_send = should_send and cooldown_ok
            
            if should_send:
                prev_tg_map[ticker] = now.isoformat()
                icon = "🟢" if upnl > 0 else ("🔴" if upnl < 0 else "⚪")
                update_msgs.append(
                    f"{icon} {ticker} {direction} | Live: {round_price(cmp)} "
                    f"| P&L Rs {upnl:+,.0f} ({upnl_pct:+.2f}%)"
                )
            
            # Save updated state
            live_state["last_pnl"] = prev_pnl_map
            live_state["last_tg"] = prev_tg_map
            _save_live_state(live_state)
            
            print(f"[Live] {ticker} {direction} | CMP={round_price(cmp)} "
                  f"Unrealized P&L Rs {upnl:+,.0f} ({upnl_pct:+.2f}%)" + 
                  (" → TG sent" if should_send else ""))
    
    # Save updated data
    if portfolio_updated:
        df.to_csv(PAPER_FILE, index=False)
        # Rebuild portfolio from the CSV (single source of truth) so bot.py and
        # live_pnl_updater.py can never diverge — both compute the same result.
        portfolio = rebuild_portfolio_from_csv()
        print(f"[Live] Portfolio rebuilt — {len(portfolio['open_positions'])} open, "
              f"{portfolio['closed_count']} closed")
    
    # Always log snapshot (append to CSV)
    _log_live_snapshot(portfolio)
    
    return closed_msgs, update_msgs


def _log_live_snapshot(portfolio: dict):
    """Log a timestamped snapshot of portfolio state to CSV."""
    try:
        now = datetime.now(IST)
        cap_by_mkt = portfolio.get("capital_by_market", {})
        total_cape = sum(cap_by_mkt.values())
        open_count = len(portfolio.get("open_positions", []))
        ret_pct = round((total_cape - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100, 2) if TOTAL_CAPITAL > 0 else 0
        total_pnl = portfolio.get("total_pnl", 0)
        
        row = {
            "Date": now.strftime("%Y-%m-%d"),
            "Time": now.strftime("%H:%M:%S IST"),
            "Type": "Live",
            "Capital": round(total_cape, 0),
            "Return_Pct": ret_pct,
            "Open": open_count,
            "Total_PnL": round(total_pnl, 0),
            "Cap_INDIAN": round(cap_by_mkt.get("INDIAN", 100000), 0),
            "Cap_US": round(cap_by_mkt.get("US", 100000), 0),
            "Cap_CRYPTO": round(cap_by_mkt.get("CRYPTO", 100000), 0),
            "Cap_INTRADAY": round(cap_by_mkt.get("INTRADAY", 100000), 0),
            "Cap_FADE": round(cap_by_mkt.get("FADE", 100000), 0),
        }
        df_new = pd.DataFrame([row])
        if os.path.exists(LIVE_PNL_LOG):
            df_old = pd.read_csv(LIVE_PNL_LOG, on_bad_lines='warn')
            df_comb = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df_comb = df_new
        df_comb.to_csv(LIVE_PNL_LOG, index=False)
    except Exception as e:
        print(f"[Live] Snapshot log error: {e}")


# ── Main ──────────────────────────────────────────────────────
def _commit_state_now():
    """Ephemeral-runner safeguard: commit state files IMMEDIATELY after trades
    change, so entries/exits survive even if the job is killed before the
    workflow's final 'Commit logs' step (timeout / push failure)."""
    try:
        import os, subprocess, time as _time
        base = os.path.dirname(os.path.abspath(__file__))
        script = os.path.join(base, ".ai", "commit_logs.sh")
        if not os.path.exists(script):
            print("[Commit] commit_logs.sh not found - skipping mid-run commit")
            return
        msg = "state " + _time.strftime("%Y-%m-%d %H:%M UTC", _time.gmtime())
        r = subprocess.run(["bash", script, msg], capture_output=True, text=True, timeout=150)
        out = (r.stdout or "").strip().splitlines()
        print(f"[Commit] mid-run commit rc={r.returncode} | {out[-1] if out else ''}")
    except Exception as e:
        print(f"[Commit] mid-run commit failed (non-fatal): {e}")

def main():
    print(f"\n{'='*60}")
    print(f"  LIVE P&L UPDATER v5.10")
    print(f"  {datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}")
    print(f"{'='*60}")
    
    # ── Initialize ALL system files FIRST ──
    initialize_system()
    
    # ── Initialize persistent live_pnl_state file ──
    state = _load_live_state()
    _save_live_state(state)
    
    start = time.time()
    closed_msgs, update_msgs = process_open_trades()
    elapsed = time.time() - start

    # ── Ephemeral-runner safeguard: persist closes NOW (see _commit_state_now). ──
    _commit_state_now()
    
    print(f"\n[Live] Check complete — {elapsed:.1f}s")
    # NOTE: per-position skip reasons are printed inside process_open_trades();
    # the skip counter lives there (not returned) so don't reference it here.
    print(f"[Live] Closed: {len(closed_msgs)} | Updates: {len(update_msgs)}")
    
    # Telegram: ALL exits in ONE batched message (was one msg per trade —
    # 6 SL hits = 6 back-to-back messages of clutter).
    if closed_msgs:
        batch_exits = (
            f"🚨 *EXITS ({len(closed_msgs)})* — "
            f"{datetime.now(IST).strftime('%H:%M:%S IST')}\n\n"
            + "\n\n".join(closed_msgs)
        )
        send_telegram(batch_exits)
    
    # Telegram: send unrealized P&L updates (batched, max 1 per ticker per 25 min)
    if update_msgs:
        batch = "\n".join(update_msgs[:5])  # Max 5 updates per batch
        if len(update_msgs) > 5:
            batch += f"\n... +{len(update_msgs) - 5} more"
        upnl_tg = (
            f"📊 *Live P&L Update*\n"
            f"{datetime.now(IST).strftime('%H:%M:%S IST')}\n\n"
            f"{batch}"
        )
        send_telegram(upnl_tg)
    
    # Summary only when something ACTUALLY changed since the last send —
    # identical "Portfolio Summary" repeats were pure noise.
    if closed_msgs or update_msgs:
        portfolio = load_portfolio()
        cap_by_mkt = portfolio.get("capital_by_market", {})
        total_cape = sum(cap_by_mkt.values())
        open_count = len(portfolio.get("open_positions", []))
        ret_pct = ((total_cape - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100) if TOTAL_CAPITAL > 0 else 0
        total_pnl = portfolio.get("total_pnl", 0)
        closed_n = portfolio.get('closed_count', 0)
        wins_n = portfolio.get('total_wins', 0)
        losses_n = portfolio.get('total_losses', 0)

        live_state = _load_live_state()
        last = live_state.get("last_summary", {})
        snapshot = {"cap": round(total_cape, 2), "pnl": round(total_pnl, 2),
                    "open": open_count, "closed": closed_n}
        if last == snapshot:
            print("[TG] Portfolio Summary unchanged — skipping")
        else:
            summary = (
                f"📊 *Portfolio Summary*\n"
                f"Capital: ₹{total_cape:,.0f} ({ret_pct:+.2f}%)\n"
                f"P&L: ₹{total_pnl:+,.0f}\n"
                f"Open: {open_count} | Closed: {closed_n}\n"
                f"Wins: {wins_n} | Losses: {losses_n}"
            )
            send_telegram(summary)
            live_state["last_summary"] = snapshot
            _save_live_state(live_state)
    
    # Auto-refresh strategy Excel report
    try:
        generate_strategy_report()
        integrity_errors = validate_all()
        if integrity_errors:
            raise RuntimeError("; ".join(integrity_errors[:5]))
    except Exception as e:
        log_error(f"Strategy Excel report failed: {e}")
        print(f"[WARN] Strategy Excel report: {e}")
        raise
    
    print(f"\n{'='*60}")
    print(f"  LIVE P&L UPDATER DONE")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error(f"Live P&L updater crashed: {e}")
        print(f"[FATAL] Live P&L updater crashed: {e}")
        traceback.print_exc()

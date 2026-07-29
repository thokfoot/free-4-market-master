"""
FREE 3-Market v5.7 — LIVE P&L UPDATER
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
import yfinance as yf
import pandas as pd
import requests
import pytz

from config import (
    CAPITAL, CAPITAL_BY_MARKET, TOTAL_CAPITAL,
    CHARGES_PER_MARKET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    MAX_HOLD_DAYS, INTRADAY_MAX_HOLD_HOURS,
    INTRADAY_CAPITAL,
)
from paper_trader import initialize_system
from logger import log_error

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


def _log_audit_exit(trade_row: dict):
    audit = _load_audit()
    audit.append({
        "event": "EXIT (Live)",
        "datetime": trade_row.get("Exit_Time", ""),
        "mode": trade_row.get("Mode", ""),
        "ticker": trade_row.get("Ticker", ""),
        "direction": trade_row.get("Direction", ""),
        "entry_price": trade_row.get("Entry_Price", ""),
        "exit_price": trade_row.get("Exit_Price", ""),
        "qty": trade_row.get("Qty", 0),
        "pnl": trade_row.get("P&L", ""),
        "pnl_pct": trade_row.get("P&L_%", ""),
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
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(STRATEGY_STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def update_strategy_stats(reason: str, pnl: float):
    rank = _extract_rank(reason)
    if rank == 0:
        return
    stats = _load_strategy_stats()
    key = str(rank)
    if key not in stats:
        stats[key] = {"rank": rank, "factors": reason[:80], "wins": 0, "losses": 0, "total_pnl": 0.0}
    stats[key]["total_pnl"] += pnl
    if pnl > 0:
        stats[key]["wins"] += 1
    else:
        stats[key]["losses"] += 1
    _save_strategy_stats(stats)


def load_portfolio() -> dict:
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "capital_by_market": {"INDIAN": 100000, "US": 100000, "CRYPTO": 100000, "INTRADAY": 100000},
        "open_positions": [],
        "closed_count": 0, "total_wins": 0, "total_losses": 0, "total_pnl": 0,
        "total_pnl_by_market": {"INDIAN": 0, "US": 0, "CRYPTO": 0, "INTRADAY": 0},
    }


def save_portfolio(port: dict):
    os.makedirs(LOG_DIR, exist_ok=True)
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
def fetch_live_ohlc(ticker: str) -> dict:
    """
    Fetch today's OHLC data using 1m interval.
    Returns {close, high, low, date} or None if failed.
    """
    for attempt in range(3):
        try:
            df = yf.download(ticker, period="1d", interval="1m",
                             progress=False, auto_adjust=False)
            if df is not None and len(df) > 0:
                # Handle multi-index columns
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                
                last = df.iloc[-1]
                current_close = float(last["Close"])
                daily_high = float(df["High"].max())
                daily_low = float(df["Low"].min())
                latest_date = str(df.index[-1].date())
                
                return {
                    "close": current_close,
                    "high": daily_high,
                    "low": daily_low,
                    "date": latest_date,
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
    now = datetime.now(IST)
    time_str = now.strftime("%H:%M:%S IST")
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
        
        # Fetch live 1m data
        ohlc = fetch_live_ohlc(ticker)
        if not ohlc:
            continue
        
        cmp = ohlc["close"]
        daily_high = ohlc["high"]
        daily_low = ohlc["low"]
        
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

        # ── SL/TP Check (intraday High/Low priority, with tolerance guard) ──
        # Use 0.01% tolerance to prevent 1-cent data noise from triggering exit
        _TOLERANCE = 0.9999
        exit_price = None
        exit_reason = None

        if direction == "LONG":
            if daily_low <= sl * _TOLERANCE:
                exit_price = sl
                exit_reason = "🎯 SL Hit (live)"
            elif daily_high >= target / _TOLERANCE:
                exit_price = target
                exit_reason = "🎯 Target Hit (live)"
        else:  # SHORT
            if daily_high >= sl / _TOLERANCE:
                exit_price = sl
                exit_reason = "🎯 SL Hit (live)"
            elif daily_low <= target * _TOLERANCE:
                exit_price = target
                exit_reason = "🎯 Target Hit (live)"
        
        if exit_price:
            # ── CLOSE THE TRADE ──
            # Gross P&L
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
            
            # Update CSV
            df.at[idx, "Exit_Price"] = round_price(exit_price)
            df.at[idx, "Exit_Time"] = time_str
            df.at[idx, "P&L"] = round(pnl, 2)
            df.at[idx, "P&L_%"] = round(pnl_pct, 2)
            df.at[idx, "Status"] = "CLOSED"
            df.at[idx, "Reason"] = str(row["Reason"]) + f" | {exit_reason}"
            
            # Update portfolio
            trade_tf = str(row.get("TimeFrame", "SWING_1d"))
            capital_key = "INTRADAY" if trade_tf == "INTRADAY_1h" else mode_norm
            mkt_cap = portfolio.setdefault("capital_by_market", {}).get(capital_key, 100000)
            portfolio["capital_by_market"][capital_key] = max(0, mkt_cap + pnl)
            portfolio["total_pnl"] += pnl
            tpnl_by_mkt = portfolio.setdefault("total_pnl_by_market", {})
            tpnl_by_mkt[capital_key] = tpnl_by_mkt.get(capital_key, 0) + pnl
            portfolio["closed_count"] += 1
            if pnl > 0:
                portfolio["total_wins"] += 1
            else:
                portfolio["total_losses"] += 1
            
            # Audit log
            _log_audit_exit({
                "Exit_Time": time_str, "Mode": row.get("Mode", ""),
                "Ticker": ticker, "Direction": direction,
                "Entry_Price": entry, "Exit_Price": round_price(exit_price),
                "Qty": qty, "P&L": round(pnl, 2), "P&L_%": round(pnl_pct, 2),
                "Pattern_Rank": row.get("Pattern_Rank", ""),
                "Expected_WinRate": row.get("Expected_WinRate", ""),
                "Pattern_Factors": row.get("Pattern_Factors", ""),
                "Reason": f"{row.get('Reason','')} | {exit_reason}",
            })
            
            # Strategy stats
            update_strategy_stats(str(row.get("Reason", "")), pnl)
            
            closed_msgs.append(f"{direction} {ticker} {exit_reason} @ {round_price(exit_price)} P&L Rs {pnl:+.0f} ({pnl_pct:+.1f}%)")
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
        open_df = df[df["Status"] == "OPEN"]
        portfolio["open_positions"] = open_df.to_dict(orient="records")
        save_portfolio(portfolio)
        print(f"[Live] Portfolio saved — {len(open_df)} open, {portfolio['closed_count']} closed")
    
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
def main():
    print(f"\n{'='*60}")
    print(f"  LIVE P&L UPDATER v5.7")
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
    
    print(f"\n[Live] Check complete — {elapsed:.1f}s")
    print(f"[Live] Closed: {len(closed_msgs)} | Updates: {len(update_msgs)}")
    
    # Telegram: send exit messages immediately
    tg_exit_msgs = []
    for msg in closed_msgs:
        tg_exit_msgs.append(msg)
        # Send each exit as separate message for visibility
        exit_tg = (
            f"🚨 *LIVE EXIT*\n"
            f"{msg}"
        )
        send_telegram(exit_tg)
    
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
    
    # Summary if anything happened
    if closed_msgs or update_msgs:
        portfolio = load_portfolio()
        cap_by_mkt = portfolio.get("capital_by_market", {})
        total_cape = sum(cap_by_mkt.values())
        open_count = len(portfolio.get("open_positions", []))
        ret_pct = ((total_cape - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100) if TOTAL_CAPITAL > 0 else 0
        total_pnl = portfolio.get("total_pnl", 0)
        
        summary = (
            f"📊 *Portfolio Summary*\n"
            f"Capital: ₹{total_cape:,.0f} ({ret_pct:+.2f}%)\n"
            f"P&L: ₹{total_pnl:+,.0f}\n"
            f"Open: {open_count} | Closed: {portfolio.get('closed_count', 0)}\n"
            f"Wins: {portfolio.get('total_wins', 0)} | Losses: {portfolio.get('total_losses', 0)}"
        )
        send_telegram(summary)
    
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

"""
FREE 3-Market v5.0 — PAPER TRADER
===================================
Portfolio & trade management for paper trading.
Supports LONG and SHORT positions with SL/TP and max hold.
"""

import os, json, math, pandas as pd
from datetime import datetime
import pytz
import requests
from config import (
    CAPITAL, CAPITAL_BY_MARKET, TOTAL_CAPITAL, RISK_PER_TRADE,
    SL_PCT, TP_PCT, MAX_HOLD_DAYS, MAX_CONCURRENT, STRATEGY_FILE,
    CHARGES_PER_MARKET,
    INTRADAY_CAPITAL, INTRADAY_SL_PCT, INTRADAY_TP_PCT,
    INTRADAY_MAX_HOLD_HOURS, INTRADAY_MAX_CONCURRENT,
    CAP_MAX_QTY_ULTRA_LOW, CAP_MAX_QTY_LOW, CAP_MAX_QTY_HIGH,
    SLIPPAGE_PCT, INTRADAY_SLIPPAGE_PCT,
    TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    MAX_CONSECUTIVE_LOSSES, CONSECUTIVE_LOSS_COOLDOWN_H,
)

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

PAPER_FILE = os.path.join(LOG_DIR, "paper_trades.csv")
PORTFOLIO_FILE = os.path.join(LOG_DIR, "portfolio.json")
STRATEGY_STATS_FILE = os.path.join(LOG_DIR, "strategy_stats.json")
LIVE_STATE_FILE_PT = os.path.join(LOG_DIR, "live_pnl_state.json")


# ── Atomic JSON Writer ───────────────────────────────────────
def _atomic_write_json(filepath: str, data):
    """
    Write JSON to a temporary file first, then atomically rename to target.
    Prevents corruption if the workflow is interrupted mid-write.
    """
    tmp = filepath + ".tmp." + str(os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        os.replace(tmp, filepath)
        print(f"[Atomic] Written {os.path.basename(filepath)}")
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except:
                pass
        print(f"[Atomic] WRITE FAILED for {os.path.basename(filepath)}: {e}")
        raise


# ── System Initialization ────────────────────────────────────
def initialize_system():
    """
    Initialize ALL portfolio/log files on first boot.
    Called at the start of every bot.py and live_pnl_updater.py run.
    
    Creates:
      1. paper_trades.csv — empty with proper columns
      2. portfolio.json — default capital structure
      3. strategy_stats.json — empty dict
      4. trade_audit.json — empty array
      5. live_pnl_state.json — default state
    
    Safe to call multiple times — only creates missing files.
    """
    os.makedirs(LOG_DIR, exist_ok=True)
    
    # 1. paper_trades.csv with correct 20 columns
    if not os.path.exists(PAPER_FILE):
        pd.DataFrame(columns=COLUMNS).to_csv(PAPER_FILE, index=False)
        print(f"[Init] Created {PAPER_FILE} ({len(COLUMNS)} columns, 0 rows)")
    else:
        # Verify columns are complete — add missing ones if needed
        try:
            df = pd.read_csv(PAPER_FILE, on_bad_lines='warn', nrows=0)
            existing_cols = set(df.columns)
            required_cols = set(COLUMNS)
            missing = required_cols - existing_cols
            if missing:
                print(f"[Init] Adding missing columns to {PAPER_FILE}: {missing}")
                df_full = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
                for col in missing:
                    df_full[col] = ""
                df_full.to_csv(PAPER_FILE, index=False)
        except Exception as e:
            print(f"[Init] Warning: Could not verify {PAPER_FILE}: {e}")
    
    # 2. strategy_stats.json
    if not os.path.exists(STRATEGY_STATS_FILE):
        _atomic_write_json(STRATEGY_STATS_FILE, {})
    
    # 3. portfolio.json
    if not os.path.exists(PORTFOLIO_FILE):
        save_portfolio(_default_portfolio())
    
    # 4. trade_audit.json
    if not os.path.exists(AUDIT_FILE):
        _atomic_write_json(AUDIT_FILE, [])
    
    # 5. live_pnl_state.json (also owned here for centralized init)
    if not os.path.exists(LIVE_STATE_FILE_PT):
        _atomic_write_json(LIVE_STATE_FILE_PT, {"last_tg": {}, "last_pnl": {}})
    
    print(f"[Init] System initialized — all log files present in {LOG_DIR}/")


# Cache for last known prices (updated each scan run)
_LAST_PRICES = {}  # {ticker: price}

def update_last_prices(prices: dict):
    """Update the cached current prices for computing unrealized P&L."""
    _LAST_PRICES.update(prices)

def _get_current_price(ticker: str) -> float:
    """Return cached current price or 0 if unknown."""
    return _LAST_PRICES.get(ticker, 0.0)

def _safe_num(val, default=""):
    """Convert a value to safe display string, handling NaN/None/empty."""
    if val is None:
        return default
    try:
        v = float(val)
        if v != v:  # NaN check
            return default
        return str(v)
    except (ValueError, TypeError):
        s = str(val).strip()
        return s if s else default

def _safe_float(val) -> float:
    """Convert to float, returning 0.0 for NaN/None/empty."""
    if val is None:
        return 0.0
    try:
        v = float(val)
        return 0.0 if v != v else v  # NaN → 0
    except (ValueError, TypeError):
        return 0.0

COLUMNS = [
    "Date","Time_IST","Mode","Ticker","Direction","TimeFrame",
    "Entry_Price","Qty","SL","Target","MaxHold",
    "Exit_Price","Exit_Time","P&L","P&L_%","Status",
    "Pattern_Rank","Expected_WinRate","Pattern_Factors","Reason"
]

AUDIT_FILE = os.path.join(LOG_DIR, "trade_audit.json")


def _load_audit() -> list:
    """Load persistent trade audit log."""
    if os.path.exists(AUDIT_FILE):
        try:
            with open(AUDIT_FILE, "r") as f:
                return json.load(f)
        except (json.JSONDecodeError, Exception):
            print(f"[Audit] WARNING: Corrupted audit file, resetting")
            return []
    return []


def _save_audit(audit: list):
    """Save persistent trade audit log (atomic write)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    _atomic_write_json(AUDIT_FILE, audit)


def _log_audit_entry(trade: dict):
    """Log a trade entry to the persistent audit trail."""
    audit = _load_audit()
    audit.append({
        "event": "ENTRY",
        "datetime": f"{trade['Date']} {trade['Time_IST']}",
        "mode": trade["Mode"],
        "ticker": trade["Ticker"],
        "direction": trade["Direction"],
        "entry_price": trade["Entry_Price"],
        "qty": trade["Qty"],
        "sl": trade["SL"],
        "target": trade["Target"],
        "pattern_rank": trade.get("Pattern_Rank", ""),
        "expected_win_rate": trade.get("Expected_WinRate", ""),
        "pattern_factors": trade.get("Pattern_Factors", ""),
        "reason": trade.get("Reason", ""),
    })
    _save_audit(audit)
    print(f"[Audit] ENTRY logged: {trade['Direction']} {trade['Ticker']} Rank#{trade.get('Pattern_Rank','?')} Expected WR={trade.get('Expected_WinRate','?')}%")


def _log_audit_exit(trade_row: dict):
    """Log a trade exit to the persistent audit trail."""
    audit = _load_audit()
    audit.append({
        "event": "EXIT",
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
    print(f"[Audit] EXIT logged: {trade_row.get('Direction','?')} {trade_row.get('Ticker','?')} P&L={trade_row.get('P&L','?')} Expected WR={trade_row.get('Expected_WinRate','?')}%")


def round_price(price):
    """Round price appropriately based on magnitude."""
    p = float(price)
    if p >= 1000: return round(p, 2)
    if p >= 100: return round(p, 2)
    if p >= 1: return round(p, 2)
    if p >= 0.1: return round(p, 4)
    if p >= 0.01: return round(p, 6)
    return round(p, 8)


def _apply_slippage(price: float, direction: str, action: str, mode: str, tf: str) -> float:
    """
    Apply realistic fill slippage to a price.
    
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
    is_intraday = (tf == "INTRADAY_1h")
    slip_pct = (
        INTRADAY_SLIPPAGE_PCT.get(mode, 0.0)
        if is_intraday else
        SLIPPAGE_PCT.get(mode, 0.0)
    )
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


def _default_portfolio() -> dict:
    """Return default portfolio with per-market capital."""
    cap = dict(CAPITAL_BY_MARKET)
    cap["INTRADAY"] = INTRADAY_CAPITAL
    return {
        "capital_by_market": cap,
        "open_positions": [],
        "closed_count": 0,
        "total_wins": 0,
        "total_losses": 0,
        "total_pnl": 0,
        "total_pnl_by_market": {"INDIAN": 0.0, "US": 0.0, "CRYPTO": 0.0, "INTRADAY": 0.0},
        "total_capital": sum(cap.values()),
    }


def load_portfolio() -> dict:
    """Load portfolio from JSON file. Migrates old format automatically."""
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                data = json.load(f)
            # Migrate old format (single capital -> per-market)
            if "capital" in data and "capital_by_market" not in data:
                port = _default_portfolio()
                port["open_positions"] = data.get("open_positions", [])
                port["closed_count"] = data.get("closed_count", 0)
                port["total_wins"] = data.get("total_wins", 0)
                port["total_losses"] = data.get("total_losses", 0)
                port["total_pnl"] = data.get("total_pnl", 0)
                # Distribute old capital proportionally (fallback: equal split)
                old_cap = float(data.get("capital", sum(CAPITAL_BY_MARKET.values())))
                total_init = sum(CAPITAL_BY_MARKET.values())
                ratio = old_cap / total_init if total_init > 0 else 1
                for mkt in port["capital_by_market"]:
                    port["capital_by_market"][mkt] = CAPITAL_BY_MARKET[mkt] * ratio
                # Distribute old PnL
                old_pnl = float(data.get("total_pnl", 0))
                if old_pnl != 0 and port["open_positions"]:
                    # Split PnL equally if we don't have per-market records
                    per_market = old_pnl / 3
                    for mkt in port["total_pnl_by_market"]:
                        port["total_pnl_by_market"][mkt] = round(per_market, 2)
                save_portfolio(port)
                print(f"[Paper] Migrated portfolio to per-market format")
                return port
            return data
        except Exception as e:
            print(f"[Paper] Portfolio load error: {e}, using defaults")
    return _default_portfolio()


def save_portfolio(port: dict):
    """Save portfolio to JSON using atomic write (temp file + rename).
    Prevents file corruption if workflow is interrupted mid-write."""
    os.makedirs(LOG_DIR, exist_ok=True)
    _atomic_write_json(PORTFOLIO_FILE, port)


def calculate_qty(entry: float, sl: float, market: str = "US", tf: str = "SWING_1d") -> int:
    """Calculate position size based on risk per trade (1% of market capital)."""
    port = load_portfolio()
    if tf == "INTRADAY_1h":
        mkt_cap = port.get("capital_by_market", {}).get("INTRADAY", INTRADAY_CAPITAL)
        risk_amt = mkt_cap * RISK_PER_TRADE
    else:
        mkt_cap = port.get("capital_by_market", {}).get(market, CAPITAL_BY_MARKET.get(market, 100000))
        risk_amt = mkt_cap * RISK_PER_TRADE
    risk_per_share = abs(entry - sl)
    if risk_per_share < 1e-9:
        return 0
    qty = int(risk_amt / risk_per_share)
    # Caps based on price (configurable via config.py)
    if entry < 0.1:
        qty = min(qty, CAP_MAX_QTY_ULTRA_LOW)
    elif entry < 1:
        qty = min(qty, CAP_MAX_QTY_LOW)
    elif entry > 100:
        qty = min(qty, CAP_MAX_QTY_HIGH)
    return max(1, qty)


def enter_trade(mode: str, ticker: str, direction: str, entry_price: float,
                 reason: str, pattern_rank: int = None,
                 expected_win_rate: float = None,
                 pattern_factors: str = None,
                 tf: str = "SWING_1d") -> dict:
    """
    Enter a paper trade.
    
    Args:
        mode: "INDIAN" | "US" | "CRYPTO"
        ticker: yfinance ticker
        direction: "LONG" | "SHORT"
        entry_price: Entry price
        reason: Signal description
        pattern_rank: Rank from CSV
        expected_win_rate: Expected win rate from CSV
        pattern_factors: Full factor string
        tf: "SWING_1d" or "INTRADAY_1h"
    
    Returns:
        Trade dict or None if rejected
    """
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    portfolio = load_portfolio()
    
    # Check max concurrent (separate limits for swing vs intraday)
    open_positions = portfolio.get("open_positions", [])
    if tf == "INTRADAY_1h":
        intraday_open = [p for p in open_positions if p.get("TimeFrame") == "INTRADAY_1h"]
        if len(intraday_open) >= INTRADAY_MAX_CONCURRENT:
            print(f"[Paper] INTRADAY MAX CONCURRENT ({INTRADAY_MAX_CONCURRENT}), skip {ticker}")
            return None
    elif len(open_positions) >= MAX_CONCURRENT:
        print(f"[Paper] MAX CONCURRENT ({MAX_CONCURRENT}), skip {ticker}")
        return None
    
    # Check duplicate (same ticker, same direction, open)
    for pos in portfolio["open_positions"]:
        if pos["Ticker"] == ticker and pos["Direction"] == direction:
            print(f"[Paper] Duplicate {ticker} {direction} already open, skip")
            return None
    
    # ── Consecutive Loss Guard ──
    # Skip entry if same ticker+strategy has lost N+ times consecutively
    # and cooldown hasn't expired yet.
    if pattern_rank is not None:
        if not _check_consecutive_loss_cooldown(ticker, pattern_rank):
            print(f"[Paper] Consecutive loss cooldown active — skip {ticker} Rank#{pattern_rank}")
            return None
    
    # Calculate SL/TP based on direction AND timeframe
    is_intraday = (tf == "INTRADAY_1h")
    if is_intraday:
        sl_pct = INTRADAY_SL_PCT.get(mode, 0.01)
        tp_pct = INTRADAY_TP_PCT.get(mode, 0.02)
        max_hold = INTRADAY_MAX_HOLD_HOURS.get(mode, 6)
    else:
        sl_pct = SL_PCT
        tp_pct = TP_PCT
        max_hold = MAX_HOLD_DAYS
    
    if direction == "LONG":
        sl = round_price(entry_price * (1 - sl_pct))
        target = round_price(entry_price * (1 + tp_pct))
    else:  # SHORT
        sl = round_price(entry_price * (1 + sl_pct))
        target = round_price(entry_price * (1 - tp_pct))
    
    # ── Apply entry slippage for realistic fills ──
    actual_entry = _apply_slippage(entry_price, direction, "ENTRY", mode, tf)
    entry = round_price(actual_entry)
    qty = calculate_qty(entry, sl, mode, tf)
    if qty == 0:
        return None
    
    # Build reason with pattern rank
    full_reason = reason
    if pattern_rank:
        tf_label = "ID" if is_intraday else "SW"
        full_reason = f"#{pattern_rank}{tf_label} {reason}"
    
    trade = {
        "Date": date_str,
        "Time_IST": time_str,
        "Mode": mode,
        "Ticker": ticker,
        "Direction": direction,
        "TimeFrame": tf,
        "Entry_Price": entry,
        "Qty": qty,
        "SL": sl,
        "Target": target,
        "MaxHold": max_hold,
        "Exit_Price": "",
        "Exit_Time": "",
        "P&L": "",
        "P&L_%": "",
        "Status": "OPEN",
        "Pattern_Rank": pattern_rank if pattern_rank else "",
        "Expected_WinRate": expected_win_rate if expected_win_rate else "",
        "Pattern_Factors": pattern_factors if pattern_factors else "",
        "Reason": full_reason,
    }
    
    # Append to CSV (handle TimeFrame column migration for old rows)
    df_new = pd.DataFrame([trade])[COLUMNS]
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(PAPER_FILE):
        df_old = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        # ── Ensure string columns are object dtype (prevent float64 inference) ──
        str_cols_pt = ["Exit_Price", "Exit_Time", "P&L", "P&L_%", "Status", "Reason", "Date", "Time_IST", "Mode", "Ticker", "Direction", "TimeFrame", "Pattern_Rank", "Expected_WinRate", "Pattern_Factors"]
        for col in str_cols_pt:
            if col in df_old.columns:
                df_old[col] = df_old[col].astype(object)
        if "TimeFrame" not in df_old.columns:
            df_old["TimeFrame"] = "SWING_1d"
        df_comb = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_comb = df_new
    df_comb.to_csv(PAPER_FILE, index=False)
    
    # Update portfolio (market-specific capital remains unchanged at entry)
    portfolio["open_positions"].append(trade)
    save_portfolio(portfolio)
    
    print(f"[Paper] ENTER {direction} {ticker} @ {entry} Qty {qty} SL {sl} TGT {target}")
    
    # Log to persistent audit trail
    _log_audit_entry(trade)
    
    return trade


# ===== CONSECUTIVE LOSS GUARD =====
# Prevents re-entering the same ticker+strategy after repeated losses.

def _consecutive_losses_for_strategy(ticker: str, strategy_rank) -> int:
    """
    Count consecutive closed losses for (ticker, strategy_rank).
    Scans paper_trades.csv chronologically (newest first).
    Returns 0 if last trade was a win, otherwise count of consecutive losses.
    """
    if not os.path.exists(PAPER_FILE):
        return 0
    try:
        df = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        if df.empty:
            return 0
        
        rank_str = str(int(strategy_rank)) if strategy_rank is not None else ""
        if not rank_str:
            return 0
        
        mask = (
            (df["Ticker"].astype(str).str.strip() == str(ticker).strip()) &
            (df["Pattern_Rank"].astype(str).str.strip() == rank_str) &
            (df["Status"].astype(str).str.strip() == "CLOSED")
        )
        matches = df[mask].sort_values(["Date", "Time_IST"], ascending=[False, False])
        if matches.empty:
            return 0
        
        count = 0
        for _, r in matches.iterrows():
            pnl = r.get("P&L")
            try:
                pnl_val = float(pnl)
            except (ValueError, TypeError):
                break
            if pnl_val < 0:
                count += 1
            else:
                break  # Win breaks the consecutive loss streak
        return count
    except Exception as e:
        print(f"[LossGuard] Error counting consecutive losses: {e}")
        return 0


def _check_consecutive_loss_cooldown(ticker: str, strategy_rank) -> bool:
    """
    Check if (ticker, strategy_rank) is on cooldown due to consecutive losses.
    Returns True if entry is ALLOWED, False if it should be BLOCKED.
    """
    if strategy_rank is None:
        return True  # No rank info — always allow
    
    consec = _consecutive_losses_for_strategy(ticker, strategy_rank)
    if consec < MAX_CONSECUTIVE_LOSSES:
        return True  # Below threshold — allow
    
    # Hit threshold — check cooldown time from last exit
    try:
        df = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        if df.empty:
            return True
        
        rank_str = str(int(strategy_rank))
        mask = (
            (df["Ticker"].astype(str).str.strip() == str(ticker).strip()) &
            (df["Pattern_Rank"].astype(str).str.strip() == rank_str) &
            (df["Status"].astype(str).str.strip() == "CLOSED")
        )
        matches = df[mask].sort_values(["Date", "Time_IST"], ascending=[False, False])
        if matches.empty:
            return True
        
        last = matches.iloc[0]
        last_date = str(last["Date"])
        last_time_raw = str(last.get("Exit_Time", str(last.get("Time_IST", "00:00:00"))))
        # Strip timezone suffix like "IST" or "UTC"
        for suffix in [" IST", " UTC", "IST", "UTC"]:
            if last_time_raw.endswith(suffix):
                last_time_raw = last_time_raw[: -len(suffix)]
                break
        if not last_time_raw.strip():
            last_time_raw = "00:00:00"
        
        last_dt = datetime.strptime(f"{last_date} {last_time_raw.strip()}", "%Y-%m-%d %H:%M:%S")
        now = datetime.now()
        hours_since = (now - last_dt).total_seconds() / 3600
        
        if hours_since < CONSECUTIVE_LOSS_COOLDOWN_H:
            print(f"[LossGuard] SKIP {ticker} Rank#{rank_str}: {consec} consecutive losses, "
                  f"only {hours_since:.0f}h since last (cooldown={CONSECUTIVE_LOSS_COOLDOWN_H}h)")
            return False
        
        print(f"[LossGuard] ALLOW {ticker} Rank#{rank_str}: cooldown expired ({hours_since:.0f}h >= {CONSECUTIVE_LOSS_COOLDOWN_H}h)")
        return True
    except Exception as e:
        print(f"[LossGuard] Error checking cooldown: {e}")
        return True


# ===== PER-STRATEGY WIN RATE TRACKING =====

def _extract_rank(reason: str) -> int:
    """Extract pattern rank from Reason field like '#1 Price<SMA50+EMA9>EMA20'."""
    if not reason:
        return 0
    import re
    match = re.match(r"#?(\d+)", reason.strip())
    if match:
        return int(match.group(1))
    return 0


def _load_strategy_stats() -> dict:
    """Load per-strategy win/Loss tracking from JSON."""
    if os.path.exists(STRATEGY_STATS_FILE):
        try:
            with open(STRATEGY_STATS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def _save_strategy_stats(stats: dict):
    """Save per-strategy tracking (atomic write)."""
    os.makedirs(LOG_DIR, exist_ok=True)
    _atomic_write_json(STRATEGY_STATS_FILE, stats)


def update_strategy_stats(reason: str, pnl: float):
    """
    Update win/loss tracking for the pattern that generated this trade.
    Called when a trade is closed.
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
        }
    
    stats[key]["total_pnl"] += pnl
    if pnl > 0:
        stats[key]["wins"] += 1
    else:
        stats[key]["losses"] += 1
    # Update the reason/factors in case it was truncated
    if len(reason) > len(stats[key]["factors"]):
        stats[key]["factors"] = reason[:80]
    
    _save_strategy_stats(stats)
    total = stats[key]["wins"] + stats[key]["losses"]
    wr = round(stats[key]["wins"] / total * 100, 1) if total > 0 else 0
    print(f"[Strategy] Rank #{rank} updated: {stats[key]['wins']}W/{stats[key]['losses']}L ({wr}%) PnL Rs {pnl:+.0f}")


def get_strategy_stats(top_n: int = 5) -> list:
    """
    Get best and worst strategies by win rate.
    
    Returns:
        {
            "top": [{rank, factors, wins, losses, win_rate, total_pnl}, ...],
            "bottom": [{rank, factors, wins, losses, win_rate, total_pnl}, ...],
        }
    """
    stats = _load_strategy_stats()
    if not stats:
        return {"top": [], "bottom": []}
    
    rows = []
    for key, data in stats.items():
        total = data.get("wins", 0) + data.get("losses", 0)
        wr = round(data["wins"] / total * 100, 1) if total > 0 else 0
        rows.append({
            "rank": data["rank"],
            "factors": data.get("factors", "")[:50],
            "wins": data["wins"],
            "losses": data["losses"],
            "total": total,
            "win_rate": wr,
            "total_pnl": round(data.get("total_pnl", 0), 0),
        })
    
    # Sort by win rate descending
    rows.sort(key=lambda x: x["win_rate"], reverse=True)
    top = rows[:top_n]
    
    # Sort by win rate ascending (worst first)
    rows.sort(key=lambda x: x["win_rate"])
    bottom = [r for r in rows if r["total"] >= 2][:top_n]  # Only if 2+ trades
    
    return {"top": top, "bottom": bottom}


def _html_escape(text):
    """Escape text for safe HTML embedding. Handles NaN gracefully."""
    if text is None:
        return ""
    try:
        v = float(text)
        if v != v:  # NaN check
            return ""
    except (ValueError, TypeError):
        pass
    s = str(text)
    if s.strip().lower() == "nan":
        return ""
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("\n", " "))


def _pnl_class(val):
    """Return CSS class for P&L value."""
    try:
        v = float(val)
        if v > 0: return "profit"
        if v < 0: return "loss"
    except:
        pass
    return ""


def _calc_unrealized_pnl(row) -> tuple:
    """
    Calculate current (unrealized) P&L for an open trade.
    Returns (pnl, pnl_pct) or (0, 0) if no current price.
    """
    ticker = row.get("Ticker", "")
    current = _get_current_price(ticker)
    if current <= 0:
        return (0.0, 0.0)
    entry = _safe_float(row.get("Entry_Price", 0))
    qty = int(_safe_float(row.get("Qty", 0)))
    direction = str(row.get("Direction", "LONG"))
    if entry <= 0 or qty <= 0:
        return (0.0, 0.0)
    if direction == "LONG":
        pnl = (current - entry) * qty
        pnl_pct = ((current - entry) / entry) * 100
    else:
        pnl = (entry - current) * qty
        pnl_pct = ((entry - current) / entry) * 100
    return (pnl, pnl_pct)


# ── SL/TP Telegram Alert ────────────────────────────────────
_SLTP_ALERT_COOLDOWN = {}  # {ticker: timestamp} — prevent duplicate alerts within same scan

def _send_sl_tp_alert(ticker: str, direction: str, exit_reason: str,
                       entry: float, exit_price: float, sl: float, target: float,
                       cmp: float, daily_high: float, daily_low: float,
                       pnl: float, pnl_pct: float, qty: int,
                       rank_str: str = ""):
    """
    Send an immediate Telegram alert when SL or TP is hit.
    Includes the OHLC data that triggered the exit for transparency.
    Rate-limited to 1 alert per ticker per scan run.
    """
    # Rate limit: skip if already sent for this ticker in this run
    if _SLTP_ALERT_COOLDOWN.get(ticker):
        return
    _SLTP_ALERT_COOLDOWN[ticker] = datetime.now(IST)
    
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print(f"[Alert] No TG credentials, skipping alert for {ticker}")
        return
    
    # Determine alert icon and label
    if "SL" in exit_reason:
        icon = "🚨"
        event_label = "SL HIT"
    elif "Target" in exit_reason:
        icon = "🎯"
        event_label = "TP HIT"
    elif "Expiry" in exit_reason:
        icon = "⏰"
        event_label = "EXPIRY"
    else:
        icon = "📊"
        event_label = "EXIT"
    
    dir_arrow = "🟢" if direction == "LONG" else "🔴"
    pnl_icon = "🟢" if pnl > 0 else ("🔴" if pnl < 0 else "⚪")
    
    rank_tag = f" #{rank_str}" if rank_str else ""
    msg = (
        f"{icon} *{event_label}:* {dir_arrow} `{ticker}`{rank_tag} {direction}\n"
        f"┣ Entry: {round_price(entry)} | Exit: {round_price(exit_price)}\n"
        f"┣ SL: {round_price(sl)} | TP: {round_price(target)}\n"
        f"┣ Qty: {qty} | {pnl_icon} P&L: Rs {pnl:+,.0f} ({pnl_pct:+.2f}%)\n"
        f"┣ *OHLC:* Close={cmp:.2f} High={daily_high:.2f} Low={daily_low:.2f}\n"
        f"┗ Reason: {exit_reason}"
    )
    
    for attempt in range(2):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                timeout=10,
            )
            if r.status_code == 200:
                print(f"[Alert] TG alert sent: {ticker} {event_label}")
                return
            else:
                print(f"[Alert] TG attempt {attempt+1} failed: {r.text[:100]}")
        except Exception as e:
            print(f"[Alert] TG attempt {attempt+1} exception: {e}")
    print(f"[Alert] Failed to send TG alert for {ticker}")


def generate_portfolio_report(current_prices: dict = None):
    """
    Generate a professional HTML portfolio report with EVERYTHING.
    
    Sections:
      1. Summary Cards — Capital, Return, Win Rate, Trades
      2. All Trades Table — Full audit trail
      3. Per-Ticker — Win rate, PnL per ticker
      4. Per-Market — Performance by region
      5. Portfolio History — Capital snapshots
    
    File: logs/portfolio_report.html (viewable in browser)
    """
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    
    portfolio = load_portfolio()
    cap_by_mkt = portfolio.get("capital_by_market", dict(CAPITAL_BY_MARKET))
    
    # Update cached prices for unrealized P&L calculations
    if current_prices:
        update_last_prices(current_prices)
    total_cape = sum(cap_by_mkt.values())
    open_count = len(portfolio.get("open_positions", []))
    closed_cnt = portfolio.get("closed_count", 0)
    wins = portfolio.get("total_wins", 0)
    losses = portfolio.get("total_losses", 0)
    total_pnl = portfolio.get("total_pnl", 0)
    total_closed = wins + losses
    win_rate = round(wins / total_closed * 100, 1) if total_closed > 0 else 0
    ret_pct = round((total_cape - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100, 2)
    total_trades = total_closed + open_count
    
    # Profit/loss direction
    pnl_direction = "▲" if ret_pct > 0 else ("▼" if ret_pct < 0 else "◆")
    pnl_color = "#00c853" if ret_pct > 0 else ("#ff5252" if ret_pct < 0 else "#888")
    
    parts = []
    parts.append(f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Portfolio Report — {_html_escape(date_str)}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: #0d1117; color: #e6edf3; padding: 24px; line-height: 1.6;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 4px; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin: 28px 0 14px; color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 6px; }}
  .subtitle {{ color: #8b949e; font-size: 0.85rem; margin-bottom: 20px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; text-align: center; transition: border-color .2s;
  }}
  .card:hover {{ border-color: #58a6ff; }}
  .card .value {{ font-size: 1.5rem; font-weight: 700; margin: 4px 0 2px; }}
  .card .label {{ font-size: 0.75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card .change {{ font-size: 0.85rem; }}
  .mkt-row {{ display: flex; justify-content: space-around; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
  .mkt-item {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 10px 16px; text-align: center; min-width: 130px; }}
  .mkt-item .mkt-label {{ font-size: 0.7rem; color: #8b949e; text-transform: uppercase; }}
  .mkt-item .mkt-value {{ font-size: 1.1rem; font-weight: 700; }}
  table {{
    width: 100%; border-collapse: collapse; font-size: 0.82rem;
    background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden;
  }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #21262d; }}
  th {{ background: #21262d; color: #8b949e; font-weight: 600; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.5px; }}
  tr:hover {{ background: #1c2128; }}
  .profit {{ color: #00c853 !important; font-weight: 600; }}
  .loss {{ color: #ff5252 !important; font-weight: 600; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
  }}
  .badge-long {{ background: #003d1a; color: #00c853; }}
  .badge-short {{ background: #3d0000; color: #ff5252; }}
  .badge-open {{ background: #003055; color: #58a6ff; }}
  .badge-closed {{ background: #21262d; color: #8b949e; }}
  .badge-sl {{ background: #3d0000; color: #ff5252; }}
  .badge-target {{ background: #003d1a; color: #00c853; }}
  .badge-expiry {{ background: #3d2e00; color: #ffc107; }}
  .section-summary {{ color: #8b949e; font-size: 0.82rem; margin-top: 6px; }}
  .footer {{ margin-top: 30px; padding-top: 16px; border-top: 1px solid #30363d; text-align: center; color: #484f58; font-size: 0.75rem; }}
  .scroll {{ overflow-x: auto; }}
  @media (max-width: 600px) {{
    body {{ padding: 12px; }}
    .cards {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .mkt-row {{ gap: 4px; }}
    .mkt-item {{ min-width: 100px; padding: 8px; }}
  }}
</style>
</head>
<body>
<div class="container">
  <h1>📊 Portfolio Report — v5.0</h1>
  <div class="subtitle">{_html_escape(date_str)} {_html_escape(time_str)} • Generated by FREE 3-Market Paper Trade Bot • <a href="https://github.com/thokfoot/free-4-market-master" style="color:#58a6ff">thokfoot/free-4-market-master</a></div>
  
  <div class="cards">
    <div class="card"><div class="label">Total Capital</div><div class="value" style="color:{pnl_color}">₹{total_cape:,.0f}</div><div class="change" style="color:{pnl_color}">{pnl_direction} {ret_pct:+.2f}%</div></div>
    <div class="card"><div class="label">Total P&amp;L</div><div class="value" style="color:{"#00c853" if total_pnl>0 else "#ff5252" if total_pnl<0 else "#888"}">₹{total_pnl:+,.0f}</div><div class="change">{total_trades} trades</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value">{win_rate}%</div><div class="change">{wins}W / {losses}L</div></div>
    <div class="card"><div class="label">Open / Closed</div><div class="value">{open_count} / {closed_cnt}</div><div class="change">{total_closed + open_count} total</div></div>
  </div>
  
  <div class="mkt-row">
''')
    # Per-market cards (including INTRADAY)
    mkt_icons = {"INDIAN": "🇮🇳", "US": "🇺🇸", "CRYPTO": "₿", "INTRADAY": "⚡"}
    mkt_init_map = dict(CAPITAL_BY_MARKET)
    mkt_init_map["INTRADAY"] = INTRADAY_CAPITAL
    for mkt in ["INDIAN", "US", "CRYPTO", "INTRADAY"]:
        mkt_cap = cap_by_mkt.get(mkt, mkt_init_map.get(mkt, 100000))
        mkt_init = mkt_init_map.get(mkt, 100000)
        mkt_ret = ((mkt_cap - mkt_init) / mkt_init * 100) if mkt_init > 0 else 0
        mkt_arrow = "▲" if mkt_ret > 0 else ("▼" if mkt_ret < 0 else "◆")
        mkt_clr = "#00c853" if mkt_ret > 0 else ("#ff5252" if mkt_ret < 0 else "#888")
        parts.append(f'    <div class="mkt-item"><div class="mkt-label">{mkt_icons.get(mkt,"")} {mkt}</div>'
                     f'<div class="mkt-value" style="color:{mkt_clr}">₹{mkt_cap:,.0f}</div>'
                     f'<div style="font-size:0.75rem;color:{mkt_clr}">{mkt_arrow} {mkt_ret:+.2f}%</div></div>')
    parts.append('  </div>')
    
    # ============================================================
    # SECTION 2: ALL TRADES
    # ============================================================
    parts.append('<h2>📋 All Trades</h2>')
    
    if os.path.exists(PAPER_FILE):
        df = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        if len(df) > 0:
            parts.append('<div class="scroll"><table>')
            cols = ["Date", "Ticker", "Direction", "Entry_Price", "Qty", "SL", "Target", 
                    "Exit_Price", "P&L", "P&L_%", "Status", "Reason"]
            cols = [c for c in cols if c in df.columns]
            
            parts.append("<tr>" + "".join(f"<th>{c.replace('_', ' ')}</th>" for c in cols) + "</tr>")
            
            # Show newest trades FIRST (reverse order)
            for _, row in df.iloc[::-1].iterrows():
                direction = str(row.get("Direction", ""))
                status = str(row.get("Status", ""))
                pnl_raw = row.get("P&L", None)
                pnl_pct_raw = row.get("P&L_%", None)
                
                # For OPEN trades, calculate current/unrealized P&L
                is_open = (status == "OPEN")
                ticker = str(row.get("Ticker", ""))
                has_current_price = _get_current_price(ticker) > 0
                
                if is_open and has_current_price:
                    unrealized_pnl, unrealized_pnl_pct = _calc_unrealized_pnl(row)
                    pnl_display = f"{unrealized_pnl:+,.0f}"
                    pnl_class = _pnl_class(unrealized_pnl)
                    pnl_pct_display = f"{unrealized_pnl_pct:+.2f}%"
                    pnl_pct_class = _pnl_class(unrealized_pnl_pct)
                elif is_open:
                    # OPEN trade but no current price available yet
                    pnl_display = "—"
                    pnl_class = ""
                    pnl_pct_display = "—"
                    pnl_pct_class = ""
                else:
                    pnl_safe = _safe_num(pnl_raw, "—")
                    pnl_pct_safe = _safe_num(pnl_pct_raw, "—")
                    pnl_display = pnl_safe
                    pnl_class = _pnl_class(pnl_safe)
                    pnl_pct_display = pnl_pct_safe
                    pnl_pct_class = _pnl_class(pnl_pct_safe)
                
                badge_dir = f'<span class="badge badge-{"long" if direction=="LONG" else "short"}">{_html_escape(direction)}</span>'
                badge_status = f'<span class="badge badge-{"open" if is_open else "closed"}">{_html_escape(status)}</span>'
                
                exit_price = row.get("Exit_Price", None)
                exit_safe = _safe_num(exit_price, "—")
                exit_display = exit_safe if exit_safe != "—" else "—"
                if is_open:
                    cp = _get_current_price(ticker)
                    exit_display = f"◉ {cp}" if cp > 0 else "—"
                
                reason = str(row.get("Reason", ""))
                reason_short = reason[:50] + ("..." if len(reason) > 50 else "")
                
                parts.append(f"<tr><td>{_html_escape(row.get('Date',''))}</td>")
                parts.append(f"<td><b>{_html_escape(row.get('Ticker',''))}</b></td>")
                parts.append(f"<td>{badge_dir}</td>")
                parts.append(f"<td>{_html_escape(row.get('Entry_Price',''))}</td>")
                parts.append(f"<td>{_html_escape(row.get('Qty',''))}</td>")
                parts.append(f"<td>{_html_escape(row.get('SL',''))}</td>")
                parts.append(f"<td>{_html_escape(row.get('Target',''))}</td>")
                parts.append(f"<td>{exit_display}</td>")
                parts.append(f'<td class="{pnl_class}">{pnl_display}</td>')
                parts.append(f'<td class="{pnl_pct_class}">{pnl_pct_display}</td>')
                parts.append(f"<td>{badge_status}</td>")
                parts.append(f"<td style=\"font-size:0.75rem;color:#8b949e\">{_html_escape(reason_short)}</td>")
                parts.append("</tr>")
            parts.append('</table></div>')
            parts.append(f'<div class="section-summary">Showing {len(df)} trade(s)</div>')
        else:
            parts.append('<div style="color:#8b949e;padding:20px;text-align:center">No trades recorded yet.</div>')
    else:
        parts.append('<div style="color:#8b949e;padding:20px;text-align:center">No trades recorded yet.</div>')
    
    # ============================================================
    # SECTION 3: PER-TICKER PERFORMANCE
    # ============================================================
    if os.path.exists(PAPER_FILE):
        df_trades = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        if len(df_trades) > 0:
            parts.append('<h2>📈 Per-Ticker Performance</h2>')
            parts.append('<div class="scroll"><table>')
            parts.append("<tr><th>Ticker</th><th>Direction</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>Total P&amp;L</th></tr>")
            
            for (ticker, direction), grp in df_trades.groupby(["Ticker", "Direction"]):
                pnl_vals = []
                for _, r in grp.iterrows():
                    v = r.get("P&L")
                    if pd.notna(v) and str(v).strip() != "":
                        try:
                            pnl_vals.append(float(v))
                        except:
                            pass
                w = sum(1 for v in pnl_vals if v > 0)
                l = sum(1 for v in pnl_vals if v < 0)
                t = len(grp)
                wr = round(w / (w + l) * 100, 1) if (w + l) > 0 else 0
                total_pnl_t = round(sum(pnl_vals), 0)
                pnl_c = "profit" if total_pnl_t > 0 else ("loss" if total_pnl_t < 0 else "")
                badge_d = f'<span class="badge badge-{"long" if direction=="LONG" else "short"}">{_html_escape(direction)}</span>'
                
                parts.append(f"<tr><td><b>{_html_escape(ticker)}</b></td><td>{badge_d}</td>")
                parts.append(f"<td>{t}</td><td>{w}</td><td>{l}</td><td>{wr}%</td>")
                parts.append(f'<td class="{pnl_c}">₹{total_pnl_t:+,.0f}</td></tr>')
            parts.append('</table></div>')
    
    # ============================================================
    # SECTION 4: PER-MARKET PERFORMANCE
    # ============================================================
    if os.path.exists(PAPER_FILE):
        df_trades = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        if len(df_trades) > 0 and "Mode" in df_trades.columns:
            parts.append('<h2>🌍 Per-Market Performance</h2>')
            parts.append('<div class="scroll"><table>')
            parts.append("<tr><th>Region</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>Total P&amp;L</th></tr>")
            
            for region, grp in df_trades.groupby("Mode"):
                pnl_vals = []
                for _, r in grp.iterrows():
                    v = r.get("P&L")
                    if pd.notna(v) and str(v).strip() != "":
                        try:
                            pnl_vals.append(float(v))
                        except:
                            pass
                w = sum(1 for v in pnl_vals if v > 0)
                l = sum(1 for v in pnl_vals if v < 0)
                t = len(grp)
                wr = round(w / (w + l) * 100, 1) if (w + l) > 0 else 0
                total_pnl_r = round(sum(pnl_vals), 0)
                pnl_c = "profit" if total_pnl_r > 0 else ("loss" if total_pnl_r < 0 else "")
                
                parts.append(f"<tr><td><b>{_html_escape(region)}</b></td><td>{t}</td><td>{w}</td><td>{l}</td><td>{wr}%</td>")
                parts.append(f'<td class="{pnl_c}">₹{total_pnl_r:+,.0f}</td></tr>')
            parts.append('</table></div>')
    
    # ============================================================
    # SECTION 5: PORTFOLIO HISTORY (from snapshots CSV)
    # ============================================================
    PORTFOLIO_LOG = os.path.join(LOG_DIR, "portfolio_snapshots.csv")
    if os.path.exists(PORTFOLIO_LOG):
        df_snap = pd.read_csv(PORTFOLIO_LOG, on_bad_lines='warn')
        if len(df_snap) > 0:
            parts.append('<h2>📅 Portfolio History</h2>')
            parts.append('<div class="scroll"><table>')
            cols = ["Date", "Time", "Capital", "Return_Pct", "Open", "Win_Rate", "Total_PnL"]
            cols = [c for c in cols if c in df_snap.columns]
            parts.append("<tr>" + "".join(f"<th>{c.replace('_', ' ')}</th>" for c in cols) + "</tr>")
            # Show latest 20 snapshots FIRST (newest at top)
            snap_limit = 20
            total_snaps = len(df_snap)
            parts.append(f'<div class="section-summary">Showing latest {min(snap_limit, total_snaps)} of {total_snaps} snapshot(s) — newest first</div>')
            for _, row in df_snap.iloc[::-1].head(snap_limit).iterrows():
                parts.append("<tr>")
                for c in cols:
                    val = row.get(c, "")
                    if c in ("Capital", "Total_PnL") and pd.notna(val):
                        try:
                            val = f"₹{float(val):,.0f}"
                        except:
                            pass
                    if c == "Return_Pct" and pd.notna(val):
                        cls = "profit" if float(val) > 0 else ("loss" if float(val) < 0 else "")
                        val = f'<span class="{cls}">{_html_escape(val)}%</span>'
                    parts.append(f"<td>{val}</td>")
                parts.append("</tr>")
            parts.append('</table></div>')
    
    # Footer
    parts.append(f'''
  <div class="footer">
    FREE 3-Market v5.0 Paper Trade Bot &bull;
    Generated {_html_escape(date_str)} {_html_escape(time_str)} &bull;
    <a href="https://github.com/thokfoot/free-4-market-master" style="color:#484f58">GitHub</a>
  </div>
</div>
</body>
</html>''')
    
    # Write
    report_file = os.path.join(LOG_DIR, "portfolio_report.html")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(report_file, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    
    report_size = os.path.getsize(report_file)
    print(f"[Report] Portfolio report generated: {report_file} ({report_size:,} bytes)")
    return report_file


def update_trades(ohlc_data: dict) -> list:
    """
    Check all open positions for SL/TP/MaxHold exit using daily OHLC data.
    Checks INTRADAY High/Low FIRST (most realistic), then Close as backup.
    
    Args:
        ohlc_data: {ticker: {"close": c, "high": h, "low": l}}
    
    Returns:
        List of closed trade messages
    """
    if not os.path.exists(PAPER_FILE):
        return []
    
    df = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
    portfolio = load_portfolio()
    updated = False
    closed_msgs = []
    now = datetime.now(IST)
    time_str = now.strftime("%H:%M:%S IST")
    
    # ── Ensure string columns are object dtype (not float64) ──
    str_cols = ["Exit_Price", "Exit_Time", "P&L", "P&L_%", "Status", "Reason", "Date", "Time_IST", "Mode", "Ticker", "Direction", "TimeFrame", "Pattern_Rank", "Expected_WinRate", "Pattern_Factors"]
    for col in str_cols:
        if col in df.columns:
            df[col] = df[col].astype(object)
    
    for idx, row in df.iterrows():
        if row["Status"] != "OPEN":
            continue
        
        ticker = str(row.get("Ticker", ""))
        if ticker not in ohlc_data:
            continue
        
        ohlc = ohlc_data[ticker]
        cmp = ohlc["close"]
        daily_high = ohlc["high"]
        daily_low = ohlc["low"]
        direction = str(row.get("Direction", "LONG"))
        entry = float(row["Entry_Price"])
        sl = float(row["SL"])
        target = float(row["Target"])
        
        exit_price = None
        exit_reason = None
        
        # ── OHLC DATA VALIDATION: Prevent false exits from corrupt data ──
        # If OHLC values are zero, negative, or NaN, DO NOT exit based on them.
        # This guards against transient yfinance data glitches (like partial candles,
        # or auto_adjust=False MultiIndex column issues returning wrong values).
        _invalid_ohlc = (
            daily_low is None or daily_high is None or cmp is None
            or not math.isfinite(daily_low)
            or not math.isfinite(daily_high)
            or not math.isfinite(cmp)
            or daily_low <= 0 or daily_high <= 0 or cmp <= 0
        )
        if _invalid_ohlc:
            print(f"[Paper] WARNING: Invalid OHLC data for {ticker}: "
                  f"close={cmp}, high={daily_high}, low={daily_low} — SKIPPING SL/TP check")
            continue  # Skip this ticker entirely — don't exit based on bad data
        
        # Log OHLC values for debugging (captured in GH Actions logs)
        print(f"[Paper] SL/TP check {direction} {ticker}: "
              f"close={cmp:.2f} high={daily_high:.2f} low={daily_low:.2f} | "
              f"entry={entry:.2f} sl={sl:.2f} target={target:.2f}")
        
        # Check max hold expiry FIRST (time-based exit, independent of SL/TP)
        entry_date = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=IST)
        trade_tf = str(row.get("TimeFrame", "SWING_1d"))
        mh = row.get("MaxHold")
        trade_max_hold = int(mh) if pd.notna(mh) else MAX_HOLD_DAYS
        is_expired = False
        
        if trade_tf == "INTRADAY_1h":
            hours_held = (now - entry_date).total_seconds() / 3600
            if hours_held >= trade_max_hold:
                exit_price = cmp
                exit_reason = f"Expiry {int(hours_held)}h"
                is_expired = True
        else:
            days_held = (now - entry_date).days
            if days_held >= trade_max_hold:
                exit_price = cmp
                exit_reason = f"Expiry {days_held}d"
                is_expired = True
        
        # SL/TP check only if not already triggered by expiry
        # Use a 0.01% tolerance guard to prevent exits triggered by 1-cent data noise
        # (e.g., if daily_low = 733.78 but actual SL is 733.79, that's data noise, not a real SL hit)
        _TOLERANCE = 0.9999  # Require 0.01% below SL / above TP before exiting (guards 1-cent noise)
        
        if not is_expired and direction == "LONG":
            # 1st: Intraday LOW hit SL → stopped out during the day
            if daily_low <= sl * _TOLERANCE:
                exit_price = sl
                exit_reason = "SL Hit (intraday)"
            # 2nd: Intraday HIGH hit TP → target reached during the day
            elif daily_high >= target / _TOLERANCE:
                exit_price = target
                exit_reason = "Target Hit"
            # 3rd: Close <= SL → SL hit on close
            elif cmp <= sl * _TOLERANCE:
                exit_price = sl
                exit_reason = "SL Hit (close)"
            # 4th: Close >= TP → TP hit on close
            elif cmp >= target / _TOLERANCE:
                exit_price = target
                exit_reason = "Target Hit (close)"
        else:  # SHORT
            # 1st: Intraday HIGH hit SL → stopped out during the day
            if daily_high >= sl / _TOLERANCE:
                exit_price = sl
                exit_reason = "SL Hit (intraday)"
            # 2nd: Intraday LOW hit TP → target reached during the day
            elif daily_low <= target * _TOLERANCE:
                exit_price = target
                exit_reason = "Target Hit"
            # 3rd: Close >= SL → SL hit on close
            elif cmp >= sl / _TOLERANCE:
                exit_price = sl
                exit_reason = "SL Hit (close)"
            # 4th: Close <= TP → TP hit on close
            elif cmp <= target * _TOLERANCE:
                exit_price = target
                exit_reason = "Target Hit (close)"
        
        if exit_price:
            # Apply exit slippage for realistic fills (worse than trigger price)
            trade_mode_exit = str(row.get("Mode", "US"))
            trade_tf_exit = str(row.get("TimeFrame", "SWING_1d"))
            exit_price = _apply_slippage(exit_price, direction, "EXIT", trade_mode_exit, trade_tf_exit)
            
            # Gross P&L (before charges)
            if direction == "LONG":
                pnl = (exit_price - entry) * row["Qty"]
                pnl_pct = ((exit_price - entry) / entry) * 100
            else:  # SHORT
                pnl = (entry - exit_price) * row["Qty"]
                pnl_pct = ((entry - exit_price) / entry) * 100
            
            # Deduct trading charges per market (Round Turn cost)
            trade_mode = str(row.get("Mode", "US"))
            # Normalize legacy case (e.g., "Crypto" → "CRYPTO", "India" → "INDIAN")
            mode_norm = trade_mode.upper()
            if mode_norm == "INDIA":
                mode_norm = "INDIAN"
            charge_rate = CHARGES_PER_MARKET.get(mode_norm, 0.001)
            notional = entry * row["Qty"]
            charges = round(notional * charge_rate, 2)
            pnl -= charges
            pnl_pct -= charge_rate * 100
            
            df.at[idx, "Exit_Price"] = round_price(exit_price)
            df.at[idx, "Exit_Time"] = time_str
            df.at[idx, "P&L"] = round(pnl, 2)
            df.at[idx, "P&L_%"] = round(pnl_pct, 2)
            df.at[idx, "Status"] = "CLOSED"
            # Reason updated via full_reason variable (used in audit + stats)
            
            full_reason = str(row["Reason"]) + f" | {exit_reason}"
            df.at[idx, "Reason"] = full_reason
            
            # Log exit to persistent audit trail (AFTER values are set)
            _log_audit_exit({
                "Exit_Time": time_str,
                "Mode": row.get("Mode", ""),
                "Ticker": row["Ticker"],
                "Direction": row["Direction"],
                "Entry_Price": entry,
                "Exit_Price": round_price(exit_price),
                "Qty": row["Qty"],
                "P&L": round(pnl, 2),
                "P&L_%": round(pnl_pct, 2),
                "Pattern_Rank": row.get("Pattern_Rank", ""),
                "Expected_WinRate": row.get("Expected_WinRate", ""),
                "Pattern_Factors": row.get("Pattern_Factors", ""),
                "Reason": full_reason,
            })
            
            # Update per-strategy win rate EXACTLY ONCE at exit (not at bottom of function)
            update_strategy_stats(full_reason, round(pnl, 2))
            
            # ── Send real-time Telegram alert with OHLC telemetry ──
            _send_sl_tp_alert(
                ticker=ticker,
                direction=direction,
                exit_reason=exit_reason,
                entry=entry,
                exit_price=exit_price,
                sl=sl,
                target=target,
                cmp=cmp,
                daily_high=daily_high,
                daily_low=daily_low,
                pnl=round(pnl, 2),
                pnl_pct=round(pnl_pct, 2),
                qty=int(row["Qty"]),
                rank_str=str(row.get("Pattern_Rank", "")),
            )
            
            # Update per-market capital (respect TimeFrame for intraday separate capital)
            trade_tf = str(row.get("TimeFrame", "SWING_1d"))
            if trade_tf == "INTRADAY_1h":
                capital_key = "INTRADAY"
            else:
                capital_key = str(row.get("Mode", "US"))
                # Normalize legacy case (e.g., "Crypto" → "CRYPTO", "India" → "INDIAN")
                ck_upper = capital_key.upper()
                capital_key = "INDIAN" if ck_upper == "INDIA" else ck_upper
            mkt_cap = portfolio.setdefault("capital_by_market", {}).get(capital_key, 100000)
            mkt_cap = max(0, mkt_cap + pnl)
            portfolio["capital_by_market"][capital_key] = mkt_cap
            portfolio["total_pnl"] += pnl
            tpnl_by_mkt = portfolio.setdefault("total_pnl_by_market", {"INDIAN":0,"US":0,"CRYPTO":0,"INTRADAY":0})
            if capital_key in tpnl_by_mkt:
                tpnl_by_mkt[capital_key] += pnl
            else:
                tpnl_by_mkt[capital_key] = pnl
            portfolio["closed_count"] += 1
            if pnl > 0:
                portfolio["total_wins"] += 1
            else:
                portfolio["total_losses"] += 1
            
            closed_msgs.append(
                f"{direction} {ticker} {exit_reason} "
                f"P&L Rs {pnl:+.0f} ({pnl_pct:+.1f}%)"
            )
            updated = True
            print(f"[Paper] EXIT {direction} {ticker} @ {round_price(exit_price)} | "
                  f"P&L {pnl:+.0f} | {exit_reason}")
    
    if updated:
        df.to_csv(PAPER_FILE, index=False)
        open_df = df[df["Status"] == "OPEN"]
        portfolio["open_positions"] = open_df.to_dict(orient="records")
        # ── Recalculate total_capital from actual capital_by_market ──
        # This field was historically never updated after trade closure.
        portfolio["total_capital"] = sum(portfolio.get("capital_by_market", {}).values())
        save_portfolio(portfolio)
        # NOTE: strategy_stats is updated INSIDE the exit loop above (ONCE per trade)
        # Do NOT re-iterate ALL closed rows here — that would double-count!
    
    return closed_msgs

"""
FREE 3-Market v5.12 — PROFESSIONAL LOGGER
==========================================
Finance Manager Grade Logging System
- Daily scan logs (JSON per day)
- Trade audit log (CSV)
- Portfolio snapshots
- Error tracking
"""

import os, json, sys, traceback, tempfile, shutil
from datetime import datetime
import pandas as pd
import pytz
from config import CAPITAL, CAPITAL_BY_MARKET, TOTAL_CAPITAL

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

ERROR_LOG = os.path.join(LOG_DIR, "errors.log")
TRADE_LOG = os.path.join(LOG_DIR, "trade_log.csv")
PORTFOLIO_LOG = os.path.join(LOG_DIR, "portfolio_snapshots.csv")


def now_ist() -> datetime:
    return datetime.now(IST)


def _atomic_write(filepath: str, data):
    """Write JSON to a temp file first, then atomically rename to target.
    Prevents corruption from concurrent writes."""
    tmp = filepath + ".tmp." + str(os.getpid())
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, default=str)
        shutil.move(tmp, filepath)
    except Exception as e:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except:
                pass
        raise


def _safe_read_json(filepath: str):
    """Read JSON, returning None if corrupted or missing."""
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, Exception):
        return None


def log_scan(scan_data: dict):
    """
    Log daily scan results to JSON file (atomic write).
    File: logs/daily_scan_YYYY-MM-DD.json
    
    Uses atomic write to prevent corruption:
      1. Read existing file (fail-safe: returns None if corrupted)
      2. Merge new run into existing runs
      3. Write to temp file, then rename to target
    
    scan_data contains:
      - date, time
      - tickers_scanned: list of {ticker, close, region}
      - patterns_checked: list of {market, ticker, factors, direction, fired, win_rate}
      - entries: list of entered trades
      - portfolio: {capital, open_count, total_pnl}
      - telegram_status
    """
    date_str = scan_data.get("date", now_ist().strftime("%Y-%m-%d"))
    mode_str = str(scan_data.get("mode", "")).upper() or "SCAN"
    # Mode-specific file (daily_scan_{MODE}_{date}.json) so concurrent
    # workflows (BOTH/FADE/GAPDOWN) never write the same JSON - prevents
    # git push conflicts between scheduled workflows.
    scan_file = os.path.join(LOG_DIR, f"daily_scan_{mode_str}_{date_str}.json")
    
    # Try to load existing — fail gracefully if corrupted
    existing = _safe_read_json(scan_file) or {}
    if not existing:
        existing = {"date": date_str, "runs": []}
    
    runs = existing.get("runs", [])
    runs.append({
        "time": scan_data.get("time", now_ist().strftime("%H:%M:%S IST")),
        "mode": scan_data.get("mode", ""),
        "tickers_scanned": scan_data.get("tickers_scanned", []),
        "market_close": scan_data.get("market_close", {}),
        "patterns_count": scan_data.get("patterns_checked", 0),
        "patterns_fired": scan_data.get("patterns_fired", 0),
        "fired_patterns": scan_data.get("fired_patterns", []),
        "entries": scan_data.get("entries", []),
        "skipped_entries": scan_data.get("skipped_entries", []),
        "portfolio": scan_data.get("portfolio", {}),
        "telegram_status": scan_data.get("telegram_status", ""),
        "duration_sec": scan_data.get("duration_sec"),
    })
    
    existing["date"] = date_str
    existing["runs"] = runs
    existing["latest_portfolio"] = scan_data.get("portfolio", {})
    
    # Atomic write to prevent corruption
    try:
        _atomic_write(scan_file, existing)
    except Exception as e:
        print(f"[Logger] Atomic write failed, falling back to direct write: {e}")
        with open(scan_file, "w", encoding="utf-8") as f:
            json.dump(existing, f, indent=2, default=str)
    
    fired = scan_data.get("patterns_fired", 0)
    print(f"[Logger] Daily scan logged: {len(scan_data.get('tickers_scanned', []))} tickers, "
          f"{fired} signals fired")


def log_trade_run(row_dict: dict):
    """
    Log a bot run to trade_log.csv (append).
    One row per run with summary stats.
    
    Uses explicit dtype handling to prevent "Invalid value for dtype float64" errors.
    """
    try:
        df_new = pd.DataFrame([row_dict])
        # ── Ensure numeric columns are float64, string columns are object ──
        numeric_cols = ["Tickers_Scanned", "Errors", "Patterns_Total", "Patterns_Fired",
                        "New_Entries", "Closed_Trades", "Open_Positions", "Capital", "Total_PnL"]
        for col in numeric_cols:
            if col in df_new.columns:
                df_new[col] = pd.to_numeric(df_new[col], errors='coerce').fillna(0).astype(float)
        
        if os.path.exists(TRADE_LOG):
            df_old = pd.read_csv(TRADE_LOG, on_bad_lines='warn')
            # Convert old numeric columns too
            for col in numeric_cols:
                if col in df_old.columns:
                    df_old[col] = pd.to_numeric(df_old[col], errors='coerce').fillna(0).astype(float)
            # Convert old string columns to object
            str_cols = ["Date", "Time", "Mode", "Telegram"]
            for col in str_cols:
                if col in df_old.columns:
                    df_old[col] = df_old[col].astype(object)
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df_combined = df_new
        df_combined.to_csv(TRADE_LOG, index=False)
        print(f"[Logger] Trade log updated — {len(df_combined)} rows")
    except Exception as e:
        log_error(f"Trade log failed: {e}")


def log_portfolio(capital: float, open_positions: list, closed_count: int,
                  total_wins: int, total_losses: int, total_pnl: float,
                  capital_by_market: dict = None):
    """Log portfolio snapshot with per-market breakdown."""
    try:
        now = now_ist()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S IST")
        total_closed = total_wins + total_losses
        win_rate = round(total_wins / total_closed * 100, 1) if total_closed > 0 else 0
        ret_pct = round((capital - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100, 2) if TOTAL_CAPITAL > 0 else 0
        
        row = {
            "Date": date_str, "Time": time_str,
            "Capital": round(capital, 0), "Initial": TOTAL_CAPITAL,
            "Return_Pct": ret_pct, "Open": len(open_positions),
            "Closed": closed_count, "Wins": total_wins,
            "Losses": total_losses, "Win_Rate": win_rate,
            "Total_PnL": round(total_pnl, 0),
        }
        # Add per-market columns
        if capital_by_market:
            for mkt in ["INDIAN", "US", "CRYPTO", "INTRADAY", "FADE"]:
                row[f"Cap_{mkt}"] = round(capital_by_market.get(mkt, 100000), 0)
        
        df_new = pd.DataFrame([row])
        if os.path.exists(PORTFOLIO_LOG):
            df_old = pd.read_csv(PORTFOLIO_LOG, on_bad_lines='warn')
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df_combined = df_new
        df_combined.to_csv(PORTFOLIO_LOG, index=False)
        
        mkt_str = ""
        if capital_by_market:
            mkt_parts = [f"{m}:₹{capital_by_market.get(m,0):,.0f}" for m in ["INDIAN","US","CRYPTO","INTRADAY","FADE"]]
            mkt_str = " | " + " ".join(mkt_parts)
        print(f"[Logger] Portfolio: Rs {capital:,.0f} | Return {ret_pct:+.2f}%{mkt_str}")
    except Exception as e:
        log_error(f"Portfolio log failed: {e}")


def log_error(message: str):
    """Log error with stack trace."""
    try:
        timestamp = now_ist().strftime("%Y-%m-%d %H:%M:%S IST")
        stack = traceback.format_exc()
        with open(ERROR_LOG, "a") as f:
            f.write(f"\n{'='*60}\n[{timestamp}] ERROR\n{'='*60}\n{message}\n")
            if stack and "NoneType: None" not in stack:
                f.write(f"Stack:\n{stack}\n")
        print(f"[Logger] Error: {message[:120]}")
    except:
        pass

"""
FREE 4-Market v5.0 — PROFESSIONAL LOGGER
==========================================
Finance Manager Grade Logging System
- Daily scan logs (JSON per day)
- Trade audit log (CSV)
- Portfolio snapshots
- Error tracking
"""

import os, json, sys, traceback
from datetime import datetime
import pandas as pd
import pytz
from config import CAPITAL

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

ERROR_LOG = os.path.join(LOG_DIR, "errors.log")
TRADE_LOG = os.path.join(LOG_DIR, "trade_log.csv")
PORTFOLIO_LOG = os.path.join(LOG_DIR, "portfolio_snapshots.csv")


def now_ist() -> datetime:
    return datetime.now(IST)


def log_scan(scan_data: dict):
    """
    Log daily scan results to JSON file.
    File: logs/daily_scan_YYYY-MM-DD.json
    
    scan_data contains:
      - date, time
      - tickers_scanned: list of {ticker, close, region}
      - patterns_checked: list of {market, ticker, factors, direction, fired, win_rate}
      - entries: list of entered trades
      - portfolio: {capital, open_count, total_pnl}
      - telegram_status
    """
    date_str = scan_data.get("date", now_ist().strftime("%Y-%m-%d"))
    scan_file = os.path.join(LOG_DIR, f"daily_scan_{date_str}.json")
    
    # Load existing if any (in case of multiple runs same day)
    existing = {}
    if os.path.exists(scan_file):
        try:
            with open(scan_file, "r") as f:
                existing = json.load(f)
        except:
            pass
    
    # Convert runs to list format
    runs = existing.get("runs", [])
    runs.append({
        "time": scan_data.get("time", now_ist().strftime("%H:%M:%S IST")),
        "tickers_scanned": scan_data.get("tickers_scanned", []),
        "patterns_checked": scan_data.get("patterns_checked", []),
        "entries": scan_data.get("entries", []),
        "portfolio": scan_data.get("portfolio", {}),
        "telegram_status": scan_data.get("telegram_status", ""),
    })
    
    # Also store latest state
    existing["date"] = date_str
    existing["runs"] = runs
    existing["latest_portfolio"] = scan_data.get("portfolio", {})
    
    with open(scan_file, "w") as f:
        json.dump(existing, f, indent=2, default=str)
    
    print(f"[Logger] Daily scan logged: {len(scan_data.get('tickers_scanned', []))} tickers, "
          f"{sum(1 for p in scan_data.get('patterns_checked', []) if p.get('fired'))} signals fired")


def log_trade_run(row_dict: dict):
    """
    Log a bot run to trade_log.csv (append).
    One row per run with summary stats.
    """
    try:
        df_new = pd.DataFrame([row_dict])
        if os.path.exists(TRADE_LOG):
            df_old = pd.read_csv(TRADE_LOG)
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df_combined = df_new
        df_combined.to_csv(TRADE_LOG, index=False)
        print(f"[Logger] Trade log updated — {len(df_combined)} rows")
    except Exception as e:
        log_error(f"Trade log failed: {e}")


def log_portfolio(capital: float, open_positions: list, closed_count: int,
                  total_wins: int, total_losses: int, total_pnl: float):
    """Log portfolio snapshot."""
    try:
        now = now_ist()
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H:%M:%S IST")
        total_closed = total_wins + total_losses
        win_rate = round(total_wins / total_closed * 100, 1) if total_closed > 0 else 0
        ret_pct = round((capital - CAPITAL) / CAPITAL * 100, 2)
        
        row = {
            "Date": date_str, "Time": time_str,
            "Capital": round(capital, 0), "Initial": CAPITAL,
            "Return_Pct": ret_pct, "Open": len(open_positions),
            "Closed": closed_count, "Wins": total_wins,
            "Losses": total_losses, "Win_Rate": win_rate,
            "Total_PnL": round(total_pnl, 0),
        }
        df_new = pd.DataFrame([row])
        if os.path.exists(PORTFOLIO_LOG):
            df_old = pd.read_csv(PORTFOLIO_LOG)
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df_combined = df_new
        df_combined.to_csv(PORTFOLIO_LOG, index=False)
        print(f"[Logger] Portfolio: Rs {capital:,.0f} | Return {ret_pct:+.2f}% | "
              f"Open {len(open_positions)} | WinRate {win_rate}%")
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

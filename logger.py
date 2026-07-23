"""
FREE 4-Market v4.1 — PROFESSIONAL LOGGER
==========================================
Finance Manager Grade Logging System
- Trade logging with full audit trail
- Daily PnL tracking
- Portfolio snapshot reports
- Excel reports with summary sheets
- Error logging with stack traces

Every trade, every decision, every PnL is logged and timestamped.
"""

import os, sys, traceback
from datetime import datetime
import pandas as pd

import pytz
from config import *

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

# ===== FILE PATHS =====
TRADE_LOG_CSV = os.path.join(LOG_DIR, "trade_log.csv")
TRADE_LOG_XLSX = os.path.join(LOG_DIR, "trade_log.xlsx")
PORTFOLIO_LOG_CSV = os.path.join(LOG_DIR, "portfolio_log.csv")
ERROR_LOG = os.path.join(LOG_DIR, "errors.log")

# ===== COLUMNS =====
TRADE_COLS = [
    "Date", "Time_IST", "Mode",
    "Gems_Found", "New_Entries", "Closed_Trades", "Open_Positions",
    "Capital", "Total_PnL",
    "Telegram_Status", "Weekday",
]


def log_trade(row_dict: dict):
    """
    Log a bot run to CSV and Excel.
    
    This is the primary audit log. Every bot run creates one row
    recording: scan results, entries, exits, capital, PnL.
    
    Args:
        row_dict: Dictionary with keys matching TRADE_COLS
    """
    try:
        os.makedirs(LOG_DIR, exist_ok=True)
        df_new = pd.DataFrame([row_dict])

        # Ensure all columns exist
        for col in TRADE_COLS:
            if col not in df_new.columns:
                df_new[col] = ""

        df_new = df_new[TRADE_COLS]

        if os.path.exists(TRADE_LOG_CSV):
            df_old = pd.read_csv(TRADE_LOG_CSV)
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df_combined = df_new

        df_combined.to_csv(TRADE_LOG_CSV, index=False)
        print(f"[Logger] Trade log updated — {len(df_combined)} rows")

        # Update Excel
        _update_excel(df_combined)

    except Exception as e:
        log_error(f"log_trade failed: {e}")


def _update_excel(df_combined: pd.DataFrame):
    """Update the Excel report with summary sheets."""
    try:
        if df_combined.empty:
            return

        # Build daily summary
        summary = df_combined.groupby("Date").agg({
            "Gems_Found": "sum",
            "New_Entries": "sum",
            "Closed_Trades": "sum",
            "Open_Positions": "last",
            "Capital": "last",
            "Total_PnL": "last",
        }).reset_index()

        # Calculate daily PnL change
        if "Capital" in summary.columns and len(summary) > 1:
            summary["Capital_Change"] = summary["Capital"].diff().fillna(0)
            summary["Cumulative_Return"] = ((summary["Capital"] - INITIAL_CAPITAL)
                                           / INITIAL_CAPITAL * 100).round(2)

        with pd.ExcelWriter(TRADE_LOG_XLSX, engine="openpyxl") as writer:
            df_combined.to_excel(writer, sheet_name="All Runs", index=False)
            summary.to_excel(writer, sheet_name="Daily Summary", index=False)

        print(f"[Logger] Excel report updated")

    except Exception as e:
        print(f"[Logger] Excel update failed: {e}")
        log_error(f"Excel update failed: {e}")


def log_portfolio_summary(portfolio: dict, date_str: str, time_str: str):
    """
    Log portfolio snapshot to CSV.
    
    Records: date, capital, open positions, PnL, win rate, etc.
    """
    try:
        row = {
            "Date": date_str,
            "Time": time_str,
            "Capital": round(portfolio.get("capital", 0), 0),
            "Initial_Capital": round(portfolio.get("initial_capital", INITIAL_CAPITAL), 0),
            "Total_PnL": round(portfolio.get("total_pnl", 0), 0),
            "Open_Positions": len(portfolio.get("open_positions", [])),
            "Closed_Trades": portfolio.get("closed_count", 0),
            "Wins": portfolio.get("total_wins", 0),
            "Losses": portfolio.get("total_losses", 0),
        }

        # Calculate return %
        init_cap = row["Initial_Capital"]
        row["Return_Pct"] = round((row["Capital"] - init_cap) / init_cap * 100, 2) if init_cap > 0 else 0

        # Win rate
        total_closed = row["Wins"] + row["Losses"]
        row["Win_Rate_Pct"] = round(row["Wins"] / total_closed * 100, 1) if total_closed > 0 else 0

        df_new = pd.DataFrame([row])
        cols = ["Date","Time","Capital","Initial_Capital","Total_PnL","Return_Pct",
                "Open_Positions","Closed_Trades","Wins","Losses","Win_Rate_Pct"]

        if os.path.exists(PORTFOLIO_LOG_CSV):
            df_old = pd.read_csv(PORTFOLIO_LOG_CSV)
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
        else:
            df_combined = df_new

        df_combined[cols].to_csv(PORTFOLIO_LOG_CSV, index=False)
        print(f"[Logger] Portfolio snapshot logged — Rs {row['Capital']:,.0f} | "
              f"PnL Rs {row['Total_PnL']:+,.0f} ({row['Return_Pct']:+.2f}%)")

    except Exception as e:
        log_error(f"log_portfolio_summary failed: {e}")


def log_error(message: str):
    """
    Log an error with full stack trace.
    
    Errors are logged to errors.log with timestamp.
    """
    try:
        timestamp = datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")
        stack = traceback.format_exc()
        with open(ERROR_LOG, "a") as f:
            f.write(f"\n{'='*60}\n")
            f.write(f"[{timestamp}] ERROR\n")
            f.write(f"{'='*60}\n")
            f.write(f"{message}\n")
            if stack and stack != "NoneType: None\n":
                f.write(f"Stack Trace:\n{stack}\n")
        print(f"[Logger] Error logged: {message[:100]}")
    except:
        print(f"[Logger] FAILED to log error: {message[:100]}")

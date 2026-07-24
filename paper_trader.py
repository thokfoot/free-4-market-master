"""
FREE 4-Market v5.0 — PAPER TRADER
===================================
Portfolio & trade management for paper trading.
Supports LONG and SHORT positions with SL/TP and max hold.
"""

import os, json, pandas as pd
from datetime import datetime
import pytz
from config import CAPITAL, RISK_PER_TRADE, SL_PCT, TP_PCT, MAX_HOLD_DAYS, MAX_CONCURRENT

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

PAPER_FILE = os.path.join(LOG_DIR, "paper_trades.csv")
PORTFOLIO_FILE = os.path.join(LOG_DIR, "portfolio.json")
COLUMNS = [
    "Date","Time_IST","Mode","Ticker","Direction",
    "Entry_Price","Qty","SL","Target","MaxHold",
    "Exit_Price","Exit_Time","P&L","P&L_%","Status","Reason"
]


def round_price(price):
    """Round price appropriately based on magnitude."""
    p = float(price)
    if p >= 1000: return round(p, 2)
    if p >= 100: return round(p, 2)
    if p >= 1: return round(p, 2)
    if p >= 0.1: return round(p, 4)
    if p >= 0.01: return round(p, 6)
    return round(p, 8)


def load_portfolio() -> dict:
    """Load portfolio from JSON file."""
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {
        "capital": CAPITAL,
        "open_positions": [],
        "closed_count": 0,
        "total_wins": 0,
        "total_losses": 0,
        "total_pnl": 0,
    }


def save_portfolio(port: dict):
    """Save portfolio to JSON."""
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(port, f, indent=2)


def calculate_qty(entry: float, sl: float) -> int:
    """Calculate position size based on risk per trade (1% of capital)."""
    risk_amt = load_portfolio()["capital"] * RISK_PER_TRADE
    risk_per_share = abs(entry - sl)
    if risk_per_share < 1e-9:
        return 0
    qty = int(risk_amt / risk_per_share)
    # Caps based on price
    if entry < 0.1:
        qty = min(qty, 50000)
    elif entry < 1:
        qty = min(qty, 10000)
    elif entry > 100:
        qty = min(qty, 5000)
    return max(1, qty)


def enter_trade(mode: str, ticker: str, direction: str, entry_price: float,
                 reason: str, pattern_rank: int = None) -> dict:
    """
    Enter a paper trade.
    
    Args:
        mode: "INDIAN" | "US" | "CRYPTO"
        ticker: yfinance ticker
        direction: "LONG" | "SHORT"
        entry_price: Entry price
        reason: Signal description
        pattern_rank: Rank from CSV (optional)
    
    Returns:
        Trade dict or None if rejected
    """
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    portfolio = load_portfolio()
    
    # Check max concurrent
    if len(portfolio["open_positions"]) >= MAX_CONCURRENT:
        print(f"[Paper] MAX CONCURRENT ({MAX_CONCURRENT}), skip {ticker}")
        return None
    
    # Check duplicate (same ticker, same direction, open)
    for pos in portfolio["open_positions"]:
        if pos["Ticker"] == ticker and pos["Direction"] == direction:
            print(f"[Paper] Duplicate {ticker} {direction} already open, skip")
            return None
    
    # Calculate SL/TP based on direction
    if direction == "LONG":
        sl = round_price(entry_price * (1 - SL_PCT))
        target = round_price(entry_price * (1 + TP_PCT))
    else:  # SHORT
        sl = round_price(entry_price * (1 + SL_PCT))
        target = round_price(entry_price * (1 - TP_PCT))
    
    entry = round_price(entry_price)
    qty = calculate_qty(entry, sl)
    if qty == 0:
        return None
    
    # Build reason with pattern rank
    full_reason = reason
    if pattern_rank:
        full_reason = f"#{pattern_rank} {reason}"
    
    trade = {
        "Date": date_str,
        "Time_IST": time_str,
        "Mode": mode,
        "Ticker": ticker,
        "Direction": direction,
        "Entry_Price": entry,
        "Qty": qty,
        "SL": sl,
        "Target": target,
        "MaxHold": MAX_HOLD_DAYS,
        "Exit_Price": "",
        "Exit_Time": "",
        "P&L": "",
        "P&L_%": "",
        "Status": "OPEN",
        "Reason": full_reason,
    }
    
    # Append to CSV
    df_new = pd.DataFrame([trade])[COLUMNS]
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(PAPER_FILE):
        df_old = pd.read_csv(PAPER_FILE)
        df_comb = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_comb = df_new
    df_comb.to_csv(PAPER_FILE, index=False)
    
    # Update portfolio
    portfolio["open_positions"].append(trade)
    save_portfolio(portfolio)
    
    print(f"[Paper] ENTER {direction} {ticker} @ {entry} Qty {qty} SL {sl} TGT {target}")
    return trade


def update_trades(current_prices: dict) -> list:
    """
    Check all open positions for SL/TP/MaxHold exit.
    
    Args:
        current_prices: {ticker: current_price}
    
    Returns:
        List of closed trade messages
    """
    if not os.path.exists(PAPER_FILE):
        return []
    
    df = pd.read_csv(PAPER_FILE)
    portfolio = load_portfolio()
    updated = False
    closed_msgs = []
    now = datetime.now(IST)
    time_str = now.strftime("%H:%M:%S IST")
    
    for idx, row in df.iterrows():
        if row["Status"] != "OPEN":
            continue
        
        ticker = row["Ticker"]
        if ticker not in current_prices:
            continue
        
        cmp = current_prices[ticker]
        direction = str(row.get("Direction", "LONG"))
        entry = float(row["Entry_Price"])
        sl = float(row["SL"])
        target = float(row["Target"])
        
        exit_price = None
        exit_reason = None
        
        # Check max hold expiry
        entry_date = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=IST)
        days_held = (now - entry_date).days
        mh = row.get("MaxHold")
        trade_max_hold = int(mh) if pd.notna(mh) else MAX_HOLD_DAYS
        
        if days_held >= trade_max_hold:
            exit_price = cmp
            exit_reason = f"Expiry {days_held}d"
        elif direction == "LONG":
            if cmp <= sl:
                exit_price = sl
                exit_reason = "SL Hit"
            elif cmp >= target:
                exit_price = target
                exit_reason = "Target Hit"
        else:  # SHORT
            if cmp >= sl:
                exit_price = sl
                exit_reason = "SL Hit"
            elif cmp <= target:
                exit_price = target
                exit_reason = "Target Hit"
        
        if exit_price:
            if direction == "LONG":
                pnl = (exit_price - entry) * row["Qty"]
                pnl_pct = ((exit_price - entry) / entry) * 100
            else:  # SHORT
                pnl = (entry - exit_price) * row["Qty"]
                pnl_pct = ((entry - exit_price) / entry) * 100
            
            df.at[idx, "Exit_Price"] = round_price(exit_price)
            df.at[idx, "Exit_Time"] = time_str
            df.at[idx, "P&L"] = round(pnl, 2)
            df.at[idx, "P&L_%"] = round(pnl_pct, 2)
            df.at[idx, "Status"] = "CLOSED"
            df.at[idx, "Reason"] = str(row["Reason"]) + f" | {exit_reason}"
            
            portfolio["capital"] = max(0, portfolio["capital"] + pnl)
            portfolio["total_pnl"] += pnl
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
        save_portfolio(portfolio)
    
    return closed_msgs

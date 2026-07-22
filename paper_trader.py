
import os, json, pandas as pd
from datetime import datetime
import pytz

IST = pytz.timezone("Asia/Kolkata")
PAPER_FILE = "logs/paper_trades.csv"
PORTFOLIO_FILE = "logs/portfolio.json"
CAPITAL = 100000
RISK_PER_TRADE = 0.01

COLUMNS = ["Date","Time_IST","Mode","Ticker","Entry_Price","Qty","SL","Target","Exit_Price","Exit_Time","P&L","P&L_%","Status","Reason"]

def load_portfolio():
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, 'r') as f:
                return json.load(f)
        except:
            return {"capital": CAPITAL, "open_positions": []}
    return {"capital": CAPITAL, "open_positions": []}

def save_portfolio(port):
    os.makedirs("logs", exist_ok=True)
    with open(PORTFOLIO_FILE, 'w') as f:
        json.dump(port, f, indent=2)

def calculate_qty(entry, sl, capital):
    risk_amt = capital * RISK_PER_TRADE
    risk_per_share = abs(entry - sl)
    if risk_per_share == 0: return 0
    qty = int(risk_amt / risk_per_share)
    return max(1, qty)

def enter_paper_trade(mode, ticker, entry_price, reason="Vol Breakout"):
    os.makedirs("logs", exist_ok=True)
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    portfolio = load_portfolio()
    for pos in portfolio["open_positions"]:
        if pos["Ticker"] == ticker and pos["Status"] == "OPEN":
            print(f"[Paper] Duplicate blocked {ticker}"); return None
    sl = round(entry_price * 0.98, 2)
    target = round(entry_price * 1.04, 2)
    qty = calculate_qty(entry_price, sl, portfolio["capital"])
    if qty == 0: return None
    trade = {
        "Date": date_str, "Time_IST": time_str, "Mode": mode, "Ticker": ticker,
        "Entry_Price": entry_price, "Qty": qty, "SL": sl, "Target": target,
        "Exit_Price": "", "Exit_Time": "", "P&L": "", "P&L_%": "", "Status": "OPEN", "Reason": reason
    }
    df_new = pd.DataFrame([trade])[COLUMNS]
    if os.path.exists(PAPER_FILE):
        df_old = pd.read_csv(PAPER_FILE)
        df_comb = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_comb = df_new
    df_comb.to_csv(PAPER_FILE, index=False)
    portfolio["open_positions"].append(trade)
    save_portfolio(portfolio)
    print(f"[Paper] ENTER {ticker} @ {entry_price} Qty {qty} SL {sl} TGT {target}")
    return trade

def update_paper_trades(current_prices_dict):
    if not os.path.exists(PAPER_FILE): return []
    df = pd.read_csv(PAPER_FILE)
    portfolio = load_portfolio()
    updated = False
    closed_msgs = []
    now = datetime.now(IST)
    time_str = now.strftime("%H:%M:%S IST")
    for idx, row in df.iterrows():
        if row["Status"] != "OPEN": continue
        ticker = row["Ticker"]
        if ticker not in current_prices_dict: continue
        cmp = current_prices_dict[ticker]
        exit_price = None; exit_reason = None
        if cmp <= row["SL"]:
            exit_price = row["SL"]; exit_reason = "SL Hit"
        elif cmp >= row["Target"]:
            exit_price = row["Target"]; exit_reason = "Target Hit"
        if exit_price:
            pnl = (exit_price - row["Entry_Price"]) * row["Qty"]
            pnl_pct = ((exit_price - row["Entry_Price"]) / row["Entry_Price"]) * 100
            df.at[idx, "Exit_Price"] = exit_price
            df.at[idx, "Exit_Time"] = time_str
            df.at[idx, "P&L"] = round(pnl, 2)
            df.at[idx, "P&L_%"] = round(pnl_pct, 2)
            df.at[idx, "Status"] = "CLOSED"
            df.at[idx, "Reason"] = str(row["Reason"]) + f" | {exit_reason}"
            portfolio["capital"] += pnl
            closed_msgs.append(f"{ticker} {exit_reason} P&L {pnl:.2f} ({pnl_pct:.1f}%)")
            updated = True
            print(f"[Paper] EXIT {ticker} @ {exit_price} P&L {pnl}")
    if updated:
        df.to_csv(PAPER_FILE, index=False)
        open_df = df[df["Status"]=="OPEN"]
        portfolio["open_positions"] = open_df.to_dict(orient="records")
        save_portfolio(portfolio)
    return closed_msgs

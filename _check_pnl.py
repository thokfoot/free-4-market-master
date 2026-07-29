import yfinance as yf
import pandas as pd
from datetime import datetime
import pytz

ist = pytz.timezone("Asia/Kolkata")
now = datetime.now(ist)
line = "=" * 85

print(line)
print(f"  LIVE P&L CHECK \u2014 {now.strftime('%Y-%m-%d %H:%M:%S %A IST')}")
print(line)

positions = [
    {"ticker": "QQQ",   "entry": 676.32,  "qty": 73,  "sl": 662.79,  "target": 703.37,  "tf": "SWING",    "rank": 46, "mode": "US"},
    {"ticker": "IWM",   "entry": 293.09,  "qty": 337, "sl": 290.16,  "target": 298.95,  "tf": "INTRADAY", "rank": 30, "mode": "US"},
    {"ticker": "^GSPC", "entry": 7435.34, "qty": 13,  "sl": 7360.99, "target": 7584.05, "tf": "INTRADAY", "rank": 16, "mode": "US"},
    {"ticker": "SPY",   "entry": 741.20,  "qty": 133, "sl": 733.79,  "target": 756.02,  "tf": "INTRADAY", "rank": 32, "mode": "US"},
]

total_pnl = 0

for p in positions:
    ticker = p["ticker"]
    entry = p["entry"]
    qty = p["qty"]
    sl = p["sl"]
    target = p["target"]
    tf = p["tf"]
    rank = p["rank"]

    try:
        df = yf.download(ticker, period="3d", interval="1d", progress=False, auto_adjust=True)
        if df.empty or len(df) == 0:
            print(f"\n  {ticker:6s} | \u274c NO DATA")
            continue

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        last = df.iloc[-1]
        cmp = float(last["Close"])
        high = float(last["High"])
        low = float(last["Low"])

        pnl = round((cmp - entry) * qty, 2)
        pnl_pct = round(((cmp - entry) / entry) * 100, 2)
        sl_dist_pct = round(((entry - sl) / entry) * 100, 2)
        tp_dist_pct = round(((target - entry) / entry) * 100, 2)

        if pnl > 0:
            icon = "\U0001f7e2"
        elif pnl < 0:
            icon = "\U0001f534"
        else:
            icon = "\u26ab"

        sl_hit = "SL HIT" if low <= sl else "No"
        tp_hit = "TP HIT" if high >= target else "No"

        print(f"\n  {icon} {ticker:6s} #{rank}{tf:8s} @ {cmp:<8.2f}")
        print(f"  \u2503 Entry: {entry:<8.2f} | P&L: Rs {pnl:+,.0f} ({pnl_pct:+.2f}%)")
        print(f"  \u2503 SL: {sl:<8.2f} ({sl_dist_pct:+.2f}%) | TP: {target:<8.2f} ({tp_dist_pct:+.2f}%)")
        print(f"  \u2503 Low: {low:<8.2f} | SL {sl_hit}")
        print(f"  \u2503 High: {high:<8.2f} | TP {tp_hit}")

        total_pnl += pnl

    except Exception as e:
        print(f"\n  {ticker:6s} | \u274c ERROR: {e}")

print(f"\n{line}")
print(f"  TOTAL UNREALIZED P&L: Rs {total_pnl:+,.0f}")
print(line)

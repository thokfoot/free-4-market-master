"""v5.27 ledger cleanup - archive rows of REMOVED strategies.

Moves index-strategy fills (^ tickers) and the 9 consistent-loser strategy
rows out of the active ledger into logs/archive/ (history preserved), closes
their OPEN positions at the latest close price, rebuilds portfolio.json.
Run: python retire_cleanup.py
"""
import os, sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from datetime import datetime

from logger import now_ist
import market_data
from paper_trader import load_portfolio, save_portfolio

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "logs", "paper_trades.csv")
ARCHIVE_DIR = os.path.join(HERE, "logs", "archive")
COMBO_TFS = {"SWING_1d", "INTRADAY_1h"}

LOSER_KEYS = {
    ("ADA-USD", 41, "SWING_1d"),
    ("TRX-USD", 1, "INTRADAY_1h"),
    ("BTC-USD", 1, "INTRADAY_1h"),
    ("XLC", 5, "SWING_1d"),
    ("XLI", 4, "SWING_1d"),
    ("XLK", 75, "SWING_1d"),
    ("AVAX-USD", 7, "SWING_1d"),
    ("LINK-USD", 2, "INTRADAY_1h"),
}


def is_removed(row):
    tf = str(row.get("TimeFrame", ""))
    if tf not in COMBO_TFS:
        return False
    try:
        rank = int(float(row.get("Pattern_Rank", 0)))
    except (TypeError, ValueError):
        return False
    if rank >= 900:
        return False
    tkr = str(row.get("Ticker", ""))
    if tkr.startswith("^"):
        return True
    return (tkr, rank, tf) in LOSER_KEYS


def main():
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    df = pd.read_csv(LEDGER, on_bad_lines="warn")
    n0 = len(df)

    stamp = datetime.utcnow().strftime("%Y%m%d")
    backup = os.path.join(ARCHIVE_DIR, "paper_trades_full_backup_" + stamp + ".csv")
    df.to_csv(backup, index=False)
    print("[cleanup] full backup ->", backup, "(", n0, "rows )")

    mask = df.apply(is_removed, axis=1)
    retired = df[mask].copy()
    kept = df[~mask].copy()

    # close OPEN removed-strategy positions at latest close
    open_ret = retired[retired["Status"].astype(str) == "OPEN"]
    for i, row in open_ret.iterrows():
        tkr = str(row["Ticker"])
        try:
            px = market_data.download(tkr, interval="1d", period="5d",
                                      allow_stale=False)
            if px is None or len(px) == 0:
                raise RuntimeError("no data")
            exit_price = float(px.iloc[-1]["Close"])
        except Exception as e:
            print("[cleanup] WARN no fresh price for", tkr, "-", e,
                  "-> closing at entry")
            exit_price = float(row["Entry_Price"])
        entry = float(row["Entry_Price"])
        qty = float(row.get("Qty") or 0)
        d = str(row.get("Direction", "LONG")).upper()
        pct = (exit_price / entry - 1) if d == "LONG" else (entry / exit_price - 1)
        pnl = round(pct * entry * qty, 2)
        retired.at[i, "Exit_Price"] = exit_price
        retired.at[i, "Exit_Time"] = now_ist().strftime("%Y-%m-%d %H:%M:%S IST")
        retired.at[i, "P&L"] = pnl
        retired.at[i, "P&L_%"] = round(pct * 100, 2)
        retired.at[i, "Status"] = "CLOSED"
        retired.at[i, "Reason"] = str(retired.at[i, "Reason"])[:40] + \
            " | STRATEGY_RETIRED v5.27"
        print("[cleanup] CLOSED retired position:", tkr, "@", exit_price,
              "P&L", pnl)

    arch = os.path.join(ARCHIVE_DIR, "paper_trades_retired_v527.csv")
    retired.to_csv(arch, index=False)
    kept.to_csv(LEDGER, index=False)
    print("[cleanup] retired rows ->", arch, "(", len(retired), ")")
    print("[cleanup] active ledger now:", len(kept), "rows (was", n0, ")")

    # rebuild portfolio.json open_positions to match kept OPEN rows only
    port = load_portfolio()
    before = len(port.get("open_positions", []))
    port["open_positions"] = [p for p in port.get("open_positions", [])
                              if not is_removed(p)]
    save_portfolio(port)
    print("[cleanup] portfolio open_positions:", before, "->",
          len(port["open_positions"]))

    tot_before = pd.to_numeric(df["P&L"], errors="coerce").fillna(0).sum()
    tot_kept = pd.to_numeric(kept["P&L"], errors="coerce").fillna(0).sum()
    tot_ret = pd.to_numeric(retired["P&L"], errors="coerce").fillna(0).sum()
    ok = abs((tot_kept + tot_ret) - tot_before) < 1.0
    print(f"[cleanup] RECON total ₹{tot_before:,.2f} = "
          f"kept ₹{tot_kept:,.2f} + retired ₹{tot_ret:,.2f} "
          f"-> {'MATCH' if ok else 'MISMATCH'}")


if __name__ == "__main__":
    main()

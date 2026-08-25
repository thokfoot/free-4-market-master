"""Independent IPO backtest - exact scanner_ipo rules, fresh data.

Answers: "IPO backtest karo - kaun sa pattern profitable hai?"

Replay per watchlist IPO on fresh yfinance 1d bars (data starts at listing):
  DIP   (936 LONG):  anchor=max(High first 3 bars); fire first bar where
          close crosses <= anchor*(1-dip%) [prev close above]; entry=close.
          exit TP entry*(1+5%) | SL entry*(1-8%) | close after 20 sessions
  SHORT (937 SHORT): enter at close of 3rd session (day-2 bar) whenever it
          exists. cover entry*(1-8%) | stop entry*(1+5%) | close after 30d
  BREAK (938 LONG):  anchor=listing-day High; fire first close ABOVE anchor
          within 31 sessions [prev close <= anchor]; entry=close.
          exit TP entry*(1+20%) | SL entry*(1-12%) | close after 45 sessions
Charges INDIAN applied per side-roundtrip. One trade per IPO per strategy
(no-backfill == first-cross only, same as scanner).

Run: python ipo_backtest.py
"""
import os, sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CHARGES_PER_MARKET, IPO_VARIANTS
from scanner_ipo import load_ipo_universe

HERE = os.path.dirname(os.path.abspath(__file__))
COST = CHARGES_PER_MARKET["INDIAN"]
V = {v["key"]: v for v in IPO_VARIANTS}


def fresh(tkr):
    import yfinance as yf, time as _t
    d = None
    for attempt in range(3):
        try:
            d = yf.download(tkr, period="730d", interval="1d",
                            progress=False, auto_adjust=False, threads=False)
            if d is not None and not d.empty:
                break
        except Exception:
            pass
        _t.sleep(1.5 * (attempt + 1))
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = d[~d.index.duplicated(keep="first")].sort_index()
    return d.dropna(subset=["Close"])


def replay(key, df, tk):
    """Return list of trade dicts for one IPO under one strategy."""
    v = V[key]
    o = df["Open"].values; h = df["High"].values
    l = df["Low"].values; c = df["Close"].values
    n = len(df)
    trades = []

    def walk(i_entry, entry, direction, hold_days):
        sl_d = v["sl_pct"]; tp_d = v["tp_pct"]
        if direction == "LONG":
            sl = entry * (1 - sl_d); tp = entry * (1 + tp_d)
        else:
            sl = entry * (1 + sl_d); tp = entry * (1 - tp_d)
        last_j = min(n - 1, i_entry + hold_days)
        for j in range(i_entry + 1, last_j + 1):
            # conservative: SL checked before TP within the same bar
            if direction == "LONG":
                if l[j] <= sl:
                    return sl, j, "SL"
                if h[j] >= tp:
                    return tp, j, "TP"
            else:
                if h[j] >= sl:
                    return sl, j, "SL"
                if l[j] <= tp:
                    return tp, j, "TP"
        return c[last_j], last_j, "TIME"

    def add(i_entry, entry, direction):
        px, j, label = walk(i_entry, entry, direction,
                            int(v["max_hold_days"]))
        gross = (px - entry) / entry * (1 if direction == "LONG" else -1)
        ts = df.index[i_entry]
        trades.append({"variant": key, "ticker": tk,
                       "entry_date": str(pd.Timestamp(ts).date()),
                       "hold_sessions": int(j - i_entry),
                       "exit": label,
                       "ret_pct": round((gross - COST) * 100, 3)})

    if key == "DIP":
        if n < 4:
            return trades
        anchor = max(h[:3])
        trigger = anchor * (1 - v["dip_pct"] / 100.0)
        for i in range(3, n):
            if c[i] <= trigger and c[i - 1] > trigger:
                add(i, c[i], "LONG")
                break  # once per IPO
    elif key == "SHORT":
        if n < 3:
            return trades
        add(2, c[2], "SHORT")  # day-2 close; in replay every historical IPO had this moment
    else:  # BREAK
        if n < 2:
            return trades
        anchor = h[0]
        wmax = int(v.get("break_max_days", 30)) + 1
        for i in range(1, min(n, wmax)):
            if c[i] > anchor and c[i - 1] <= anchor:
                add(i, c[i], "LONG")
                break  # once per IPO
    return trades


def main():
    universe = load_ipo_universe()
    print(f"[ipobt] watchlist: {len(universe)} IPOs "
          f"({pd.Series([u['strategy'] for u in universe]).value_counts().to_dict()})")

    rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fresh, u["ticker"]): u for u in universe}
        done = 0
        for fu in as_completed(futs):
            u = futs[fu]
            done += 1
            try:
                df = fu.result()
            except Exception:
                df = None
            if df is None or len(df) < 3:
                continue
            rows.extend(replay(u["strategy"], df, u["ticker"]))
            if done % 20 == 0:
                print(f"[ipobt] {done}/{len(universe)}")

    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(HERE, "logs", "ipo_backtest_trades.csv"), index=False)
    print(f"\n{'='*74}\n  IPO BACKTEST (exact scanner rules, fresh data)\n{'='*74}")
    if out.empty:
        print("no trades generated")
        return
    g = out.groupby("variant").agg(n=("ret_pct", "size"),
                                   wr=("ret_pct", lambda x: round((x > 0).mean() * 100, 1)),
                                   avg=("ret_pct", lambda x: round(x.mean(), 2)),
                                   net=("ret_pct", lambda x: round(x.sum(), 1)))
    order = ["DIP", "SHORT", "BREAK"]
    g = g.reindex([k for k in order if k in g.index])
    print(g.to_string())
    print("\nexit mix per variant:")
    for k, s in out.groupby("variant"):
        print(f"  {k:6s}", s.exit.value_counts().to_dict())
    print(f"\nTOTAL: {len(out)} trades | WR {(out.ret_pct > 0).mean()*100:.1f}% "
          f"| net {out.ret_pct.sum():+.1f}%")
    print("saved -> logs/ipo_backtest_trades.csv")


if __name__ == "__main__":
    main()

"""GAP_DOWN 30-day replay - closes the coverage gap from backtest_30d_active.py
("jo data nahi mila uske dusre source try kro": 1m data fetched in Yahoo's
max-7d chunks x4 to cover 28+ days - no alternate vendor needed).

Faithful to scanner_gap_down.py + config:
  universe    : INDIAN_TICKERS (100)
  factor      : day_open / prev_day_close - 1 < -0.5%  (day-level flag)
  A (rank997) : flag AND close<=rolling252-bar-low*1.01 -> LONG next-candle open
                SL 1.5% | TP 3.0%
  B (rank998) : GAP_DOWN_B_ENABLED=False -> NEVER fires (by design)
  hold        : 60 minutes max, exit at close of hold bar
  once per ticker per gap-down day (live blocks duplicate entries)

Run: python gapdown_backtest_30d.py
"""
import os, sys, warnings
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import pytz

IST = pytz.timezone("Asia/Kolkata")
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta

from config import (INDIAN_TICKERS, CHARGES_PER_MARKET,
                    GAP_DOWN_A_SL_PCT, GAP_DOWN_A_TP_PCT,
                    GAP_DOWN_MAX_HOLD_MINUTES)

HERE = os.path.dirname(os.path.abspath(__file__))
COST = CHARGES_PER_MARKET["INDIAN"]
CHUNKS = [(28, 21), (21, 14), (14, 7), (7, 0)]  # days ago [start,end) - stay inside Yahoo's 30d/1m limit


def chunk_1m(tkr):
    parts = []
    end0 = datetime.now()
    for a, b in CHUNKS:
        s = (end0 - timedelta(days=a)).strftime("%Y-%m-%d")
        e = (end0 - timedelta(days=b)).strftime("%Y-%m-%d")
        df = None
        for attempt in range(2):
            try:
                df = yf.Ticker(tkr).history(start=s, end=e, interval="1m",
                                            auto_adjust=True)
                if df is not None and len(df):
                    break
            except Exception:
                pass
            import time as _t; _t.sleep(1.5 * (attempt + 1))
        if df is not None and len(df):
            parts.append(df)
        import time as _t; _t.sleep(0.3)
    if not parts:
        return None
    out = pd.concat(parts)
    if isinstance(out.columns, pd.MultiIndex):
        out.columns = out.columns.get_level_values(0)
    out = out[~out.index.duplicated(keep="first")].sort_index()
    return out.dropna(subset=["Close"]) if len(out) else None


def factors(df):
    d = df.copy()
    d["_day"] = d.index.date
    do = d.groupby("_day")["Open"].first()
    dc = d.groupby("_day")["Close"].last()
    gap = (do / dc.shift(1) - 1) * 100
    d["gap"] = d["_day"].map(gap)
    d["flag"] = (d["gap"] < -0.5).astype(int)
    lb = min(len(d), 252)
    d["low_n"] = d["Close"].rolling(lb, min_periods=lb).min() if lb >= 20 else np.nan
    d["near_low"] = (d["Close"] <= d["low_n"] * 1.01).astype(int)
    return d


def replay_one(tkr):
    raw = chunk_1m(tkr)
    if raw is None or len(raw) < 300:
        return None, None
    idx = raw.index.tz_convert(IST) if raw.index.tz is not None \
        else raw.index.tz_localize(IST)
    raw.index = idx
    f = factors(raw)
    c = f["Open"].values; h = f["High"].values; l = f["Low"].values
    cl = f["Close"].values
    flag = f["flag"].values; nl = f["near_low"].values
    days = f.index.date
    hold = int(GAP_DOWN_MAX_HOLD_MINUTES)
    rows_a = []
    seen_days = set()
    n = len(f)
    i = 1
    while i < n:
        if flag[i - 1] == 1 and nl[i - 1] == 1 and days[i] not in seen_days:
            entry = float(c[i])
            sl = entry * (1 - GAP_DOWN_A_SL_PCT)
            tp = entry * (1 + GAP_DOWN_A_TP_PCT)
            exit_px, label, j = None, "TIME", min(n - 1, i + hold)
            for k in range(i + 1, j + 1):
                if l[k] <= sl:
                    exit_px, label = sl, "SL"; break
                if h[k] >= tp:
                    exit_px, label = tp, "TP"; break
            if exit_px is None:
                exit_px = cl[j]
            pnl = (exit_px - entry) / entry - COST
            rows_a.append({"ticker": tkr, "date": str(days[i]),
                           "entry": round(entry, 2), "exit": label,
                           "ret_pct": round(pnl * 100, 3)})
            seen_days.add(days[i])
            i = j + 1
            continue
        i += 1
    return rows_a, len(raw)


def main():
    print(f"[gdbt] universe: {len(INDIAN_TICKERS)} tickers | "
          f"A: SL{GAP_DOWN_A_SL_PCT:.1%}/TP{GAP_DOWN_A_TP_PCT:.1%} "
          f"hold {GAP_DOWN_MAX_HOLD_MINUTES}m | B disabled by config")
    all_rows, ok, fail = [], 0, 0
    with ThreadPoolExecutor(max_workers=4) as ex:
        futs = {ex.submit(replay_one, t): t for t in INDIAN_TICKERS}
        done = 0
        for fu in as_completed(futs):
            t = futs[fu]
            try:
                rows, nbars = fu.result()
            except Exception as e:
                print(f"[gdbt] ERR {t}: {type(e).__name__} {str(e)[:80]}")
                rows, nbars = None, None
            if rows is None:
                fail += 1
            else:
                ok += 1
                all_rows.extend(rows)
            done += 1
            if done % 20 == 0:
                print(f"[gdbt] {done}/{len(INDIAN_TICKERS)}")
    out = pd.DataFrame(all_rows).sort_values("date") if all_rows \
        else pd.DataFrame(columns=["ticker", "date", "entry", "exit", "ret_pct"])
    out.to_csv(os.path.join(HERE, "logs", "gapdown_backtest30d_trades.csv"),
               index=False)
    print(f"\n{'='*70}\n  GAP_DOWN 30-DAY REPLAY (rank 997 'A')\n{'='*70}")
    print(f"data ok: {ok}/{len(INDIAN_TICKERS)} tickers ({fail} failed)")
    if len(out) == 0:
        print("no signals fired in window")
        return
    wr = (out.ret_pct > 0).mean() * 100
    print(f"trades: {len(out)} | WR {wr:.1f}% | net {out.ret_pct.sum():+.2f}% "
          f"| avg {out.ret_pct.mean():+.2f}%/trade")
    print("exit mix:", out.exit.value_counts().to_dict())
    worst = out.nsmallest(5, "ret_pct")
    print("\nworst 5:")
    print(worst.to_string(index=False))
    print("\nsaved -> logs/gapdown_backtest30d_trades.csv")


if __name__ == "__main__":
    main()

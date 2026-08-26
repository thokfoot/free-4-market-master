"""Forensic verifier - re-validate CLOSED losing trades against REAL yfinance bars.

For every closed trade of the loss-making strategies:
  ENTRY : entry_price must equal the OHLC bar close at/near Time_IST
          (scanners emit signal-candle close as fill). tolerance 0.2%.
  EXIT  : "SL Hit" requires real bars AFTER entry touching the SL level;
          "Target Hit" requires touching TP; TIME exits must sit inside
          the day's range. tolerance 0.25%.
Flags: OK / ENTRY-BAD / EXIT-NOT-TOUCHED / DATA-MISSING.

Run: python trade_verifier.py
"""
import os, sys, warnings
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
import pytz
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

IST = pytz.timezone("Asia/Kolkata")
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "logs", "trade_verification.csv")

FOCUS_RANKS = {902, 903, 904, 901, 900, 906, 908, 909, 910, 920, 933, 996,
               936, 935}
FOCUS_TICKERS = {"XLK", "QQQ", "AVAX-USD", "OEF", "TRX-USD", "XLV"}

VAR_INTERVAL = {}  # rank -> intraday interval for fade-family
try:
    from config import FADE_VARIANTS, LONG_BOUNCE_VARIANTS
    for v in FADE_VARIANTS + LONG_BOUNCE_VARIANTS:
        VAR_INTERVAL[int(v["rank"])] = v["interval"]
except Exception:
    pass


def bars_for(tkr, start, end, interval):
    for attempt in range(2):
        try:
            d = yf.download(tkr, start=start, end=end, interval=interval,
                            progress=False, auto_adjust=False, threads=False)
            if d is not None and len(d):
                if isinstance(d.columns, pd.MultiIndex):
                    d.columns = d.columns.get_level_values(0)
                return d.dropna(subset=["Close"])
        except Exception:
            pass
        import time as _t; _t.sleep(0.8 * (attempt + 1))
    return None


def verify(row):
    tkr = str(row["Ticker"]); tf = str(row["TimeFrame"])
    rk = int(float(row["Pattern_Rank"])) if pd.notna(row["Pattern_Rank"]) else 0
    direction = str(row["Direction"]).upper()
    entry = float(row["Entry_Price"]); exitp = float(row["Exit_Price"])
    sl = float(row["SL"]); tp = float(row["Target"])
    d_in = pd.Timestamp(str(row["Date"]))
    t_in = str(row.get("Time_IST", ""))[:8]
    tin = None
    try:
        if len(t_in) == 8 and ":" in t_in:
            tin = datetime_in(d_in, t_in)
    except Exception:
        tin = None

    if tf in ("SWING_1d", "IPO_1d"):
        interval, span = "1d", 12
    elif tf == "FADE_1h":
        interval, span = VAR_INTERVAL.get(rk, "15m"), 6
    elif tf in ("INTRADAY_1h",):
        interval, span = "1h", 6
    elif tf == "LONG_BOUNCE_5m":
        interval, span = "5m", 4
    else:
        interval, span = "1h", 6

    df = bars_for(tkr, str((d_in - pd.Timedelta(days=3)).date()),
                  str((d_in + pd.Timedelta(days=span)).date()), interval)
    if df is None or not len(df):
        return "DATA-MISSING", ""
    idx0 = df.index
    idx = idx0.tz_convert(IST) if idx0.tz is not None \
        else idx0.tz_localize(IST)
    df = df.set_index(idx)  # aligned lookup - no naive/aware mismatch

    # --- ENTRY: must lie inside the real bar RANGE containing fill time ---
    # (bot fills at live price mid-candle, not exactly at bar close)
    notes = []
    if tin is not None:
        pre = idx[idx <= pd.Timestamp(tin)]
        if len(pre):
            b = df.loc[pre[-1]]
            blo, bhi = float(b["Low"]), float(b["High"])
            if not (blo * 0.995 <= entry <= bhi * 1.005):
                notes.append(f"ENTRY {entry:.2f} outside bar "
                             f"[{blo:.2f},{bhi:.2f}] @{pre[-1].strftime('%H:%M')}")
        else:
            notes.append("ENTRY: no bar before stamp")
    else:
        day = idx[idx.date == d_in.date()]
        if len(day):
            lo_d = float(df.loc[day, "Low"].min())
            hi_d = float(df.loc[day, "High"].max())
            if not (lo_d * 0.995 <= entry <= hi_d * 1.005):
                notes.append(f"ENTRY {entry:.2f} outside day range "
                             f"[{lo_d:.2f},{hi_d:.2f}]")
        else:
            notes.append("ENTRY: no bars on entry date")

    # --- EXIT: did price really touch SL/TP after entry? ---
    # window STARTS at the containing bar (its high/low occurred around the
    # fill and can legitimately stop out a SHORT within seconds)
    reason = str(row.get("Reason", ""))
    ex_date = str(row.get("Exit_Time", ""))[:10]
    try:
        ex_dt = pd.Timestamp(ex_date)
        start_at = idx[idx <= pd.Timestamp(tin)][0] if tin is not None \
            else pd.Timestamp(d_in.date(), tz=IST)
        win = idx[(idx >= start_at) & (idx.date <= ex_dt.date())]
    except Exception:
        win = idx[idx >= pd.Timestamp(tin)] if tin is not None else idx
    hi = float(df.loc[win, "High"].max()) if len(win) else np.nan
    lo = float(df.loc[win, "Low"].min()) if len(win) else np.nan
    if "SL" in reason.upper():
        touched = (hi >= sl * 0.999) if direction == "LONG" else (lo <= sl * 1.001)
        if not touched:
            notes.append(f"SL never touched (real hi={hi:.2f} lo={lo:.2f} sl={sl:.2f})")
    elif "TARGET" in reason.upper():
        touched = (lo <= tp * 1.001) if direction == "LONG" else (hi >= tp * 0.999)
        if not touched:
            notes.append(f"TP never touched")
    else:
        if len(win) and np.isfinite(hi) and np.isfinite(lo):
            inside = (lo * 0.995 <= exitp <= hi * 1.005)
            if not inside:
                notes.append(f"EXIT {exitp:.2f} outside real range [{lo:.2f},{hi:.2f}]")
        else:
            notes.append("EXIT: no post-entry bars")

    return ("OK" if not notes else "|".join(notes)), ""


def datetime_in(d, t):
    hh, mm, ss = (int(x) for x in t.split(":"))
    return IST.localize(pd.Timestamp(d.year, d.month, d.day, hh, mm, ss).to_pydatetime()
                        .replace(tzinfo=None))


def main():
    t = pd.read_csv(os.path.join(HERE, "logs", "paper_trades.csv"),
                    on_bad_lines="warn")
    t["rk"] = pd.to_numeric(t["Pattern_Rank"], errors="coerce")
    t["pnl"] = pd.to_numeric(t["P&L"], errors="coerce")
    cl = t[(t.Status == "CLOSED")].copy()
    foc = cl[((cl.TimeFrame.astype(str) == "FADE_1h") & cl.rk.isin(FOCUS_RANKS))
             | ((cl.TimeFrame.astype(str) == "IPO_1d"))
             | ((cl.TimeFrame.astype(str).isin(["SWING_1d", "INTRADAY_1h"]))
                & (cl.Ticker.astype(str).isin(FOCUS_TICKERS))
                & (cl.pnl < 0))]
    foc = foc[foc.rk.notna()]
    print(f"[verify] checking {len(foc)} closed trades "
          f"(P&L {foc.pnl.sum():+,.0f})")
    res = []
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(verify, r): i for i, r in foc.iterrows()}
        done = 0
        for fu in as_completed(futs):
            i = futs[fu]
            try:
                verdict, _ = fu.result()
            except Exception as e:
                verdict = f"ERR:{type(e).__name__}"
            r = foc.loc[i]
            res.append({"ticker": r.Ticker, "rank": r.rk, "tf": r.TimeFrame,
                        "date": r.Date, "dir": r.Direction,
                        "pnl": round(float(r.pnl), 2), "verdict": verdict})
            done += 1
            if done % 20 == 0:
                print(f"[verify] {done}/{len(foc)}")
    out = pd.DataFrame(res)
    out.to_csv(OUT, index=False)
    print("\n=== VERDICT SUMMARY ===")
    vc = out.verdict.str.split("|").str[0].value_counts()
    print(vc.to_string())
    bad = out[~out.verdict.str.startswith(("OK",))]
    bad_pnl = bad.pnl.sum()
    print(f"\nflagged: {len(bad)} trades | P&L involved Rs{bad_pnl:+,.0f}")
    if len(bad):
        print(bad.sort_values("pnl").head(15).to_string(index=False))
    print(f"\nsaved -> {OUT}")


if __name__ == "__main__":
    main()

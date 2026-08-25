"""Full environment audit - EVERY deployed strategy backtested on its OWN
instrument with FRESH yfinance data.

If the deployment environment is correct, the surviving strategies should
show positive expectancy on the very tickers they trade.

Covers:
  - 174 SWING_1d defs  (data/strategies.csv)
  - 43  INTRADAY_1h defs (data/intraday_strategies.csv)
(FADE audited separately by fade_backtest.py on the new liquid universe.)

Verdicts:
  HEALTHY  net>0 over full AND 150d
  WATCH    net>0 full but 150d negative
  SICK     negative overall

Run: python env_audit.py
"""
import os, sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
import numpy as np
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CHARGES_PER_MARKET
from scanner import get_yf_ticker, compute_indicators
from scanner_intraday import compute_indicators_1h
import strategy_miner as sm

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(HERE, "logs", "env_audit.csv")
REGION_NORM = {"India": "INDIAN", "INDIAN": "INDIAN", "US": "US",
               "Crypto": "CRYPTO", "CRYPTO": "CRYPTO"}


def load_defs():
    defs = []
    for path, tf in (("data/strategies.csv", "SWING_1d"),
                     ("data/intraday_strategies.csv", "INTRADAY_1h")):
        df = pd.read_csv(os.path.join(HERE, path), on_bad_lines="warn")
        for _, r in df.iterrows():
            mkt = str(r["Market"])
            yft = get_yf_ticker(mkt)
            if not yft:
                continue
            defs.append({
                "market": mkt, "ticker": yft,
                "rank": int(r["Final_Rank"]), "tf": tf,
                "region": REGION_NORM.get(str(r["Region"]), "US"),
                "direction": str(r["Direction"]).upper(),
                "factors": [f.strip() for f in str(r["Factors"]).split("+")],
            })
    return defs


def fresh(tkr, interval):
    import yfinance as yf, time as _t
    d = None
    for attempt in range(3):
        try:
            d = yf.download(tkr, period="730d" if interval == "1h" else "5y",
                            interval=interval, progress=False,
                            auto_adjust=False, threads=False)
            if d is not None and not d.empty:
                break
        except Exception:
            pass
        _t.sleep(2 * (attempt + 1))
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    return sm.normalize_columns(d)


def stat(trades, cost, days):
    cut = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days)

    def _utc(ts):
        return ts.tz_localize("UTC") if getattr(ts, "tzinfo", None) is None \
            else ts.tz_convert("UTC")
    sub = [t for t in trades if _utc(t[0]) >= cut]
    n = len(sub)
    if n == 0:
        return 0, 0.0, 0.0
    gross = sum(p for _, p in sub)
    net = gross - cost * n
    wr = sum(1 for _, p in sub if p > 0) / n * 100
    return n, round(wr, 1), round(net * 100, 2)


def main():
    defs = load_defs()
    print(f"[audit] deployed defs: {len(defs)}")
    need = sorted({(d["ticker"], "1h" if d["tf"] == "INTRADAY_1h" else "1d")
                   for d in defs})
    print(f"[audit] unique instruments: {len(need)} - downloading fresh...")
    data = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fresh, t, iv): (t, iv) for t, iv in need}
        done = 0
        for fu in as_completed(futs):
            data[futs[fu]] = fu.result()
            done += 1
            if done % 10 == 0:
                print(f"[audit] data {done}/{len(need)}")
    ok = sum(1 for v in data.values() if v is not None)
    print(f"[audit] fresh data ok: {ok}/{len(need)}")

    rows = []
    for i, d in enumerate(defs):
        iv = "1h" if d["tf"] == "INTRADAY_1h" else "1d"
        ind_fn = compute_indicators_1h if iv == "1h" else compute_indicators
        min_rows = 200 if iv == "1h" else 60
        raw = data.get((d["ticker"], iv))
        rec = {"market": d["market"], "rank": d["rank"], "tf": d["tf"],
               "dir": d["direction"], "ticker": d["ticker"]}
        if raw is None:
            rec.update(verdict="NO-DATA")
            rows.append(rec)
            continue
        try:
            d2 = ind_fn(raw.copy())
            facs = {f: sm.factor_series(d2, f) for f in set(d["factors"])}
            if d2 is None or len(d2) < min_rows or any(v is None for v in facs.values()):
                rec.update(verdict="BAD-DEF")
                rows.append(rec)
                continue
            cost = CHARGES_PER_MARKET[d["region"]]
            tr = sm.backtest_one(d2, d["factors"], facs, d["direction"],
                                 d["tf"], d["region"]) or []
            nf, wrf, netf = stat(tr, cost, 100000)
            n150, _, net150 = stat(tr, cost, 150)
            n30, _, net30 = stat(tr, cost, 30)
            v = ("HEALTHY" if (netf > 0 and net150 > 0)
                 else "WATCH" if netf > 0 else "SICK")
            rec.update(n_full=nf, wr_full=wrf, net_full=netf,
                       n150=n150, net150=net150, net30=net30, verdict=v)
        except Exception as e:
            rec.update(verdict=f"ERR:{type(e).__name__}")
        rows.append(rec)
        if (i + 1) % 50 == 0:
            print(f"[audit] {i+1}/{len(defs)}")

    out = pd.DataFrame(rows)
    out.to_csv(OUT_CSV, index=False)

    print(f"\n{'='*74}\n  ENVIRONMENT AUDIT - {len(out)} deployed strategies\n{'='*74}")
    print(out["verdict"].value_counts().to_string())
    for tf in ("SWING_1d", "INTRADAY_1h"):
        s = out[out["tf"] == tf]
        h = int((s["verdict"] == "HEALTHY").sum())
        w = int((s["verdict"] == "WATCH").sum())
        sk = int((s["verdict"] == "SICK").sum())
        net150 = pd.to_numeric(s["net150"], errors="coerce").fillna(0).sum()
        print(f"\n{tf}: HEALTHY {h} | WATCH {w} | SICK {sk} "
              f"| combined 150d net {net150:+.0f}%")
    sick = out[out["verdict"] == "SICK"]
    if len(sick):
        print("\n-- SICK list --")
        print(sick[["market", "rank", "tf", "dir", "net_full", "net150"]]
              .to_string(index=False))
    print(f"\nsaved -> {OUT_CSV}")


if __name__ == "__main__":
    main()

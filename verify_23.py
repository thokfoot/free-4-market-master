"""Re-verify the 23 CONSISTENT-PROFIT groups on FRESH yfinance data.

For each group: reload its CURRENT strategy def, download fresh data
(auto_adjust=False, harness parity), run the bot-identical backtest and
report full / 150d / 30d stats. Index-ticker groups are additionally
annotated with their ETF-proxy retest status (from etf_retest.csv).

Run: python verify_23.py
"""
import os, sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CHARGES_PER_MARKET
from scanner import get_yf_ticker, compute_indicators
from scanner_intraday import compute_indicators_1h
import strategy_miner as sm

HERE = os.path.dirname(os.path.abspath(__file__))
GROUPS = [  # (market, rank, tf) of the 23 CONSISTENT-PROFIT groups
    ("XLK_Tech", 3, "INTRADAY_1h"), ("XLV", 4, "SWING_1d"), ("QQQ", 52, "SWING_1d"),
    ("ETH", 32, "SWING_1d"), ("QQQ", 5, "INTRADAY_1h"), ("Nasdaq100", 6, "INTRADAY_1h"),
    ("IWM", 14, "INTRADAY_1h"), ("DIA", 36, "INTRADAY_1h"), ("SP500", 16, "INTRADAY_1h"),
    ("XLP", 5, "SWING_1d"), ("SPY", 31, "INTRADAY_1h"), ("XLY", 1, "SWING_1d"),
    ("PHLX_Semi", 3, "SWING_1d"), ("QQQ", 37, "SWING_1d"), ("SPY", 32, "INTRADAY_1h"),
    ("DIA", 13, "INTRADAY_1h"), ("NYSE_Comp", 3, "SWING_1d"), ("Nasdaq100", 64, "SWING_1d"),
    ("XLK_Tech", 28, "SWING_1d"), ("PHLX_Semi", 46, "SWING_1d"), ("Nasdaq100", 45, "SWING_1d"),
    ("QQQ", 50, "SWING_1d"), ("TRX", 1, "SWING_1d"),
]
REGION_NORM = {"India": "INDIAN", "INDIAN": "INDIAN", "US": "US",
               "Crypto": "CRYPTO", "CRYPTO": "CRYPTO"}


def load_defs():
    rows = {}
    for path, tf in (("data/strategies.csv", "SWING_1d"),
                     ("data/intraday_strategies.csv", "INTRADAY_1h")):
        df = pd.read_csv(os.path.join(HERE, path), on_bad_lines="warn")
        for _, r in df.iterrows():
            key = (str(r["Market"]), int(r["Final_Rank"]), tf)
            if key in GROUPS and key not in rows:
                rows[key] = {
                    "region": REGION_NORM.get(str(r["Region"]), "US"),
                    "direction": str(r["Direction"]).upper(),
                    "factors": [f.strip() for f in str(r["Factors"]).split("+")]}
    return rows


def fresh(yft, interval, period):
    import yfinance as yf, time as _t
    d = None
    for attempt in range(3):
        try:
            d = yf.download(yft, period=period, interval=interval,
                            progress=False, auto_adjust=False, threads=False)
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
    print(f"defs found: {len(defs)}/23")
    need = {(get_yf_ticker(m),
             "1h" if tf == "INTRADAY_1h" else "1d",
             "730d" if tf == "INTRADAY_1h" else "5y")
            for m, r, tf in defs}
    data = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fresh, y, iv, pe): (y, iv, pe) for y, iv, pe in need}
        for fu in as_completed(futs):
            data[futs[fu]] = fu.result()

    try:
        etf = pd.read_csv(os.path.join(HERE, "logs", "index_to_etf_retest.csv"))
        etf_status = {(r["tf"], r["rank"]): r["verdict"] for _, r in etf.iterrows()}
    except Exception:
        etf_status = {}

    out = []
    for (mkt, rank, tf), d in defs.items():
        yft = get_yf_ticker(mkt)
        iv = "1h" if tf == "INTRADAY_1h" else "1d"
        raw = data.get((yft, iv, "730d" if iv == "1h" else "5y"))
        rec = {"market": mkt, "rank": rank, "tf": tf, "dir": d["direction"]}
        if raw is None:
            rec.update(status="no-data", n150="", net150="")
            out.append(rec)
            continue
        ind_fn = compute_indicators_1h if iv == "1h" else compute_indicators
        min_rows = 200 if iv == "1h" else 60
        d2 = ind_fn(raw.copy())
        facs = {f: sm.factor_series(d2, f) for f in set(d["factors"])}
        if d2 is None or len(d2) < min_rows or any(v is None for v in facs.values()):
            rec.update(status="bad-factor", n150="", net150="")
            out.append(rec)
            continue
        cost = CHARGES_PER_MARKET[d["region"]]
        tr = sm.backtest_one(d2, d["factors"], facs, d["direction"], tf, d["region"]) or []
        nf, wrf, netf = stat(tr, cost, 100000)
        n150, wr150, net150 = stat(tr, cost, 150)
        n30, _, net30 = stat(tr, cost, 30)
        still = "STILL-PROFIT ✔" if net150 > 0 and netf > 0 else (
            "WEAKENED ~" if netf > 0 else "TURNED-LOSS ✘")
        note = ""
        if str(yft).startswith("^"):
            v = etf_status.get((tf, rank), "")
            note = f"| ETF-swap:{v}"
        rec.update(status=still, n_full=nf, wr_full=wrf, net_full=netf,
                   n150=n150, net150=net150, net30=net30, note=note)
        out.append(rec)

    o = pd.DataFrame(out)
    print(f"\n{'='*80}\n  RE-VERIFICATION OF 23 LIVE-PROFITABLE GROUPS (fresh data)\n{'='*80}")
    print(o.to_string(index=False))
    print()
    vc = o["status"].value_counts()
    print(vc.to_string())
    o.to_csv(os.path.join(HERE, "logs", "verify23_fresh.csv"), index=False)


if __name__ == "__main__":
    main()

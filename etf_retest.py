"""Re-validate the 93 index-ticker strategies on tradable ETF proxies.

Index symbols (^NDX, ^NSEI, ...) can never be traded — no shares exist.
Their factor combos were mined on index DATA (fine), but execution needs an
instrument. This tool swaps each blocked market for its liquid ETF proxy,
re-runs the bot-identical backtest engine on FRESH yfinance data, and grades
each strategy PASS / WEAK / FAIL against the miner's own deployment gates.

Outputs:
  logs/index_to_etf_retest.csv          per-strategy results
  data/proposed_strategies.csv           swing file with PASS rows swapped
  data/proposed_intraday_strategies.csv  intraday file with PASS rows swapped
(no live file is modified — deploy only after review)

Run: python etf_retest.py
"""
import os, sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CHARGES_PER_MARKET
from scanner import get_yf_ticker, compute_indicators
from scanner_intraday import compute_indicators_1h
import strategy_miner as sm

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(HERE, "logs", "index_to_etf_retest.csv")

# Blocked index -> tradable proxy (None = no liquid proxy, strategy must die)
ETF_MAP = {
    "Nasdaq100": "QQQ",
    "SP500": "SPY",
    "PHLX_Semi": "SOXX",
    "NYSE_Comp": "VTI",       # broad US proxy for NYSE Composite
    "SP400": "IJH",           # exact mid-cap match
    "KBW": "KBE",             # diversified banks vs KBW index
    "Dow_Util": "XLU",        # utilities sector vs Dow utility avg
    "Dow_Trans": None,        # no liquid transports ETF proxy
    "Sensex": None,           # no liquid Sensex ETF
    "Nifty 50": "NIFTYBEES.NS",
    "Nifty 50 Yahoo": "NIFTYBEES.NS",
    "GIFT Nifty": "NIFTYBEES.NS",
    "Bank Nifty": "BANKBEES.NS",
    "Bank Nifty Yahoo": "BANKBEES.NS",
}
REGION_NORM = {"India": "INDIAN", "INDIAN": "INDIAN", "US": "US",
               "Crypto": "CRYPTO", "CRYPTO": "CRYPTO"}
PERIOD = {"SWING_1d": ("1d", "5y", 60), "INTRADAY_1h": ("1h", "730d", 200)}
WINDOWS_DAYS = {"full": 100000, "d150": 150, "d30": 30}


def fresh(yft, interval, period):
    import yfinance as yf
    import time as _t
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


def stat(trades, cost_rt, days):
    if not trades:
        return 0, 0.0, 0.0
    cut = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=days) \
        if days < 100000 else None

    def _utc(ts):
        return ts.tz_localize("UTC") if getattr(ts, "tzinfo", None) is None \
            else ts.tz_convert("UTC")
    sub = trades if cut is None else [t for t in trades if _utc(t[0]) >= cut]
    n = len(sub)
    if n == 0:
        return 0, 0.0, 0.0
    gross = sum(p for _, p in sub)
    net = gross - cost_rt * n
    wr = sum(1 for _, p in sub if p > 0) / n * 100
    return n, round(wr, 1), round(net * 100, 2)


def main():
    files = {"SWING_1d": "data/strategies.csv",
             "INTRADAY_1h": "data/intraday_strategies.csv"}
    targets = []
    for tf, path in files.items():
        df = pd.read_csv(os.path.join(HERE, path), on_bad_lines="warn")
        for i, r in df.iterrows():
            mkt = str(r["Market"])
            if get_yf_ticker(mkt) and str(get_yf_ticker(mkt)).startswith("^"):
                targets.append({"tf": tf, "path": path, "row_idx": i,
                                "market": mkt,
                                "rank": int(r["Final_Rank"]),
                                "region": REGION_NORM.get(str(r["Region"]), "US"),
                                "direction": str(r["Direction"]).upper(),
                                "factors": [f.strip() for f in str(r["Factors"]).split("+")]})
    print(f"[etf] index-blocked strategies found: {len(targets)}")

    need = {}
    for t in targets:
        etf = ETF_MAP.get(t["market"])
        if etf:
            iv, pe, _ = PERIOD[t["tf"]]
            need[(etf, iv, pe)] = None
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fresh, y, iv, pe): (y, iv, pe) for y, iv, pe in need}
        for fu in as_completed(futs):
            need[futs[fu]] = fu.result()
    ok_data = {k: v for k, v in need.items() if v is not None}
    print(f"[etf] fresh proxy data: {len(ok_data)}/{len(need)} tickers")

    out_rows = []
    for t in targets:
        etf = ETF_MAP.get(t["market"])
        rec = {"tf": t["tf"], "market": t["market"], "rank": t["rank"],
               "direction": t["direction"], "factors": "+".join(t["factors"]),
               "proxy": etf or ""}
        if etf is None:
            rec.update(verdict="FAIL-no-proxy")
            out_rows.append(rec)
            continue
        iv, pe, min_rows = PERIOD[t["tf"]]
        raw = ok_data.get((etf, iv, pe))
        if raw is None:
            rec.update(verdict="FAIL-no-data")
            out_rows.append(rec)
            continue
        ind_fn = compute_indicators_1h if iv == "1h" else compute_indicators
        try:
            d2 = ind_fn(raw.copy())
            facs = {f: sm.factor_series(d2, f) for f in set(t["factors"])}
            if any(v is None for v in facs.values()) or d2 is None or len(d2) < min_rows:
                rec.update(verdict="FAIL-bad-factor")
                out_rows.append(rec)
                continue
            new_region = "INDIAN" if etf.endswith(".NS") else "US"
            cost = CHARGES_PER_MARKET[new_region]
            tr = sm.backtest_one(d2, t["factors"], facs, t["direction"],
                                 t["tf"], new_region) or []
            nF, wrF, netF = stat(tr, cost, WINDOWS_DAYS["full"])
            n150, wr150, net150 = stat(tr, cost, WINDOWS_DAYS["d150"])
            n30, _, net30 = stat(tr, cost, WINDOWS_DAYS["d30"])
            gate = sm.GATES[t["tf"]]
            passed = (netF > 0 and wrF >= gate["min_win"] and nF >= gate["min_trades"]
                      and n150 >= 3 and net150 > 0)
            weak = (netF > 0 and not passed)
            rec.update(proxy_region=new_region, n_full=nF, wr_full=wrF, net_full=netF,
                       n_150d=n150, wr_150d=wr150, net_150d=net150, net_30d=net30,
                       verdict="PASS" if passed else ("WEAK" if weak else "FAIL"))
        except Exception as e:
            rec.update(verdict=f"ERROR:{e}")
        out_rows.append(rec)

    out = pd.DataFrame(out_rows)
    out.to_csv(OUT_CSV, index=False)

    print(f"\n{'='*74}\n  INDEX -> ETF PROXY RE-VALIDATION ({len(out)} strategies)\n{'='*74}")
    print(out["verdict"].value_counts().to_string())
    ps = out[out["verdict"] == "PASS"]
    if len(ps):
        print("\n-- PASS (deployable on proxy) --")
        print(ps[["market", "rank", "tf", "direction", "proxy", "n_full",
                  "wr_full", "net_full", "n_150d", "net_150d"]].to_string(index=False))

    # ── proposed files: swap Market for PASS rows only ──
    pass_keys = {(r["tf"], r["market"], r["rank"]) for r in out_rows
                 if r.get("verdict") == "PASS"}
    proxy_of = {(r["tf"], r["market"], r["rank"]): r["proxy"]
                for r in out_rows if r.get("verdict") == "PASS"}
    for tf, path in files.items():
        src = pd.read_csv(os.path.join(HERE, path), on_bad_lines="warn")
        changed = 0
        for i, r in src.iterrows():
            key = (tf, str(r["Market"]), int(r["Final_Rank"]))
            if key in pass_keys:
                src.at[i, "Market"] = proxy_of[key]
                src.at[i, "Region"] = "India" if proxy_of[key].endswith(".NS") else "US"
                changed += 1
        dst = os.path.join(HERE, "data",
                           "proposed_" + os.path.basename(path))
        src.to_csv(dst, index=False)
        print(f"[etf] {dst}: {changed} rows swapped -> {dst}")


if __name__ == "__main__":
    main()

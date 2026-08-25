"""30-day backtest of ALL ACTIVE strategies (205 = 245 minus 40 paused FADE).

Covers:
  SWING_1d    159 defs  - strategy_miner engine on fresh 1d data
  INTRADAY_1h  35 defs  - same engine on fresh 1h data (GAP_DOWN excluded, see below)
  GAP_DOWN      2 defs  - SKIPPED: needs 1-minute bars + next-open fills; not
                          faithfully replayable here (reported as N/A)
  IPO           3 vars  - exact scanner rules via ipo_backtest.replay()
  US_FADE       5 vars  - faithful replay: shoot+vol+RSI+below-VWAP, SPY-gap cap,
                          ET window, SHORT at NEXT bar open, SL/TP/6h hold
  LONG_BOUNCE   1 var   - drop+vol+RSI<=45+below-VWAP&VWAP-down, NIFTY-gap cap,
                          IST window, LONG at next bar open, SL/TP/6h hold

Window: trades ENTERED in the last 30 days. Charges applied per region.

Run: python backtest_30d_active.py
"""
import os, sys, io, warnings
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CHARGES_PER_MARKET, IPO_VARIANTS, US_FADE_VARIANTS, LONG_BOUNCE_VARIANTS
from config import US_FADE_MIN_PRICE, LONG_BOUNCE_MIN_PRICE
from scanner import get_yf_ticker, compute_indicators
from scanner_intraday import compute_indicators_1h
from scanner_fade import _shoot_series, BAR_MINUTES, WINDOWS
from scanner_fade_us import compute_us_fade_indicators, WINDOWS as WIN_US
from scanner_long import compute_long_indicators, _drop_series
import strategy_miner as sm
from ipo_backtest import replay as ipo_replay

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "logs", "backtest_30d_trades.csv")
REGION_NORM = {"India": "INDIAN", "INDIAN": "INDIAN", "US": "US",
               "Crypto": "CRYPTO", "CRYPTO": "CRYPTO"}
CUTOFF = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=30)


def dl(tkr, interval, period):
    d = None
    for attempt in range(3):
        try:
            d = yf.download(tkr, period=period, interval=interval,
                            progress=False, auto_adjust=False, threads=False)
            if d is not None and not d.empty:
                break
        except Exception:
            pass
        import time as _t; _t.sleep(1.5 * (attempt + 1))
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    return sm.normalize_columns(d).dropna(subset=["Close"])


def load_defs():
    defs = []
    for path, tf in (("data/strategies.csv", "SWING_1d"),
                     ("data/intraday_strategies.csv", "INTRADAY_1h")):
        df = pd.read_csv(os.path.join(HERE, path), on_bad_lines="warn")
        for _, r in df.iterrows():
            if tf == "INTRADAY_1h" and int(r["Final_Rank"]) in (997, 998):
                continue  # GAP_DOWN handled separately (skipped)
            yft = get_yf_ticker(str(r["Market"]))
            if not yft:
                continue
            defs.append({"market": str(r["Market"]), "ticker": yft,
                         "rank": int(r["Final_Rank"]), "tf": tf,
                         "region": REGION_NORM.get(str(r["Region"]), "US"),
                         "direction": str(r["Direction"]).upper(),
                         "factors": [f.strip() for f in str(r["Factors"]).split("+")]})
    return defs


def combo_trades_last30():
    defs = load_defs()
    print(f"[bt30] combos: {len(defs)} defs")
    need = sorted({(d["ticker"], "1h" if d["tf"] == "INTRADAY_1h" else "1d") for d in defs})
    data = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(dl, t, iv, "730d" if iv == "1h" else "5y"): (t, iv) for t, iv in need}
        done = 0
        for fu in as_completed(futs):
            data[futs[fu]] = fu.result()
            done += 1
            if done % 15 == 0:
                print(f"[bt30] data {done}/{len(need)}")
    rows = []
    for i, d in enumerate(defs):
        iv = "1h" if d["tf"] == "INTRADAY_1h" else "1d"
        raw = data.get((d["ticker"], iv))
        rec = {"family": d["tf"], "name": f"{d['market']}#{d['rank']}", "dir": d["direction"]}
        if raw is None or len(raw) < (200 if iv == "1h" else 60):
            rows.append({**rec, "n30": 0}); continue
        try:
            d2 = (compute_indicators_1h if iv == "1h" else compute_indicators)(raw.copy())
            facs = {f: sm.factor_series(d2, f) for f in set(d["factors"])}
            if any(v is None for v in facs.values()):
                rows.append({**rec, "n30": 0}); continue
            tr = sm.backtest_one(d2, d["factors"], facs, d["direction"],
                                 d["tf"], d["region"]) or []
            cost = CHARGES_PER_MARKET[d["region"]]
            sub = [t for t in tr
                   if (t[0].tz_localize("UTC") if t[0].tzinfo is None
                       else t[0].tz_convert("UTC")) >= CUTOFF]
            n = len(sub)
            if n:
                gross = sum(p for _, p in sub)
                rows.append({**rec, "n30": n,
                             "wr30": round(sum(1 for _, p in sub if p > 0) / n * 100, 1),
                             "net30": round((gross - cost * n) * 100, 2)})
            else:
                rows.append({**rec, "n30": 0})
        except Exception:
            rows.append({**rec, "n30": 0})
        if (i + 1) % 60 == 0:
            print(f"[bt30] combos {i+1}/{len(defs)}")
    return rows


def spy_gaps():
    d = dl("SPY", "1d", "90d")
    if d is None:
        return {}
    pc = d["Close"].shift(1)
    return {ts.date(): float(v) for ts, v in ((d["Open"] - pc) / pc * 100).dropna().items()}


def nifty_gaps():
    d = dl("^NSEI", "1d", "90d")
    if d is None:
        return {}
    pc = d["Close"].shift(1)
    return {ts.date(): float(v) for ts, v in ((d["Open"] - pc) / pc * 100).dropna().items()}


def intraday_fade_style(variants, universe_file, ind_fn, min_price, gaps,
                        index_tz_min, direction="SHORT", suffix="", win_table=None):
    """Faithful replay of US_FADE / LONG_BOUNCE style variants.
    Entry at NEXT bar open after signal bar; SL/TP walk; max-hold close."""
    WT = win_table if win_table is not None else WINDOWS
    syms = [str(s).strip() for s in pd.read_csv(universe_file).iloc[:, 0].tolist()]
    if suffix:
        syms = [s if s.endswith(suffix) else s + suffix for s in syms]
    syms = [s for s in syms if s]
    iv = variants[0]["interval"]
    bm = BAR_MINUTES.get(iv, 5)

    data = {}
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(dl, s, iv, "60d"): s for s in syms}
        done = 0
        for fu in as_completed(futs):
            df = fu.result()
            if df is not None and len(df) > 40:
                try:
                    data[futs[fu]] = ind_fn(df)
                except Exception:
                    pass
            done += 1
            if done % 40 == 0:
                print(f"[bt30] {os.path.basename(universe_file)} {done}/{len(syms)}")

    out = []
    for v in variants:
        dur_bars = max(1, int(np.ceil(v.get("dur_min", v.get("drop_dur", 90)) / bm)))
        hold_bars = int(np.ceil(6 * 60 / bm))
        wa, wb = WT.get(v.get("win", "0930_1500"), (570, 900))
        shoot_cache = {}
        for tk, df in data.items():
            busy_until = -1
            day_count = {}
            if direction == "SHORT":
                ck = (tk, v["dur_min"])
                if ck not in shoot_cache:
                    shoot_cache[ck] = _shoot_series(df, v["dur_min"], iv)
                sig = shoot_cache[ck]
            else:
                sig = _drop_series(df, v["dur_min"], iv)
            c = df["Close"].values; o = df["Open"].values
            h = df["High"].values; l = df["Low"].values
            vola = df.get("VolAvg20"); rsi = df.get("RSI14")
            bw = df.get("BelowVWAP"); vd = df.get("VWAPDown")
            idx = df.index
            for i in range(len(df)):
                if i <= busy_until or i + 1 >= len(idx):
                    continue
                ts = idx[i]
                um = index_tz_min(ts)
                if not (wa <= um < wb):
                    continue
                sv = float(sig.iloc[i]) if hasattr(sig, "iloc") else float(sig[i])
                thr = v["shoot_pct"] if direction == "SHORT" else v["drop_pct"]
                if not np.isfinite(sv) or sv < thr:
                    continue
                if direction == "SHORT" and float(c[i]) < min_price:
                    continue
                if direction == "LONG" and float(c[i]) < min_price:
                    continue
                va = vola.iloc[i] if vola is not None else np.nan
                if not (np.isfinite(va) and va > 0 and float(df["Volume"].iloc[i]) >= va * v["vol_mult"]):
                    continue
                rv = rsi.iloc[i] if rsi is not None else np.nan
                if direction == "SHORT":
                    if not (np.isfinite(rv) and rv >= v["rsi_min"]):
                        continue
                else:
                    if not (np.isfinite(rv) and rv <= v["rsi_max"]):
                        continue
                if v.get("vwap") and (bw is None or not bool(bw.iloc[i])):
                    continue
                if v.get("vwap_down") and (vd is None or not bool(vd.iloc[i])):
                    continue
                gm = v.get("gap_max")
                if gm is not None:
                    g = gaps.get(ts.date(), np.nan)
                    if not np.isfinite(g) or abs(g) > gm:
                        continue
                key = (v["rank"], ts.date())
                if day_count.get(key, 0) >= v.get("max_per_day", 2):
                    continue
                entry = float(o[i + 1])          # NEXT bar open fill
                if direction == "SHORT":
                    sl = entry * (1 + v["sl_pct"]); tp = entry * (1 - v["tp_pct"])
                else:
                    sl = entry * (1 - v["sl_pct"]); tp = entry * (1 + v["tp_pct"])
                exit_px, exit_i, label = None, None, "TIME"
                last_j = min(len(df) - 1, i + 1 + hold_bars)
                for j in range(i + 2, last_j + 1):
                    if direction == "SHORT":
                        if h[j] >= sl: exit_px, exit_i, label = sl, j, "SL"; break
                        if l[j] <= tp: exit_px, exit_i, label = tp, j, "TP"; break
                    else:
                        if l[j] <= sl: exit_px, exit_i, label = sl, j, "SL"; break
                        if h[j] >= tp: exit_px, exit_i, label = tp, j, "TP"; break
                if exit_px is None:
                    exit_px, exit_i = c[last_j], last_j
                sign = 1 if direction == "LONG" else -1
                cost = CHARGES_PER_MARKET["US" if ".NS" not in tk else "INDIAN"]
                pnl = ((exit_px - entry) / entry * sign) - cost
                busy_until = exit_i
                day_count[key] = day_count.get(key, 0) + 1
                out.append({"family": ("US_FADE" if direction == "SHORT" else "LONG_BOUNCE"),
                            "name": f"{v['key']}", "ticker": tk,
                            "entry_ts": str(ts), "exit": label,
                            "ret_pct": round(pnl * 100, 3)})
    # keep only entries within window
    keep = []
    for r in out:
        t = pd.Timestamp(r["entry_ts"])
        t = t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")
        if t >= CUTOFF:
            keep.append(r)
    return keep


def main():
    all_rows = []

    # 1) combos ----------------------------------------------------------
    combo_rows = combo_trades_last30()
    for r in combo_rows:
        all_rows.append({"family": r["family"], "name": r["name"],
                         "n": r.get("n30", 0),
                         "wr": r.get("wr30", np.nan),
                         "net": r.get("net30", 0.0)})

    # 2) IPO -------------------------------------------------------------
    from scanner_ipo import load_ipo_universe
    uni = load_ipo_universe()
    ipo_rows = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(dl, u["ticker"], "1d", "730d"): u for u in uni}
        for fu in as_completed(futs):
            u = futs[fu]
            try:
                df = fu.result()
            except Exception:
                df = None
            if df is None or len(df) < 3:
                continue
            for t in ipo_replay(u["strategy"], df, u["ticker"]):
                if pd.Timestamp(t["entry_date"]) >= CUTOFF.tz_localize(None):
                    ipo_rows.append({"family": "IPO_" + t["variant"], "name": t["variant"],
                                     "ret_pct": t["ret_pct"]})
    for r in ipo_rows:
        all_rows.append({"family": r["family"], "name": r["name"], "n": 1,
                         "wr": 100.0 if r["ret_pct"] > 0 else 0.0,
                         "net": r["ret_pct"]})

    # 3) US_FADE + LONG_BOUNCE -------------------------------------------
    us_rows = intraday_fade_style(US_FADE_VARIANTS, "data/us_fade_universe.csv",
                                  compute_us_fade_indicators, US_FADE_MIN_PRICE,
                                  spy_gaps(), lambda ts: ts.hour * 60 + ts.minute,
                                  direction="SHORT", suffix="", win_table=WIN_US)
    lb_rows = intraday_fade_style(LONG_BOUNCE_VARIANTS, "data/nse_fade_universe.csv",
                                  compute_long_indicators, LONG_BOUNCE_MIN_PRICE,
                                  nifty_gaps(), lambda ts: (ts.hour * 60 + ts.minute - 330) % 1440,
                                  direction="LONG", suffix=".NS")
    for src in (us_rows, lb_rows):
        df = pd.DataFrame(src)
        if len(df):
            for name, s in df.groupby("name"):
                all_rows.append({"family": s.family.iloc[0], "name": f"{name}",
                                 "n": len(s), "wr": round((s.ret_pct > 0).mean() * 100, 1),
                                 "net": round(s.ret_pct.sum(), 2)})

    # ---------------- summary ----------------
    det = pd.DataFrame(all_rows)
    det.to_csv(OUT, index=False)
    fam = det.groupby("family").agg(strategies=("name", "count"),
                                    traded=("n", "sum"),
                                    net=("net", "sum")).round(1)
    print(f"\n{'='*74}\n  30-DAY BACKTEST - ACTIVE STRATEGIES\n{'='*74}")
    print(fam.to_string())
    tot_n = int(det.n.sum()); tot_net = float(det.net.sum())
    print(f"\nTOTAL: {tot_n} trades entered in last 30 days | combined net {tot_net:+.1f}%")
    print("NOTE: GAP_DOWN (2 defs) skipped - needs 1m-data engine, not replayable here.")
    print("      FADE (40) excluded - paused.")
    print("      % = sum of per-trade returns after charges (not Rs).")
    print(f"saved -> {OUT}")


if __name__ == "__main__":
    main()

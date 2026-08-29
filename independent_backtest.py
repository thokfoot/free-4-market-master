"""
INDEPENDENT BACKTEST — all quantified strategies (196: 159 SWING_1d + 37 INTRADAY_1h).

Fully self-contained engine: indicators (SMA/EMA/Wilder-RSI14/Range/Ret/2Red),
factor parsing, and the walk-forward trade loop are implemented HERE from scratch
(pandas/numpy) — they do NOT call strategy_miner / scanner backtest code.

Only non-backtest pieces are reused:
  - scanner.get_yf_ticker  (CSV market name -> yfinance ticker mapping)
  - config constants       (SL/TP/MaxHold, region charges) — the live contract

Execution model (no look-ahead):
  - signal at bar i uses indicators defined on bars <= i
  - ENTRY convention A (parity):  fill at close of signal bar i  (matches live
    post-close scanning + the mined claim)
  - ENTRY convention B (honest):  fill at OPEN of bar i+1, and skip a signal if
    the previous position is still open (no overlapping trades)
  - EXIT: SL/TP first-touch over the next `hold` bars (same-bar SL priority),
    else close at the last hold bar. Swing SL 2% TP 4% hold 5d; intraday uses
    per-region SL/TP/hold from config.
  - COST: region round-turn charge; TAX: 25% on net profit (same as repo).

For each strategy it also reports the OUT-OF-SAMPLE run (last 365d swing /
240d intraday), independent of the mined claim.

Run:  python independent_backtest.py
"""
import os, io, sys, time, re, warnings, json
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

from config import (CHARGES_PER_MARKET, SL_PCT, TP_PCT, MAX_HOLD_DAYS,
                    INTRADAY_SL_PCT, INTRADAY_TP_PCT, INTRADAY_MAX_HOLD_HOURS,
                    get_region)
from scanner import get_yf_ticker

HERE = os.path.dirname(os.path.abspath(__file__))
CACHE = os.path.join(HERE, "data", "indbt_cache")
os.makedirs(CACHE, exist_ok=True)
OUT_CSV = os.path.join(HERE, "logs", "independent_backtest_results.csv")
OUT_JSON = os.path.join(HERE, "logs", "independent_backtest_summary.json")

TAX_PCT = 0.25
OOS_DAYS = {"SWING_1d": 365, "INTRADAY_1h": 240}
WINDOW = 0  # 0 = full history; >0 = only entries within last N calendar days
REGION_NORM = {"India": "INDIAN", "INDIAN": "INDIAN", "US": "US",
               "Crypto": "CRYPTO", "CRYPTO": "CRYPTO"}

# ======================================================================
# 1. Indicators — independent implementation
# ======================================================================
def add_indicators(df):
    if df is None or len(df) < 60:
        return None
    df = df.copy()
    c, h, l, o = df["Close"], df["High"], df["Low"], df["Open"]
    df["SMA20"] = c.rolling(20).mean()
    df["SMA50"] = c.rolling(50).mean()
    df["EMA9"]  = c.ewm(span=9,  adjust=False).mean()
    df["EMA20"] = c.ewm(span=20, adjust=False).mean()
    df["EMA50"] = c.ewm(span=50, adjust=False).mean()
    delta = c.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI14"] = 100 - 100 / (1 + rs)
    df["Range"] = (h - l) / c
    df["Ret"] = c.pct_change(fill_method=None)
    df["2Red"] = (df["Ret"] < 0) & (df["Ret"].shift(1) < 0)
    return df


# ======================================================================
# 2. Factor parsing — independent
# ======================================================================
_COL = {"Price": "Close", "Close": "Close", "Open": "Open", "High": "High",
        "Low": "Low", "SMA20": "SMA20", "SMA50": "SMA50", "EMA9": "EMA9",
        "EMA20": "EMA20", "EMA50": "EMA50", "RSI": "RSI14", "RSI14": "RSI14",
        "Range": "Range", "Ret": "Ret"}

def _resolve(df, token):
    token = token.strip()
    if token.endswith("%"):
        try:
            return float(token.rstrip("%")) / 100.0
        except Exception:
            return None
    try:
        return float(token)
    except Exception:
        pass
    col = _COL.get(token)
    if col and col in df.columns:
        return df[col].astype(float)
    return None

def factor_series(df, factor):
    """Return np.bool array (len(df)) where the factor is TRUE (NaN -> False)."""
    if factor.strip() == "2Red":
        return df["2Red"].fillna(False).astype(bool).values
    m = re.match(r"^([A-Za-z0-9_.%]+)([<>])(.+)$", factor.strip())
    if not m:
        return None
    lv = _resolve(df, m.group(1))
    rv = _resolve(df, m.group(3))
    if lv is None or rv is None:
        return None
    try:
        if m.group(2) == "<":
            return (lv < rv).fillna(False).values
        return (lv > rv).fillna(False).values
    except Exception:
        return None


# ======================================================================
# 3. Walk-forward trade loop — no look-ahead
# ======================================================================
def run_trades(df, signal, direction, sl, tp, hold, entry_mode):
    """signal: np.bool array. entry_mode 'close' (fill at bar i close) or
    'open' (fill at bar i+1 open, skip while previous position open).
    Returns list of (entry_dt, pnl)."""
    n = len(df)
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    ope = df["Open"].values
    ts = df.index
    idxs = np.where(signal)[0]
    trades = []
    last_exit = -1          # bar index where the last position ended (open mode)
    for i in idxs:
        if i + 1 >= n:
            continue
        if last_exit >= 0 and i <= last_exit:
            continue        # previous position still open -> no overlapping
        last_j = min(n - 1, i + hold)
        if last_j <= i:
            continue
        entry = close[i] if entry_mode == "close" else ope[i + 1]
        if not np.isfinite(entry) or entry <= 0:
            continue
        exit_px = None
        if direction == "LONG":
            slp, tpp = entry * (1 - sl), entry * (1 + tp)
            for j in range(i + 1, last_j + 1):
                if low[j] <= slp:
                    exit_px, last_exit = slp, j
                    break
                if high[j] >= tpp:
                    exit_px, last_exit = tpp, j
                    break
        else:
            slp, tpp = entry * (1 + sl), entry * (1 - tp)
            for j in range(i + 1, last_j + 1):
                if high[j] >= slp:
                    exit_px, last_exit = slp, j
                    break
                if low[j] <= tpp:
                    exit_px, last_exit = tpp, j
                    break
        if exit_px is None:
            exit_px, last_exit = close[last_j], last_j
        if not np.isfinite(exit_px) or exit_px <= 0:
            continue
        pnl = (exit_px - entry) / entry if direction == "LONG" else (entry - exit_px) / entry
        trades.append((ts[i], pnl))
    return trades


# ======================================================================
# 4. Stats
# ======================================================================
def stats(trades, cost_rt, cutoff=None):
    if not trades:
        return None
    sub = [t for t in trades if t[0] >= cutoff] if cutoff is not None else trades
    if not sub:
        return None
    cnt = len(sub)
    wins = sum(1 for _, p in sub if p > 0)
    gross = sum(p for _, p in sub)
    net = gross - cost_rt * cnt
    net_tax = net * (1 - TAX_PCT) if net > 0 else net
    return {"trades": cnt, "win": wins / cnt * 100,
            "netpct": net * 100, "nettax": net_tax * 100}


def st(trades, cost_rt, cutoff=None):
    s = stats(trades, cost_rt, cutoff)
    if s is None:
        return (0, 0.0, 0.0, 0.0)
    return (s["trades"], round(s["win"], 2), round(s["netpct"], 2), round(s["nettax"], 2))


# ======================================================================
# 5. Data loading (cached)
# ======================================================================
_cache = {}
_FIELDS = {"Open", "High", "Low", "Close", "Volume"}
def _normalize(df):
    if isinstance(df.columns, pd.MultiIndex):
        l0 = [str(x) for x in df.columns.get_level_values(0)]
        l1 = [str(x) for x in df.columns.get_level_values(1)]
        if set(l0) & _FIELDS:
            df.columns = l0
        elif set(l1) & _FIELDS:
            df.columns = l1
    keep = [c for c in _FIELDS if c in df.columns]
    if not keep:
        return None
    df = df[keep].dropna(subset=["Close"])
    df = df[~df.index.duplicated(keep="last")].sort_index()
    return df

def load_bars(yft, interval, refresh=False):
    key = (yft, interval)
    if key in _cache and not refresh:
        return _cache[key]
    safe = re.sub(r"[^A-Za-z0-9_.]", "_", yft)
    path = os.path.join(CACHE, f"{safe}.{interval}.csv")
    if os.path.exists(path):
        # fresh run: drop the cached file so the download below replaces it
        if refresh:
            try:
                os.remove(path)
            except Exception:
                pass
        else:
            try:
                df = pd.read_csv(path, index_col=0, parse_dates=True)
                if df is not None and len(df) > 50:
                    _cache[key] = df
                    return df
            except Exception:
                pass
    period = "5y" if interval == "1d" else "730d"
    df = None
    for _ in range(3):
        try:
            d = yf.download(yft, period=period, interval=interval,
                            progress=False, auto_adjust=False, threads=False)
            if d is not None and not d.empty:
                df = _normalize(d.copy())
                if df is not None and len(df) > 50:
                    break
        except Exception:
            time.sleep(1)
    if df is None or len(df) <= 50:
        _cache[key] = None
        return None
    tmp = path + ".tmp"
    df.to_csv(tmp)
    os.replace(tmp, path)          # atomic write (thread-safe)
    _cache[key] = df
    return df


# ======================================================================
# 6. Run
# ======================================================================
def build_rows(tf, path):
    df = pd.read_csv(os.path.join(HERE, path))
    rows = []
    for _, r in df.iterrows():
        rows.append({
            "rank": int(r["Final_Rank"]), "market": str(r["Market"]),
            "region": REGION_NORM.get(str(r["Region"]).strip(), None),
            "tf": tf, "direction": str(r["Direction"]).strip().upper(),
            "factors": str(r["Factors"]).strip(),
            "claimed_win": r.get("AvgWin%", ""), "claimed_trades": r.get("Trades", ""),
            "claimed_net": r.get("Net_TotalPnL%_After_Tax", ""),
        })
    return rows


def main():
    global WINDOW
    args = sys.argv[1:]
    refresh = "--refresh" in args
    if "--window" in args:
        WINDOW = int(args[args.index("--window") + 1])
    strategies = build_rows("SWING_1d", "data/strategies.csv") + \
                 build_rows("INTRADAY_1h", "data/intraday_strategies.csv")
    print(f"[IndBT] {len(strategies)} strategies  refresh={refresh}  window={WINDOW}d")

    # Serial pre-warm: one clean download per unique (ticker, interval), then
    # the threaded analysis below only reads the cache — no download races.
    warm = {}
    for s in strategies:
        yft = get_yf_ticker(s["market"])
        if not yft:
            continue
        interval = "1d" if s["tf"] == "SWING_1d" else "1h"
        if (yft, interval) not in warm:
            warm[(yft, interval)] = True
    if refresh:
        for i, (yft, interval) in enumerate(sorted(warm)):
            load_bars(yft, interval, refresh=True)
            if (i + 1) % 10 == 0:
                print(f"[IndBT] fresh download {i + 1}/{len(warm)} ...")

    results = []

    def process(idx, s):
        yft = get_yf_ticker(s["market"])
        if yft is None:
            return (idx, s, None)
        region = s["region"] or get_region(yft)
        interval = "1d" if s["tf"] == "SWING_1d" else "1h"
        try:
            df = load_bars(yft, interval, refresh=False)
            dfi = add_indicators(df)
        except Exception as e:
            return (idx, s, {"skip": "err:" + str(e)[:60]})
        if dfi is None or len(dfi) < 60:
            return (idx, s, None)
        # signal
        sig = np.ones(len(dfi), dtype=bool)
        for f in s["factors"].split("+"):
            fs = factor_series(dfi, f)
            if fs is None:
                return (idx, s, None)
            sig &= fs
        if not sig.any():
            return (idx, s, {"skip": "no signals"})
        # params
        if s["tf"] == "SWING_1d":
            sl, tp, hold = SL_PCT, TP_PCT, MAX_HOLD_DAYS
        else:
            sl = INTRADAY_SL_PCT.get(region, 0.01)
            tp = INTRADAY_TP_PCT.get(region, 0.02)
            hold = int(INTRADAY_MAX_HOLD_HOURS.get(region, 6))
        cost = CHARGES_PER_MARKET.get(region, 0.001)
        cutoff = dfi.index.max() - pd.Timedelta(days=OOS_DAYS[s["tf"]])
        ca = run_trades(dfi, sig, s["direction"], sl, tp, hold, "close")
        ob = run_trades(dfi, sig, s["direction"], sl, tp, hold, "open")
        # window mode: fresh-start, entries only within last WINDOW days
        if WINDOW:
            wc = dfi.index.max() - pd.Timedelta(days=WINDOW)
            okm = dfi.index >= wc
            start_i = int(okm.argmax()) if okm.any() else len(dfi)
            sw = sig.copy()
            sw[:start_i] = False
            cutoff = wc
            if sw.any():
                wca = run_trades(dfi, sw, s["direction"], sl, tp, hold, "close")
                wob = run_trades(dfi, sw, s["direction"], sl, tp, hold, "open")
            else:
                wca = []; wob = []
        else:
            wca = ca; wob = ob
        ca_t, ca_w, ca_n, ca_nt = st(ca, cost)
        ca_o_t, ca_o_w, ca_o_n, ca_o_nt = st(ca, cost, cutoff)
        ob_t, ob_w, ob_n, ob_nt = st(ob, cost)
        ob_o_t, ob_o_w, ob_o_n, ob_o_nt = st(ob, cost, cutoff)
        w_t, w_w, w_n, w_nt = st(wca, cost) if WINDOW else (0, 0.0, 0.0, 0.0)
        wo_t, wo_w, wo_n, wo_nt = st(wob, cost) if WINDOW else (0, 0.0, 0.0, 0.0)
        return (idx, s, {
            "yft": yft, "region": region, "df_days": int((dfi.index.max() - dfi.index.min()).days),
            "ca_t": ca_t, "ca_w": ca_w, "ca_n": round(ca_n, 2), "ca_nt": round(ca_nt, 2),
            "ca_o_t": ca_o_t, "ca_o_w": ca_o_w, "ca_o_n": round(ca_o_n, 2), "ca_o_nt": round(ca_o_nt, 2),
            "ob_t": ob_t, "ob_w": ob_w, "ob_n": round(ob_n, 2), "ob_nt": round(ob_nt, 2),
            "ob_o_t": ob_o_t, "ob_o_w": ob_o_w, "ob_o_n": round(ob_o_n, 2), "ob_o_nt": round(ob_o_nt, 2),
            "w_t": w_t, "w_w": w_w, "w_n": round(w_n, 2), "w_nt": round(w_nt, 2),
            "wo_t": wo_t, "wo_w": wo_w, "wo_n": round(wo_n, 2), "wo_nt": round(wo_nt, 2),
        })

    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(process, i, s): i for i, s in enumerate(strategies)}
        done = 0
        for f in as_completed(futs):
            done += 1
            results.append(f.result())
            if done % 30 == 0:
                print(f"[IndBT] {done}/{len(strategies)} ...")
    results.sort(key=lambda x: x[0])

    out = []
    for idx, s, res in results:
        has_res = res is not None and "skip" not in res
        row = {
            "TF": s["tf"], "Rank": s["rank"], "Market": s["market"],
            "Ticker": res["yft"] if has_res else "",
            "Region": res["region"] if has_res else "",
            "Direction": s["direction"], "Factors": s["factors"][:44],
            "Claimed_Win%": s["claimed_win"], "Claimed_Trades": s["claimed_trades"],
            "Claimed_Net%_AfterTax": s["claimed_net"],
        }
        if res is None:
            row["Status"] = "SKIP"; row["Verdict"] = "SKIP"
            out.append(row); continue
        if "skip" in res:
            row["Status"] = "NO_SIGNALS"; row["Verdict"] = "NO_SIGNALS"
            out.append(row); continue
        row.update({
            "Data_Days": res["df_days"],
            "CL_Trades": res["ca_t"], "CL_Win%": res["ca_w"], "CL_Net%": res["ca_n"], "CL_TaxNet%": res["ca_nt"],
            "CL_OOS_Trades": res["ca_o_t"], "CL_OOS_Win%": res["ca_o_w"], "CL_OOS_Net%": res["ca_o_n"],
            "OP_Trades": res["ob_t"], "OP_Win%": res["ob_w"], "OP_Net%": res["ob_n"], "OP_TaxNet%": res["ob_nt"],
            "OP_OOS_Trades": res["ob_o_t"], "OP_OOS_Win%": res["ob_o_w"], "OP_OOS_Net%": res["ob_o_n"],
            "W_OP_Trades": res["wo_t"], "W_OP_Win%": res["wo_w"], "W_OP_Net%": res["wo_n"], "W_OP_TaxNet%": res["wo_nt"],
            "W_CL_Trades": res["w_t"], "W_CL_Win%": res["w_w"], "W_CL_Net%": res["w_n"], "W_CL_TaxNet%": res["w_nt"],
        })
        ob_net = res["ob_n"]
        ob_os_net = res["ob_o_n"]
        if ob_net <= 0:
            verdict = "FAIL"
        elif ob_os_net > 0:
            verdict = "PASS"
        else:
            verdict = "DEGRADE"
        row["Verdict"] = verdict
        row["Status"] = "OK"
        out.append(row)

    rdf = pd.DataFrame(out)
    rdf.to_csv(OUT_CSV, index=False)
    print(f"[IndBT] results -> {OUT_CSV}")

    if WINDOW:
        rw = rdf[rdf["Status"] == "OK"].copy().reset_index(drop=True)
        for c in ["W_OP_Trades", "W_OP_Win%", "W_OP_Net%", "W_OP_TaxNet%",
                  "W_CL_Trades", "W_CL_Win%", "W_CL_Net%", "W_CL_TaxNet%"]:
            rw[c] = pd.to_numeric(rw[c], errors="coerce")
        tot_trades = int(rw["W_OP_Trades"].sum())
        tot_net = round(rw["W_OP_TaxNet%"].sum(), 2)
        tot_cl = round(rw["W_CL_TaxNet%"].sum(), 2)
        pos = int((rw["W_OP_TaxNet%"] > 0).sum())
        wcl = pd.DataFrame({
            "TF": rw["TF"], "Rank": rw["Rank"], "Market": rw["Market"],
            "Direction": rw["Direction"], "Factors": rw["Factors"],
            "CL_Trades": rw["W_CL_Trades"], "CL_Win%": rw["W_CL_Win%"],
            "CL_TaxNet%": rw["W_CL_TaxNet%"], "OP_Trades": rw["W_OP_Trades"],
            "OP_Win%": rw["W_OP_Win%"], "OP_TaxNet%": rw["W_OP_TaxNet%"],
        })
        out30 = os.path.join(HERE, "logs", "independent_backtest_30d.csv")
        wcl.to_csv(out30, index=False)
        print(f"\n[IndBT] === 30-DAY WINDOW (last {WINDOW}d) ===")
        print(f"  total trades (OP): {tot_trades} | net tax OP: {tot_net}% | close-entry: {tot_cl}% | positive strats: {pos}/{len(rw)}")
        for tf in ("SWING_1d", "INTRADAY_1h"):
            g = rw[rw["TF"] == tf]
            print(f"  {tf}: {int(g['W_OP_Trades'].sum()):>4} trades | "
                  f"net {round(g['W_OP_TaxNet%'].sum(), 2):>9}% | "
                  f"mean/strat {round(g['W_OP_TaxNet%'].mean(), 2):>7} | positive {int((g['W_OP_TaxNet%']>0).sum())}/{len(g)}")
        print(f"[IndBT] 30d rows -> {out30}")

    summary = {}
    for tf in ("SWING_1d", "INTRADAY_1h"):
        sub = rdf[rdf["TF"] == tf]
        ok = sub[sub["Status"] == "OK"]
        vc = ok["Verdict"].value_counts().to_dict()
        simp = {"total": len(sub), "ok": len(ok), **vc}
        sim_claimed = None
        sim_obs = None
        if len(ok):
            sim_claimed = round(pd.to_numeric(ok["Claimed_Net%_AfterTax"], errors="coerce").mean(), 2)
            sim_obs = round(pd.to_numeric(ok["OP_TaxNet%"], errors="coerce").mean(), 2)
        simp["mean_claimed_net%"] = sim_claimed
        simp["mean_independent_net%"] = sim_obs
        summary[tf] = simp
        print(f"\n=== {tf}: {len(sub)} strategies, {len(ok)} with trades ===")
        print(f"  Verdicts: {vc}")
        print(f"  Mean claimed Net%_afterTax: {sim_claimed}  |  mean independent OP_TaxNet%: {sim_obs}")
        worst = ok[pd.to_numeric(ok['OP_TaxNet%'], errors='coerce') < 0].sort_values('OP_TaxNet%').head(8)
        if len(worst):
            print("  Worst (claimed vs observed):")
            for _, rw in worst.iterrows():
                print(f"    {rw['Market']:14s} {rw['Direction']:5s} {rw['Factors'][:38]:40s} "
                      f"claimed {rw['Claimed_Net%_AfterTax']:>8}  observed {rw['OP_TaxNet%']:>8}")
    with open(OUT_JSON, "w") as f:
        json.dump(summary, f, indent=2)
    print(f"\n[IndBT] summary -> {OUT_JSON}")


if __name__ == "__main__":
    main()
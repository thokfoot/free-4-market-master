"""
FREE 3-Market — STRATEGY MINER v2 (cross-market validated)
==========================================================
Brute-force factor combos across ALL markets (India / US / Crypto) and
backtest each combo on the ENTIRE universe with the bot-identical execution
model. A combo only qualifies if it generalizes: profitable on >= N markets,
positive out-of-sample, and strong aggregate stats. Per-market rows for the
qualifying combos are then appended to the strategy CSVs so the paper-trading
bot picks them up automatically.

Run:
    python strategy_miner.py                # swing + intraday, appends verified
    python strategy_miner.py --tf swing     # daily swing only
    python strategy_miner.py --tf intraday  # 1h intraday only
    python strategy_miner.py --dry-run      # backtest + report only, no CSV write
    python strategy_miner.py --refresh      # force re-download of data cache
    python strategy_miner.py --top-combos 60 --top-per-market 5

Execution model matches bot.py / paper_trader.py:
    - data: yfinance raw prices (auto_adjust=False), 5y daily / 2y 1h
    - indicators: scanner.compute_indicators / compute_indicators_1h
      (SMA/EMA adjust=False, Wilder RSI) — same as live scanner
    - entry: close of signal candle
    - exit: SL/TP hit intra-hold, else close at max-hold candle
      (swing: SL 2% TP 4% hold 5d; intraday: per-market SL/TP/hold)
    - cost: CHARGES_PER_MARKET round-turn; tax: 25% on net profit
"""

import os
import re
import sys
import time
import argparse
import warnings
from concurrent.futures import ThreadPoolExecutor, as_completed

warnings.filterwarnings("ignore")

import numpy as np
import pandas as pd
import yfinance as yf

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from config import (
    TICKER_MAP, CHARGES_PER_MARKET, SL_PCT, TP_PCT, MAX_HOLD_DAYS,
    INTRADAY_SL_PCT, INTRADAY_TP_PCT, INTRADAY_MAX_HOLD_HOURS,
    STRATEGY_FILE, INTRADAY_STRATEGY_FILE, ALLOW_SHORT, get_region,
)
from scanner import compute_indicators
from scanner_intraday import compute_indicators_1h

CACHE_DIR = os.path.join(HERE, "data", "miner_cache")
RESULTS_FILE = os.path.join(HERE, "logs", "miner_results_all.csv")
MIN_5Y_DAILY = "5y"           # swing backtest period
INTRADAY_1H_PERIOD = "730d"   # yfinance max for 1h bars (~2y)

TAX_PCT = 0.25
OOS_DAYS_SWING = 365          # last 1y of 5y = out-of-sample
OOS_DAYS_INTRADAY = 240       # last 8 months of 2y = out-of-sample

# ── Factor vocabulary (all parseable by scanner compute_signal/_resolve_value) ──
STRUCT = [
    "Price>SMA20", "Price<SMA20", "Price>SMA50", "Price<SMA50",
    "Price>EMA9", "Price<EMA9", "Price>EMA20", "Price<EMA20",
    "Price>EMA50", "Price<EMA50",
    "SMA20>SMA50", "SMA20<SMA50",
    "EMA9>EMA20", "EMA9<EMA20", "EMA20>EMA50", "EMA20<EMA50",
    "EMA9>EMA50", "EMA9<EMA50",
    "SMA20>EMA20", "SMA20<EMA20",
    "Close>Open", "Close<Open",
]

NONCORE = [
    "RSI>50", "RSI<50", "RSI>60", "RSI<40", "RSI>70", "RSI<30",
    "RSI>65", "RSI<45",
    "Range>1%", "Range>1.5%", "Range>2%", "Range>2.5%", "Range<1%",
    "Ret>0", "Ret>1%", "Ret>2%", "Ret>3%", "Ret<0", "Ret<-1%", "Ret<-2%",
    "2Red",
]

BULL_PRICE = ["Price>SMA20", "Price>SMA50", "Price>EMA9", "Price>EMA20", "Price>EMA50"]
BULL_TREND = ["SMA20>SMA50", "EMA9>EMA20", "EMA20>EMA50", "SMA20>EMA20"]
BEAR_PRICE = ["Price<SMA20", "Price<SMA50", "Price<EMA9", "Price<EMA20", "Price<EMA50"]
BEAR_TREND = ["SMA20<SMA50", "EMA9<EMA20", "EMA20<EMA50", "SMA20<EMA20"]

COL_MAP = {
    "Price": "Close", "Close": "Close", "Open": "Open", "High": "High",
    "Low": "Low", "Volume": "Volume", "SMA20": "SMA20", "SMA50": "SMA50",
    "EMA9": "EMA9", "EMA20": "EMA20", "EMA50": "EMA50",
    "RSI": "RSI14", "RSI14": "RSI14", "Range": "Range", "Ret": "Ret",
}

REGION_LABEL = {"US": "US", "CRYPTO": "Crypto", "INDIAN": "India"}
INDICATORS_DOC = ("SMA20=Close.rolling(20).mean(), SMA50=rolling(50).mean(), "
                  "EMA9/20/50=ewm(span,adjust=False), RSI14=Wilder alpha=1/14 adjust=False, "
                  "Range=(High-Low)/Close, Ret=pct_change, 2Red=2 consecutive down candles")
COST_DOC = {
    "US": "0.02% RT (0 commission + SEC/FINRA + exchange + slippage 0.01%)",
    "CRYPTO": "0.30% RT (Binance 0.1% per side + slippage 0.05% per side)",
    "INDIAN": "0.12% RT (brokerage + STT + exchange + GST + stamp + slippage)",
}

# Per-ticker emit gates per timeframe
GATES = {
    "SWING_1d": dict(min_trades=30, min_win=45.0,
                     min_oos_trades=5, min_oos_net=0.0),
    "INTRADAY_1h": dict(min_trades=80, min_win=47.0,
                        min_oos_trades=12, min_oos_net=0.0),
}
# Combo-level generalization gates per timeframe
COMBO_GATES = {
    "SWING_1d": dict(min_total_trades=120, min_net=20.0, min_n_prof=4,
                     min_oos_trades=15, min_oos_net=0.0),
    "INTRADAY_1h": dict(min_total_trades=300, min_net=10.0, min_n_prof=4,
                        min_oos_trades=40, min_oos_net=0.0),
}


def canonical(factors_list):
    return "+".join(sorted(f.strip() for f in factors_list))


def generate_combos():
    combos = set()
    for i, a in enumerate(STRUCT):
        for b in STRUCT[i + 1:]:
            combos.add(tuple(sorted([a, b])))
        for b in NONCORE:
            combos.add(tuple(sorted([a, b])))
    bull_pairs = [(a, b) for a in BULL_PRICE for b in BULL_TREND]
    bear_pairs = [(a, b) for a in BEAR_PRICE for b in BEAR_TREND]
    for p1, p2 in bull_pairs + bear_pairs:
        for n in NONCORE:
            combos.add(tuple(sorted([p1, p2, n])))
    return sorted(combos)


def collect_universe():
    seen = set()
    out = []
    for market, yft in TICKER_MAP.items():
        if yft in seen:
            continue
        seen.add(yft)
        out.append((market, yft, get_region(yft)))
    return out


def normalize_columns(df):
    if df is None:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        l0 = [str(x) for x in df.columns.get_level_values(0)]
        l1 = [str(x) for x in df.columns.get_level_values(1)]
        fields = {"Close", "High", "Low", "Open", "Volume"}
        if set(l0) & fields:
            df.columns = l0
        elif set(l1) & fields:
            df.columns = l1
    for c in ["Adj Close", "Dividends", "Stock Splits", "Capital Gains"]:
        if c in df.columns:
            df = df.drop(columns=[c])
    df = df.loc[:, ~df.columns.duplicated()]
    return df


def _resolve_series(df, expr):
    if expr.endswith("%"):
        try:
            return pd.Series(float(expr.rstrip("%")) / 100.0, index=df.index)
        except Exception:
            return None
    try:
        return pd.Series(float(expr), index=df.index)
    except Exception:
        pass
    col = COL_MAP.get(expr.strip())
    if col and col in df.columns:
        return df[col].astype(float)
    return None


def factor_series(df, factor):
    if factor == "2Red":
        return df["2Red"].fillna(False).astype(bool).values
    m = re.match(r"^([A-Za-z0-9_.%]+)([<>])(.+)$", factor)
    if not m:
        return None
    left, op, right = m.group(1), m.group(2), m.group(3)
    lv = _resolve_series(df, left)
    rv = _resolve_series(df, right)
    if lv is None or rv is None:
        return None
    if op == "<":
        return (lv < rv).values
    return (lv > rv).values


def load_data(yft, interval, refresh=False):
    os.makedirs(CACHE_DIR, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9_.]", "_", yft)
    path = os.path.join(CACHE_DIR, f"{safe}.{interval}.csv")
    if os.path.exists(path) and not refresh:
        try:
            return normalize_columns(pd.read_csv(path, index_col=0, parse_dates=True))
        except Exception:
            pass
    period = MIN_5Y_DAILY if interval == "1d" else INTRADAY_1H_PERIOD
    df = yf.download(yft, period=period, interval=interval,
                     progress=False, auto_adjust=False, threads=False)
    if df is None or df.empty:
        return None
    df = normalize_columns(df)
    df = df[~df.index.duplicated(keep="last")].sort_index()
    df.to_csv(path)
    return df


def prepare_all(universe, tf, refresh, workers):
    """Download + compute indicators + precompute factor boolean arrays."""
    interval = "1h" if tf == "INTRADAY_1h" else "1d"
    min_rows = 200 if tf == "INTRADAY_1h" else 60
    ind_fn = compute_indicators_1h if tf == "INTRADAY_1h" else compute_indicators
    all_factors = sorted(set(STRUCT) | set(NONCORE))

    def prep(row):
        market, yft, region = row
        try:
            df = load_data(yft, interval, refresh)
            if df is None or len(df) < min_rows:
                return None
            df = ind_fn(df)
            if df is None or len(df) < min_rows:
                return None
            facs = {}
            for f in all_factors:
                s = factor_series(df, f)
                if s is not None:
                    facs[f] = s
            return (market, yft, region, df, facs)
        except Exception as e:
            print(f"[Miner] Data prep failed {yft}: {e}")
            return None

    prepared = []
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(prep, r): r for r in universe}
        for f in as_completed(futs):
            r = f.result()
            if r:
                prepared.append(r)
    return prepared


def backtest_one(df, factors, facs, direction, tf, region):
    sig = np.ones(len(df), dtype=bool)
    for f in factors:
        s = facs.get(f)
        if s is None:
            return None
        sig &= s
    idxs = np.where(sig)[0]
    if len(idxs) < 5:
        return []
    n = len(df)
    close = df["Close"].values
    high = df["High"].values
    low = df["Low"].values
    ts = df.index
    if tf == "SWING_1d":
        sl, tp, hold = SL_PCT, TP_PCT, MAX_HOLD_DAYS
    else:
        sl = INTRADAY_SL_PCT.get(region, 0.01)
        tp = INTRADAY_TP_PCT.get(region, 0.02)
        hold = INTRADAY_MAX_HOLD_HOURS.get(region, 6)
    trades = []
    for i in idxs:
        last_j = min(n - 1, i + hold)
        if last_j <= i:
            continue
        entry = close[i]
        exit_price = None
        if direction == "LONG":
            slp = entry * (1 - sl)
            tpp = entry * (1 + tp)
            for j in range(i + 1, last_j + 1):
                if low[j] <= slp:
                    exit_price = slp
                    break
                if high[j] >= tpp:
                    exit_price = tpp
                    break
            if exit_price is None:
                exit_price = close[last_j]
            pnl = (exit_price - entry) / entry
        else:
            slp = entry * (1 + sl)
            tpp = entry * (1 - tp)
            for j in range(i + 1, last_j + 1):
                if high[j] >= slp:
                    exit_price = slp
                    break
                if low[j] <= tpp:
                    exit_price = tpp
                    break
            if exit_price is None:
                exit_price = close[last_j]
            pnl = (entry - exit_price) / entry
        trades.append((ts[i], pnl))
    return trades


def _stats(trades, cost_rt, cutoff=None):
    if not trades:
        return None
    subset = trades if cutoff is None else [t for t in trades if t[0] >= cutoff]
    if not subset:
        return None
    cnt = len(subset)
    wins = sum(1 for _, p in subset if p > 0)
    gross = sum(p for _, p in subset)
    net_chg = gross - cost_rt * cnt
    net_tax = net_chg * (1 - TAX_PCT) if net_chg > 0 else net_chg
    return {
        "Trades": cnt, "Wins": wins, "Win%": round(wins / cnt * 100, 2),
        "Gross%": round(gross * 100, 2), "NetChg%": round(net_chg * 100, 2),
        "NetTax%": round(net_tax * 100, 2),
    }


def evaluate_combo(prepared, factors, direction, tf):
    """Backtest a combo on every ticker; return combo-level + per-market stats."""
    if tf == "SWING_1d":
        oos_days = OOS_DAYS_SWING
    else:
        oos_days = OOS_DAYS_INTRADAY
    combo = {
        "Direction": direction, "Factors": "+".join(factors),
        "Trades": 0, "Wins": 0, "NetChg%": 0.0, "OOSTrades": 0, "OOSNetChg%": 0.0,
        "NProfitable": 0, "NTested": 0, "Markets": [],
    }
    for market, yft, region, df, facs in prepared:
        if direction == "SHORT" and region == "INDIAN":
            continue
        cost_rt = CHARGES_PER_MARKET.get(region, 0.001)
        trades = backtest_one(df, factors, facs, direction, tf, region)
        if trades is None:
            return None
        if not trades:
            continue
        cutoff = df.index.max() - pd.Timedelta(days=oos_days)
        full = _stats(trades, cost_rt)
        oos = _stats(trades, cost_rt, cutoff)
        if full is None:
            continue
        combo["NTested"] += 1
        combo["Trades"] += full["Trades"]
        combo["Wins"] += full["Wins"]
        combo["NetChg%"] += full["NetChg%"]
        combo["OOSTrades"] += (oos or {}).get("Trades", 0)
        combo["OOSNetChg%"] += (oos or {}).get("NetChg%", 0)
        combo["Markets"].append({
            "Market": market, "YFTicker": yft, "Region": region,
            "Trades": full["Trades"], "Win%": full["Win%"],
            "Gross%": full["Gross%"],
            "NetChg%": full["NetChg%"], "NetTax%": full["NetTax%"],
            "OOSTrades": (oos or {}).get("Trades", 0),
            "OOSNetChg%": (oos or {}).get("NetChg%", 0),
        })
    if combo["Trades"] < 5:
        return None
    combo["Win%"] = round(combo["Wins"] / combo["Trades"] * 100, 2)
    net_tax = combo["NetChg%"] * (1 - TAX_PCT) if combo["NetChg%"] > 0 else combo["NetChg%"]
    combo["NetTax%"] = round(net_tax, 2)
    return combo


def run_tf(universe, combos, tf, args):
    t0 = time.time()
    print(f"\n{'='*70}\n  MINING {tf}  ({len(combos)} combos x {len(universe)} tickers)\n{'='*70}")
    prepared = prepare_all(universe, tf, args.refresh, args.workers)
    print(f"[Miner] {tf}: data ready {len(prepared)}/{len(universe)} "
          f"({time.time()-t0:.0f}s)")

    tasks = []
    for factors in combos:
        for direction in ["LONG", "SHORT"]:
            tasks.append((factors, direction))

    combo_results = []
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        futs = {}
        for factors, direction in tasks:
            futs[ex.submit(evaluate_combo, prepared, factors, direction, tf)] = (factors, direction)
        for f in as_completed(futs):
            r = f.result()
            done += 1
            if r and r["NTested"] >= 3:
                combo_results.append(r)
            if done % 500 == 0:
                print(f"[Miner] {tf}: evaluated {done}/{len(tasks)} "
                      f"({time.time()-t0:.0f}s)")
    print(f"[Miner] {tf}: {len(combo_results)} combos evaluated "
          f"({time.time()-t0:.0f}s)")
    return combo_results, prepared


def emit_rows(combo_results, tf, args, existing_keys):
    gates = GATES[tf]
    cg = COMBO_GATES[tf]
    picked_combos = []
    for c in combo_results:
        if c["Trades"] < cg["min_total_trades"]:
            continue
        if c["NetTax%"] < cg["min_net"]:
            continue
        if c["OOSTrades"] < cg["min_oos_trades"]:
            continue
        if c["OOSNetChg%"] <= cg["min_oos_net"]:
            continue
        n_prof = sum(1 for m in c["Markets"]
                     if m["Trades"] >= gates["min_trades"] and m["NetTax%"] > 0)
        if n_prof < cg["min_n_prof"]:
            continue
        if c["Win%"] < gates["min_win"]:
            continue
        picked_combos.append(c)
    picked_combos.sort(key=lambda c: c["NetTax%"], reverse=True)
    picked_combos = picked_combos[:args.top_combos]
    print(f"[Miner] {tf}: {len(combo_results)} combos -> {len(picked_combos)} "
          f"passed generalization gates")

    rows = []
    for c in picked_combos:
        for m in c["Markets"]:
            fac = canonical(c["Factors"].split("+"))
            if (m["Market"], fac, c["Direction"]) in existing_keys:
                continue
            if m["Trades"] < gates["min_trades"]:
                continue
            if m["NetTax%"] <= 0:
                continue
            if m["Win%"] < gates["min_win"]:
                continue
            if m["OOSTrades"] < gates["min_oos_trades"]:
                continue
            if m["OOSNetChg%"] <= gates["min_oos_net"]:
                continue
            rows.append({
                "TF": tf, "Market": m["Market"], "YFTicker": m["YFTicker"],
                "Region": m["Region"], "Direction": c["Direction"],
                "Factors": c["Factors"],
                "ComboNetTax%": c["NetTax%"], "ComboOOS%": c["OOSNetChg%"],
                **m,
            })
    rows.sort(key=lambda r: r["NetTax%"], reverse=True)
    # per-market cap
    kept = {}
    final = []
    for r in rows:
        key = r["Market"]
        if kept.get(key, 0) >= args.top_per_market:
            continue
        kept[key] = kept.get(key, 0) + 1
        final.append(r)
    print(f"[Miner] {tf}: emitted {len(final)} per-market rows "
          f"(cap {args.top_per_market}/market)")
    return final


def build_new_rows(rows, tf, existing_df):
    max_rank = {}
    if existing_df is not None and not existing_df.empty and "Final_Rank" in existing_df.columns:
        for m, g in existing_df.groupby("Market"):
            try:
                max_rank[str(m)] = int(g["Final_Rank"].max())
            except Exception:
                max_rank[str(m)] = 0
    tf_label = "1d_5y" if tf == "SWING_1d" else "1h"
    vfile = "MINER_5y_daily_OOS_OK" if tf == "SWING_1d" else "MINER_2y_1h_OOS_OK"
    if tf == "INTRADAY_1h":
        REGION_LABEL.update({"INDIAN": "INDIAN"})
    new_rows = []
    for r in rows:
        mkt = r["Market"]
        max_rank[mkt] = max_rank.get(mkt, 0) + 1
        new_rows.append({
            "Final_Rank": max_rank[mkt],
            "Market": mkt,
            "Region": REGION_LABEL.get(r["Region"], r["Region"]),
            "TF": tf_label,
            "Factors": r["Factors"],
            "Direction": r["Direction"],
            "AvgWin%": r["Win%"],
            "Trades": r["Trades"],
            "TotalPnL%": r["Gross%"],
            "Cost%_per_trade_RT": round(CHARGES_PER_MARKET.get(r["Region"], 0.001) * 100, 4),
            "Net_TotalPnL%_After_Charges": r["NetChg%"],
            "Tax%": round(TAX_PCT * 100),
            "Net_TotalPnL%_After_Tax": r["NetTax%"],
            "Verified_File": vfile,
            "Indicators_Computation": INDICATORS_DOC,
            "Cost_Documentation": COST_DOC.get(r["Region"], ""),
            "Tax_Documentation": "25% short-term capital gains",
            "Verification_Code_Snippet": "",
            "Verification_Status": "MINED_VECTORIZED_2026-08_OOS_OK",
        })
    df_new = pd.DataFrame(new_rows)
    if tf == "INTRADAY_1h" and not df_new.empty:
        df_new = df_new.rename(columns={
            "Cost%_per_trade_RT": "Cost%_RT",
            "Net_TotalPnL%_After_Charges": "Net_Total%_After_Charges",
            "Cost_Documentation": "Cost_Doc",
        })
        df_new = df_new.drop(columns=[
            "Verification_Code_Snippet", "Tax_Documentation"])
        df_new["Tax%"] = round(TAX_PCT * 100)
    return df_new


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tf", choices=["swing", "intraday", "both"], default="both")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--top-combos", type=int, default=40)
    ap.add_argument("--top-per-market", type=int, default=5)
    args = ap.parse_args()

    universe = collect_universe()
    combos = generate_combos()
    print(f"[Miner] Universe: {len(universe)} tickers | Combos: {len(combos)}")

    order = [("swing", "SWING_1d"), ("intraday", "INTRADAY_1h")]
    for arg_name, tf in order:
        if args.tf != "both" and args.tf != arg_name:
            continue
        combo_results, _ = run_tf(universe, combos, tf, args)

        existing_keys = load_existing_keys(
            STRATEGY_FILE if tf == "SWING_1d" else INTRADAY_STRATEGY_FILE)
        rows = emit_rows(combo_results, tf, args, existing_keys)

        os.makedirs(os.path.dirname(RESULTS_FILE), exist_ok=True)
        if combo_results:
            pd.DataFrame(combo_results).to_csv(
                f"logs/miner_combos_{tf}.csv", index=False)
        if rows:
            pd.DataFrame(rows).to_csv(f"logs/miner_rows_{tf}.csv", index=False)
            print(f"[Miner] {tf}: saved logs/miner_rows_{tf}.csv "
                  f"({len(rows)} rows)")

        cols = ["Market", "Region", "Direction", "Factors", "Trades", "Win%",
                "Gross%", "NetChg%", "NetTax%", "OOSTrades", "OOSNetChg%"]
        if rows:
            show = pd.DataFrame(rows)[cols].head(args.top_per_market * 6)
            print(show.to_string(index=False))
        else:
            print(f"[Miner] {tf}: no rows passed filters")

        if rows and not args.dry_run:
            csv_file = STRATEGY_FILE if tf == "SWING_1d" else INTRADAY_STRATEGY_FILE
            existing_df = pd.read_csv(csv_file, on_bad_lines="warn") \
                if os.path.exists(csv_file) else None
            new_df = build_new_rows(rows, tf, existing_df)
            if not new_df.empty:
                combined = pd.concat([existing_df, new_df], ignore_index=True)
                combined.to_csv(csv_file, index=False)
                print(f"[Miner] Appended {len(new_df)} strategies -> {csv_file}")

    print("\n[Miner] Done.")


def load_existing_keys(filepath):
    keys = set()
    if not os.path.exists(filepath):
        return keys
    df = pd.read_csv(filepath, on_bad_lines="warn")
    if "Factors" not in df.columns:
        return keys
    for _, r in df.iterrows():
        keys.add((str(r.get("Market", "")),
                  canonical(str(r["Factors"]).split("+")),
                  str(r.get("Direction", ""))))
    return keys


if __name__ == "__main__":
    main()

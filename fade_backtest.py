"""Independent 60-day FADE backtest - exact scanner_fade rules, fresh data.

Answers: "FADE strategies claimed profitable when mined (40-45% WR) - are
they actually loss-making in a backtest too?"

Replay per variant x ticker on fresh yfinance 15m/5m bars:
  signal: shoot>=pct & vol>=avg20*mult & RSI14>=min & [day-high] & |nifty
          gap|<=cap & time-window & price floor & not circuit-pinned
  entry SHORT at signal-bar close
  exit:   SL entry*(1+sl) | TP entry*(1-tp) | close after max-hold (5h)
  gates:  one open trade per (variant,ticker), max 5 entries/day/variant,
          INDIAN charges 0.12% RT

Run: python fade_backtest.py
"""
import os, sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CHARGES_PER_MARKET, FADE_VARIANTS
from scanner_fade import (BAR_MINUTES, WINDOWS, FADE_MIN_PRICE,
                          compute_fade_indicators, _shoot_series,
                          _is_pinned_bar, load_fade_universe)

HERE = os.path.dirname(os.path.abspath(__file__))
COST = CHARGES_PER_MARKET["INDIAN"]
MAX_HOLD_HOURS = 5
MAX_PER_DAY = 5


def fresh(tkr, interval):
    d = None
    for attempt in range(3):
        try:
            d = yf.download(tkr, period="60d", interval=interval,
                            progress=False, auto_adjust=False, threads=False)
            if d is not None and not d.empty:
                break
        except Exception:
            pass
        import time as _t
        _t.sleep(1.5 * (attempt + 1))
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    return d.dropna(subset=["Close"])


def nifty_gaps():
    d = fresh("^NSEI", "1d")
    if d is None:
        return {}
    pc = d["Close"].shift(1)
    g = ((d["Open"] - pc) / pc * 100).dropna()
    return {ts.date(): float(v) for ts, v in g.items()}


def main():
    universe = load_fade_universe()
    print(f"[fadebt] universe: {len(universe)} stocks")
    gaps = nifty_gaps()

    data = {}
    need = sorted({(t, v["interval"]) for t in universe for v in FADE_VARIANTS})
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(fresh, t, iv): (t, iv) for t, iv in need}
        done = 0
        for fu in as_completed(futs):
            df = fu.result()
            if df is not None and len(df) > 40:
                try:
                    data[futs[fu]] = compute_fade_indicators(df)
                except Exception:
                    pass
            done += 1
            if done % 40 == 0:
                print(f"[fadebt] data {done}/{len(need)}")

    shoot_cache = {}
    rows = []
    for v in FADE_VARIANTS:
        iv = v["interval"]
        bm = BAR_MINUTES.get(iv, 15)
        dur_bars = max(1, int(np.ceil(v["dur_min"] / bm)))
        hold_bars = int(np.ceil(MAX_HOLD_HOURS * 60 / bm))
        wa, wb = WINDOWS.get(v.get("win", "0930_1500"), (240, 570))
        day_count = {}
        for (t, interval), df in data.items():
            if interval != iv:
                continue
            ck = (t, iv, v["dur_min"])
            if ck not in shoot_cache:
                shoot_cache[ck] = _shoot_series(df, v["dur_min"], iv)
            shoot = shoot_cache[ck]
            close = df["Close"].values
            high = df["High"].values
            low = df["Low"].values
            vola = df.get("VolAvg20")
            rsi = df.get("RSI14")
            ndh = df.get("NearDayHigh98")
            idx = df.index
            busy_until = -1
            for i in range(len(df)):
                if i <= busy_until:
                    continue
                ts = idx[i]
                uc = ts.hour * 60 + ts.minute
                if ts.tzinfo is not None:
                    uc = ts.tz_convert("UTC").hour * 60 + ts.tz_convert("UTC").minute
                if not (wa <= uc < wb):
                    continue
                s = float(shoot.iloc[i]) if hasattr(shoot, "iloc") else float(shoot[i])
                if not np.isfinite(s) or s < v["shoot_pct"]:
                    continue
                c = close[i]
                if c < FADE_MIN_PRICE or _is_pinned_bar(df.iloc[i]):
                    continue
                va = vola.iloc[i] if vola is not None else np.nan
                if not (np.isfinite(va) and va > 0 and df["Volume"].iloc[i] >= va * v["vol_mult"]):
                    continue
                rv = rsi.iloc[i] if rsi is not None else np.nan
                if not (np.isfinite(rv) and rv >= v["rsi_min"]):
                    continue
                if v.get("dh") and not bool(ndh.iloc[i]):
                    continue
                gm = v.get("gap_max")
                if gm is not None:
                    g = gaps.get(ts.date(), 0.0)
                    if abs(g) > gm:
                        continue
                key = (v["rank"], ts.date())
                if day_count.get(key, 0) >= MAX_PER_DAY:
                    continue
                # enter SHORT at close[i]; walk forward
                entry = c
                sl = entry * (1 + v["sl_pct"])
                tp = entry * (1 - v["tp_pct"])
                exit_px, exit_i = None, None
                last_j = min(len(df) - 1, i + hold_bars)
                for j in range(i + 1, last_j + 1):
                    if high[j] >= sl:
                        exit_px, exit_i = sl, j
                        break
                    if low[j] <= tp:
                        exit_px, exit_i = tp, j
                        break
                if exit_px is None:
                    exit_px, exit_i = close[last_j], last_j
                pnl_pct = (entry - exit_px) / entry - COST
                busy_until = exit_i
                day_count[key] = day_count.get(key, 0) + 1
                rows.append({"variant": v["key"], "rank": v["rank"],
                             "ticker": t, "entry_ts": str(ts),
                             "ret_pct": round(pnl_pct * 100, 3)})
    out = pd.DataFrame(rows)
    out.to_csv(os.path.join(HERE, "logs", "fade_backtest60d_trades.csv"), index=False)
    print(f"\n{'='*74}\n  FADE 60-DAY REPLAY - {len(out)} trades\n{'='*74}")
    if len(out) == 0:
        print("no signals")
        return
    g = out.groupby(["variant"]).agg(n=("ret_pct", "size"),
                                     wr=("ret_pct", lambda x: round((x > 0).mean() * 100, 1)),
                                     net=("ret_pct", "sum")).round(2)
    g = g.sort_values("net")
    print(g.to_string())
    tot = out["ret_pct"].sum()
    wr = (out["ret_pct"] > 0).mean() * 100
    print(f"\nTOTAL: {len(out)} trades | WR {wr:.1f}% | net {tot:+.2f}% "
          f"(sum of per-trade % after charges)")
    pos = (out.groupby('variant')['ret_pct'].sum() > 0).sum()
    print(f"profitable variants: {pos}/{g.shape[0]}")


if __name__ == "__main__":
    main()

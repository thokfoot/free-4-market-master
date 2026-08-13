"""
FREE 3-Market v5.19 — NSE LONG-BOUNCE SCANNER (verified 5m dip-buy)
====================================================================
"Buy the dump": when a stock drops fast (volume + oversold RSI) LONG it —
expect a bounce.  Verified on real NSE 5m data (800 stocks x 58 days):

  L1: 5m drop 3.5%/90m + vol 2.5x + RSI<=45 + NIFTY gap<=1.5 +
      below-VWAP & VWAP-down + 10:30-12:30 IST
  -> 70 trades, 54.3% win, +56.0% net; OOS last-20-days 72.7% win +30.5%

Signal definition (no lookahead, faithful to the backtest):
  drop  = (close[t-dur] - close[t]) / close[t-dur] * 100 >= drop_pct
  vol   = volume[t] >= vol_avg20[t] * vol_mult        (prior-day avg)
  rsi   = rsi14 Wilder <= rsi_max
  gap   = abs(NIFTY day gap) <= gap_max
  vwap  = close[t] < daily VWAP[t]  AND  VWAP[t] < VWAP[t-1]
  win   = signal candle's IST time inside 10:30-12:30

Signal on the LAST COMPLETED candle; entry at next candle open (current
price when scanning after the close).  LONG: SL below, TP above.

Usage (called from bot.py --mode=fade, or standalone):
    python scanner_long.py [--dry] [--limit N]
"""
import os
import pandas as pd
import numpy as np
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from config import (
    LONG_BOUNCE_UNIVERSE_FILE, LONG_BOUNCE_VARIANTS,
    LONG_BOUNCE_MIN_PRICE, LONG_BOUNCE_MAX_HOLD_HOURS,
)
from scanner_fade import wilder_rsi, nifty_day_gap_pct, _utc_minutes
import market_data

IST = pytz.timezone("Asia/Kolkata")
BAR_MINUTES = {"1h": 60, "15m": 15, "5m": 5, "1m": 1}
# IST windows in UTC minutes: 10:30-12:30 IST = 05:00-07:00 UTC = (300, 420)
WINDOWS = {"1030_1230": (300, 420)}


def _download(ticker: str, interval: str, period: str) -> pd.DataFrame:
    import time as _time
    for attempt in range(3):
        try:
            df = market_data.download(ticker, interval=interval, period=period)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            pass
        _time.sleep(1.5 * (attempt + 1))
    return None


def compute_long_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """VolAvg20 (prior-day), RSI14, daily VWAP + below_vwap / vwap_down."""
    if df is None or len(df) < 40:
        return df
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df["_date"] = df.index.date
    df["_dnum"] = pd.factorize(df["_date"])[0]
    _prior_vol = {d: df.loc[df["_dnum"] < d, "Volume"].tail(20).mean()
                  for d in sorted(df["_dnum"].unique())}
    df["VolAvg20"] = df["_dnum"].map(lambda d: _prior_vol.get(d, np.nan))
    df["RSI14"] = wilder_rsi(df["Close"])
    # daily cumulative VWAP (typical * vol / vol, per day — no lookahead)
    typical = (df["High"] + df["Low"] + df["Close"]) / 3.0
    df["_tv"] = typical * df["Volume"]
    df["CumTV"] = df.groupby("_date")["_tv"].cumsum()
    df["CumVol"] = df.groupby("_date")["Volume"].cumsum()
    with np.errstate(divide="ignore", invalid="ignore"):
        df["VWAP"] = df["CumTV"] / df["CumVol"].replace(0, np.nan)
    df["BelowVWAP"] = df["Close"] < df["VWAP"]
    df["VWAPDown"] = df["VWAP"] < df["VWAP"].shift(1)
    return df


def _drop_series(df: pd.DataFrame, dur_min: int, interval: str) -> pd.Series:
    """rolling drop: (close[t-dur] - close[t]) / close[t-dur] * 100."""
    dur_bars = max(1, int(np.ceil(dur_min / BAR_MINUTES.get(interval, 5))))
    prev = df["Close"].shift(dur_bars)
    with np.errstate(invalid="ignore", divide="ignore"):
        return (prev - df["Close"]) / prev * 100.0


def _variant_fired_long(last, v: dict, gap: float, drop_val: float, utc_min: int) -> bool:
    """All factors for one LONG variant on the last completed candle."""
    close = float(last["Close"])
    if not np.isfinite(close) or close < LONG_BOUNCE_MIN_PRICE:
        return False
    if not np.isfinite(drop_val) or drop_val < v["drop_pct"]:
        return False
    vol_avg = last.get("VolAvg20", np.nan)
    rsi = last.get("RSI14", np.nan)
    vol = float(last["Volume"])
    if not (np.isfinite(vol_avg) and vol_avg > 0 and vol >= vol_avg * v["vol_mult"]):
        return False
    if not (np.isfinite(rsi) and rsi <= v["rsi_max"]):
        return False
    if v.get("vwap") and not bool(last.get("BelowVWAP", False)):
        return False
    if v.get("vwap_down") and not bool(last.get("VWAPDown", False)):
        return False
    if v.get("gap_max") is not None:
        if not np.isfinite(gap) or abs(gap) > v["gap_max"]:
            return False
    wa, wb = WINDOWS.get(v.get("win", "1030_1230"), (300, 420))
    if not (wa <= utc_min < wb):
        return False
    return True


def load_long_universe() -> list:
    if not os.path.exists(LONG_BOUNCE_UNIVERSE_FILE):
        return []
    df = pd.read_csv(LONG_BOUNCE_UNIVERSE_FILE)
    syms = [str(s).strip() for s in df.iloc[:, 0].tolist()]
    return [s if s.endswith(".NS") else s + ".NS" for s in syms if s]


def scan_long(limit: int = None, dry: bool = False) -> dict:
    """Scan universe for LONG-BOUNCE signals across all LONG variants."""
    start = datetime.now(IST)
    date_str = start.strftime("%Y-%m-%d")
    time_str = start.strftime("%H:%M:%S IST")

    universe = load_long_universe()
    if limit:
        universe = universe[:limit]
    print(f"[Long] Universe: {len(universe)} stocks | {date_str} {time_str}")

    gap = nifty_day_gap_pct()
    if not np.isfinite(gap):
        print("[Long] WARNING: NIFTY gap unknown (download failed)")

    intervals_needed = sorted({v["interval"] for v in LONG_BOUNCE_VARIANTS})
    period_map = {v["interval"]: v["period"] for v in LONG_BOUNCE_VARIANTS}
    ticker_data = {}
    scan_errors = 0
    skipped_entries = []
    for interval in intervals_needed:
        period = period_map.get(interval, "60d")
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_download, t, interval, period): t for t in universe}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    df = fut.result()
                    if df is not None and len(df) > 40:
                        ticker_data[(interval, t)] = compute_long_indicators(df)
                    else:
                        scan_errors += 1
                except Exception:
                    scan_errors += 1
        print(f"[Long] {interval} data OK: "
              f"{len([1 for (i, _) in ticker_data if i == interval])}/{len(universe)}, errors {scan_errors}")

    signals = []
    fired = []
    for v in LONG_BOUNCE_VARIANTS:
        dur_bars = max(1, int(np.ceil(v["dur_min"] / BAR_MINUTES.get(v["interval"], 5))))
        drop_cache = {}
        v_signals = []
        for (interval, t), df in ticker_data.items():
            if interval != v["interval"]:
                continue
            key = (interval, dur_bars, t)
            if key not in drop_cache:
                drop_cache[key] = _drop_series(df, v["dur_min"], interval).to_numpy()
            sig_idx = len(df) - 1
            last = df.iloc[sig_idx]
            close = float(last["Close"])
            if sig_idx + 1 < len(df):
                entry_price = float(df.iloc[sig_idx + 1]["Open"])
            else:
                entry_price = close
            if not np.isfinite(close) or close < LONG_BOUNCE_MIN_PRICE:
                continue
            vol_avg = last.get("VolAvg20", np.nan)
            rsi = last.get("RSI14", np.nan)
            vol = float(last["Volume"])
            drop_val = float(drop_cache[key][sig_idx])
            utc_min = _utc_minutes(df.index[sig_idx])
            is_fired = _variant_fired_long(last, v, gap, drop_val, utc_min)

            reason = "All factors met" if is_fired else (
                f"Drop {drop_val:.2f}% (<{v['drop_pct']})" if np.isfinite(drop_val) and drop_val < v["drop_pct"]
                else f"Vol {vol/vol_avg:.1f}x (<{v['vol_mult']})" if np.isfinite(vol_avg) and vol_avg > 0 and vol < vol_avg * v["vol_mult"]
                else f"RSI {rsi:.0f} (>{v['rsi_max']})" if np.isfinite(rsi) and rsi > v["rsi_max"]
                else f"No below-VWAP" if v.get("vwap") and not bool(last.get("BelowVWAP", False))
                else f"VWAP not down" if v.get("vwap_down") and not bool(last.get("VWAPDown", False))
                else f"Gap skip {gap:+.2f}%" if v.get("gap_max") is not None and np.isfinite(gap) and abs(gap) > v["gap_max"]
                else f"Window skip (UTC {utc_min // 60:02d}:{utc_min % 60:02d})"
                if not (WINDOWS.get(v.get("win", "1030_1230"), (300, 420))[0] <= utc_min <
                        WINDOWS.get(v.get("win", "1030_1230"), (300, 420))[1])
                else "No signal"
            )
            v_signals.append({
                "rank": v["rank"], "market": "NSE", "region": "INDIAN",
                "ticker": t, "direction": "LONG",
                "factors": v["factors"], "win_rate": v["win_rate"],
                "trades_count": v["trades_count"], "close": close,
                "entry_price": entry_price,
                "interval": interval,
                "sl_pct": v["sl_pct"], "tp_pct": v["tp_pct"],
                "max_hold_hours": LONG_BOUNCE_MAX_HOLD_HOURS,
                "fired": is_fired, "reason": reason,
                "signal_indicators": {
                    "Close": round(close, 2), "Volume": int(vol),
                    "VolAvg20": round(float(vol_avg), 0) if np.isfinite(vol_avg) else None,
                    "RSI14": round(float(rsi), 1) if np.isfinite(rsi) else None,
                    "Drop_pct": round(float(drop_val), 2) if np.isfinite(drop_val) else None,
                    "BelowVWAP": bool(last.get("BelowVWAP", False)),
                    "VWAPDown": bool(last.get("VWAPDown", False)),
                    "NIFTY_gap_pct": round(float(gap), 2) if np.isfinite(gap) else None,
                },
            })
        v_fired = [s for s in v_signals if s["fired"]]
        v_fired.sort(key=lambda s: -abs(s["signal_indicators"].get("Drop_pct") or 0))
        cap_cut = v_fired[v["max_per_day"]:]
        v_fired = v_fired[:v["max_per_day"]]
        signals.extend(v_signals)
        fired.extend(v_fired)
        for c in cap_cut:
            skipped_entries.append({
                "ticker": c["ticker"], "direction": "LONG",
                "close": c["close"], "rank": c["rank"],
                "win_rate": c.get("win_rate"),
                "reason": f"Variant daily cap ({v['key']} #{v['rank']}: {v['max_per_day']}/day) - stronger signals took slots",
            })
        print(f"[Long] {v['key']} #{v['rank']} {v['interval']}: "
              f"signals {len(v_signals)}, fired {len(v_fired)}, cap-cut {len(cap_cut)}")

    if dry:
        print("[Long] DRY RUN — no entries will be made")

    return {
        "mode": "LONG_BOUNCE",
        "ticker_data": ticker_data,
        "market_status": {},
        "current_prices": {s["ticker"]: s["close"] for s in fired},
        "ohlc_data": {},
        "all_signals": signals,
        "fired_signals": fired,
        "best_entries": fired,
        "entries": [],
        "skipped_entries": skipped_entries,
        "closed_msgs": [],
        "scan_errors": scan_errors,
        "duration": (datetime.now(IST) - start).total_seconds(),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    res = scan_long(limit=a.limit, dry=a.dry)
    print(f"\n=== LONG-BOUNCE SCAN (dry={a.dry}) ===")
    print(f"signals: {len(res['all_signals'])}, fired: {len(res['fired_signals'])}")
    for s in res["fired_signals"]:
        print(f"  LONG {s['ticker']} @ {s['close']} | rank#{s['rank']} {s['interval']} | {s['reason']}")

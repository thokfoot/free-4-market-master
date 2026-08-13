"""
FREE 3-Market v5.18 — US FADE SCANNER (5 verified 5m variants)
===============================================================
"Big Player Exit Fade" on US large-caps (129-stock universe): when a stock
shoots up fast (volume + RSI + price BELOW VWAP) SHORT it — the big player
is exiting.

5 verified variants (real 5m candles May-Jul 2026, 87 days, 129 US stocks,
clean train/test split, 0.1% costs included):
  U1-U5 = top US 5m fade combos, ALL with the VWAP-below filter (extra=1):
          +6.4 to +11.1 net/mo full-period, +2.6 to +5.9 net/mo at cap5.

Signal definition (faithful to the backtest, no lookahead):
  shoot = (close[t] - min(low over last dur_min)) / min(low) * 100 >= shoot_pct
  vol   = volume[t] >= vol_avg20[t] * vol_mult
  rsi   = rsi14 Wilder >= rsi_min
  vwap  = close < running-day VWAP  (the winning US filter)
  gap   = abs(SPY day gap) <= gap_max  (when gap_max set)
  win   = signal candle's ET time inside variant window (0930_1500 ET)

Signal on the LAST COMPLETED candle; entry at that candle's close.

Usage (called from bot.py --mode=fade, or standalone):
    python scanner_fade_us.py [--dry] [--limit N]
"""
import os
import pandas as pd
import numpy as np
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from config import (
    US_FADE_UNIVERSE_FILE, US_FADE_VARIANTS, US_FADE_ALLOW_SHORT,
    US_FADE_MIN_PRICE, US_FADE_MAX_HOLD_HOURS,
)
import market_data

IST = pytz.timezone("Asia/Kolkata")
ET = pytz.timezone("America/New_York")
# yfinance bar seconds per interval (for completed-candle detection)
BAR_SECONDS = {"1h": 3600, "15m": 900, "5m": 300, "1m": 60}
# bar minutes per interval (for rolling-low shoot over dur_min)
BAR_MINUTES = {"1h": 60, "15m": 15, "5m": 5, "1m": 1}
# variant time windows in ET minutes: 570-900 = 09:30-15:00 ET
WINDOWS = {"0930_1500": (570, 900)}


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI — matches the backtest indicator."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _signal_candle_index(df: pd.DataFrame, interval: str = "5m") -> int:
    """Index of the LAST COMPLETED candle for the given interval.

    yfinance can return the still-forming candle as the last row. Signals
    must only use completed candles (no lookahead).
    """
    try:
        if len(df) < 2:
            return len(df) - 1
        now = pd.Timestamp.utcnow()
        last_ts = df.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        secs = BAR_SECONDS.get(interval, 300)
        if (now - last_ts).total_seconds() < secs:
            return len(df) - 2
        return len(df) - 1
    except Exception:
        return len(df) - 1


def _et_minutes(ts) -> int:
    """ET minutes-of-day for a (possibly tz-aware UTC) timestamp."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    ts = ts.tz_convert(ET)
    return ts.hour * 60 + ts.minute


def compute_us_fade_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """VolAvg20 + RSI14 + running-day VWAP + below-VWAP on any TF.
    VWAP uses per-day cumulative typical-price*volume (NO lookahead)."""
    if df is None or len(df) < 40:
        return df
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    df["RSI14"] = wilder_rsi(df["Close"])
    # per calendar day causal VWAP (no lookahead)
    df["_date"] = df.index.date
    tp = (df["High"] + df["Low"] + df["Close"]) / 3.0
    cp = (tp * df["Volume"]).groupby(df["_date"]).cumsum()
    cv = df["Volume"].groupby(df["_date"]).cumsum()
    df["VWAPRun"] = cp / cv.replace(0, np.nan)
    df["BelowVWAP"] = df["Close"] < df["VWAPRun"]
    return df


def spy_day_gap_pct() -> float:
    """Today's SPY gap: (open - prev_close)/prev_close * 100. NaN if unavailable."""
    try:
        df = market_data.download("SPY", interval="1d", period="5d")
        if df is None or len(df) < 2:
            return float("nan")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        return float((last["Open"] - prev["Close"]) / prev["Close"] * 100.0)
    except Exception:
        return float("nan")


def load_us_fade_universe() -> list:
    """Universe: 129 US large-caps from the backtest meta."""
    syms = []
    if os.path.exists(US_FADE_UNIVERSE_FILE):
        df = pd.read_csv(US_FADE_UNIVERSE_FILE)
        syms = [str(s).strip() for s in df["symbol"].tolist() if str(s).strip()]
    return list(dict.fromkeys(syms))


def _download(ticker: str, interval: str, period: str) -> pd.DataFrame:
    """Download OHLCV with retries (yfinance is rate-limit prone)."""
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


def _variant_fired_us(last, v: dict, gap: float, shoot_val: float, et_min: int) -> bool:
    """Check all factors for one US variant on the last completed candle."""
    close = float(last["Close"])
    if not np.isfinite(close) or close < US_FADE_MIN_PRICE:
        return False
    if not np.isfinite(shoot_val) or shoot_val < v["shoot_pct"]:
        return False
    vol_avg = last.get("VolAvg20", np.nan)
    rsi = last.get("RSI14", np.nan)
    vol = float(last["Volume"])
    if not (np.isfinite(vol_avg) and vol_avg > 0 and vol >= vol_avg * v["vol_mult"]):
        return False
    if not (np.isfinite(rsi) and rsi >= v["rsi_min"]):
        return False
    if v.get("vwap") and not bool(last.get("BelowVWAP", False)):
        return False
    if v.get("gap_max") is not None:
        if not np.isfinite(gap) or abs(gap) > v["gap_max"]:
            return False
    wa, wb = WINDOWS.get(v.get("win", "0930_1500"), (570, 900))
    if not (wa <= et_min < wb):
        return False
    return True


def scan_fade_us(limit: int = None, dry: bool = False) -> dict:
    """
    Scan US universe for FADE SHORT signals across all US variants.

    Returns dict compatible with bot.py scan_result format.
    """
    start = datetime.now(IST)
    date_str = start.strftime("%Y-%m-%d")
    time_str = start.strftime("%H:%M:%S IST")

    universe = load_us_fade_universe()
    if limit:
        universe = universe[:limit]
    print(f"[FadeUS] Universe: {len(universe)} stocks | {date_str} {time_str}")

    gap = spy_day_gap_pct()
    if not np.isfinite(gap):
        print("[FadeUS] WARNING: SPY gap unknown (download failed)")

    intervals_needed = sorted({v["interval"] for v in US_FADE_VARIANTS})
    period_map = {v["interval"]: v["period"] for v in US_FADE_VARIANTS}
    ticker_data = {}
    scan_errors = 0
    for interval in intervals_needed:
        period = period_map.get(interval, "60d")
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_download, t, interval, period): t for t in universe}
            for fut in as_completed(futs):
                t = futs[fut]
                try:
                    df = fut.result()
                    if df is not None and len(df) > 40:
                        ticker_data[(interval, t)] = compute_us_fade_indicators(df)
                    else:
                        scan_errors += 1
                except Exception:
                    scan_errors += 1
        print(f"[FadeUS] {interval} data OK: {len([1 for (i, _) in ticker_data if i == interval])} "
              f"/{len(universe)}, errors {scan_errors}")

    # ── per-variant signal detection on last COMPLETED candle (no lookahead) ──
    signals = []
    fired = []
    shoot_cache = {}
    for v in US_FADE_VARIANTS:
        dur_bars = max(1, int(np.ceil(v["dur_min"] / BAR_MINUTES.get(v["interval"], 5))))
        for (interval, t), df in ticker_data.items():
            if interval == v["interval"]:
                key = (interval, dur_bars, t)
                if key not in shoot_cache:
                    low_min = df["Low"].rolling(dur_bars, min_periods=1).min()
                    with np.errstate(invalid="ignore", divide="ignore"):
                        shoot_cache[key] = ((df["Close"] - low_min) / low_min * 100.0).to_numpy()
        v_signals = []
        for (interval, t), df in ticker_data.items():
            if interval != v["interval"]:
                continue
            sig_idx = _signal_candle_index(df, interval)
            last = df.iloc[sig_idx]
            close = float(last["Close"])
            if not np.isfinite(close) or close < US_FADE_MIN_PRICE:
                continue
            vol_avg = last.get("VolAvg20", np.nan)
            rsi = last.get("RSI14", np.nan)
            vol = float(last["Volume"])
            shoot_val = float(shoot_cache[(interval, dur_bars, t)][sig_idx])
            et_min = _et_minutes(df.index[sig_idx])
            is_fired = _variant_fired_us(last, v, gap, shoot_val, et_min)

            # reason for non-fired (first failing factor)
            if is_fired:
                reason = "All factors met"
            elif np.isfinite(shoot_val) and shoot_val < v["shoot_pct"]:
                reason = f"Shoot {shoot_val:.2f}% (<{v['shoot_pct']})"
            elif np.isfinite(vol_avg) and vol_avg > 0 and vol < vol_avg * v["vol_mult"]:
                reason = f"Vol {vol/vol_avg:.1f}x (<{v['vol_mult']})"
            elif np.isfinite(rsi) and rsi < v["rsi_min"]:
                reason = f"RSI {rsi:.0f} (<{v['rsi_min']})"
            elif v.get("vwap") and not bool(last.get("BelowVWAP", False)):
                reason = "Not below VWAP"
            elif v.get("gap_max") is not None and np.isfinite(gap) and abs(gap) > v["gap_max"]:
                reason = f"Gap skip {gap:+.2f}%"
            elif not (WINDOWS.get(v.get("win", "0930_1500"), (570, 900))[0] <= et_min <
                      WINDOWS.get(v.get("win", "0930_1500"), (570, 900))[1]):
                reason = f"Window skip (ET {et_min // 60:02d}:{et_min % 60:02d})"
            else:
                reason = "No signal"
            v_signals.append({
                "rank": v["rank"], "market": "US", "region": "US",
                "ticker": t, "direction": "SHORT",
                "factors": v["factors"], "win_rate": v["win_rate"],
                "trades_count": v["trades_count"], "close": close,
                "interval": interval,
                "sl_pct": v["sl_pct"], "tp_pct": v["tp_pct"],
                "max_hold_hours": US_FADE_MAX_HOLD_HOURS,
                "fired": is_fired, "reason": reason,
                "signal_indicators": {
                    "Close": round(close, 2), "Volume": int(vol),
                    "VolAvg20": round(float(vol_avg), 0) if np.isfinite(vol_avg) else None,
                    "RSI14": round(float(rsi), 1) if np.isfinite(rsi) else None,
                    "Shoot_pct": round(float(shoot_val), 2) if np.isfinite(shoot_val) else None,
                    "BelowVWAP": bool(last.get("BelowVWAP", False)),
                    "SPY_gap_pct": round(float(gap), 2) if np.isfinite(gap) else None,
                },
            })
        v_fired = [s for s in v_signals if s["fired"]]
        v_fired.sort(key=lambda s: -abs(s["signal_indicators"].get("Shoot_pct") or 0))
        v_fired = v_fired[:v["max_per_day"]]
        signals.extend(v_signals)
        fired.extend(v_fired)
        print(f"[FadeUS] {v['key']} #{v['rank']} {v['interval']}: "
              f"signals {len(v_signals)}, fired {len(v_fired)} (per-run cap {v['max_per_day']})")

    if dry:
        print("[FadeUS] DRY RUN — no entries will be made")

    return {
        "mode": "US_FADE",
        "ticker_data": ticker_data,
        "market_status": {},
        "current_prices": {s["ticker"]: s["close"] for s in fired},
        "ohlc_data": {},
        "all_signals": signals,
        "fired_signals": fired,
        "best_entries": fired,
        "errors": scan_errors,
        "tickers_total": len(universe) * len(intervals_needed),
        "strategies_total": len(US_FADE_VARIANTS),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    res = scan_fade_us(limit=a.limit, dry=a.dry)
    print(f"\n[FadeUS] signals: {len(res['all_signals'])}, fired: {len(res['fired_signals'])}")
    for s in res["fired_signals"]:
        print(f"  SHORT {s['ticker']} @ {s['close']:.2f} | {s['factors']}")

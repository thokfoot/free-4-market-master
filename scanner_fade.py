"""
FREE 3-Market v5.15 — NSE FADE SCANNER (35 verified variants)
=============================================================
"Big Player Exit Fade": when a stock shoots up fast (volume + RSI) SHORT it —
the big player is exiting.

35 verified variants (2015-2022, 828 NSE stocks, strict train/test split):
  S1-S10  = other-AI TOP 10 verified on 7.5yr OOS (all 8/8 yrs positive)
  G1-G25  = grid-search combos (3.5-4% shoot + day-high), test +16..+29 net/mo

Signal definition (faithful to the 7.5-year backtest, no lookahead):
  shoot = (close[t] - min(low over last dur_min)) / min(low) * 100 >= shoot_pct
  vol   = volume[t] >= vol_avg20[t] * vol_mult
  rsi   = rsi14 Wilder >= rsi_min
  gap   = abs(NIFTY day gap) <= gap_max  (when gap_max set)
  dh    = close >= running-day-high * 0.98  (day-high filter, cummax — no lookahead)
  win   = signal candle's IST time inside variant window (0930_1500 / 1030_1300)

Signal on the LAST COMPLETED candle; entry at that candle's close (next candle
open ≈ current price when scanning after the close).

Usage (called from bot.py --mode=fade, or standalone):
    python scanner_fade.py [--dry] [--limit N]
"""
import os
import pandas as pd
import numpy as np
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from config import (
    FADE_UNIVERSE_FILE, FADE_VARIANTS, FADE_ALLOW_SHORT,
    FADE_MIN_PRICE, INDIAN_TICKERS,
)
import market_data

IST = pytz.timezone("Asia/Kolkata")
# yfinance bar seconds per interval (for completed-candle detection)
BAR_SECONDS = {"1h": 3600, "15m": 900, "5m": 300, "1m": 60}
# bar minutes per interval (for rolling-low shoot over dur_min)
BAR_MINUTES = {"1h": 60, "15m": 15, "5m": 5, "1m": 1}
# variant time windows in UTC minutes: 240-570 = 09:30-15:00 IST, 300-450 = 10:30-13:00 IST
# (450 = 13:00 IST = 07:30 UTC — backtest spec says 10:30-13:00, fix from 420=12:30)
WINDOWS = {"0930_1500": (240, 570), "1030_1300": (300, 450)}


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI — matches the 7.5-year backtest indicator."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _signal_candle_index(df: pd.DataFrame, interval: str = "15m") -> int:
    """Index of the LAST COMPLETED candle for the given interval.

    yfinance can return the still-forming candle as the last row. Signals
    must only use completed candles (no lookahead). Entry is at the signal
    candle's close (next candle open ~= current price when scanning after close).
    """
    try:
        if len(df) < 2:
            return len(df) - 1
        now = pd.Timestamp.utcnow()
        last_ts = df.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        secs = BAR_SECONDS.get(interval, 900)
        if (now - last_ts).total_seconds() < secs:
            return len(df) - 2
        return len(df) - 1
    except Exception:
        return len(df) - 1


def _utc_minutes(ts) -> int:
    """UTC minutes-of-day for a (possibly tz-aware) timestamp."""
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    else:
        ts = ts.tz_convert("UTC")
    return ts.hour * 60 + ts.minute


def compute_fade_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Volume avg20 + RSI14 + running-day-high + prev-day-high on any TF.
    Running day high uses cummax (NO lookahead) — matches the backtest."""
    if df is None or len(df) < 40:
        return df
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df["_date"] = df.index.date
    # Backtest spec: "avg(volume last 20 bars same timeframe, EXCLUDING current day)"
    # Live rolling(20).mean() would include today's bars — that overstates baseline
    # early in the day and understates the true prior-day volume level. Replicate the
    # backtest exactly: baseline = mean of last 20 bars from PRIOR trading days only.
    df["_dnum"] = pd.factorize(df["_date"])[0]  # 0=first day, 1=second, ...
    _prior_vol = {d: df.loc[df["_dnum"] < d, "Volume"].tail(20).mean()
                  for d in sorted(df["_dnum"].unique())}
    df["VolAvg20"] = df["_dnum"].map(lambda d: _prior_vol.get(d, np.nan))
    df["RSI14"] = wilder_rsi(df["Close"])
    # per calendar day running high (cummax — no lookahead)
    df["DayHighRun"] = df.groupby("_date")["High"].cummax()
    df["NearDayHigh98"] = df["Close"] >= df["DayHighRun"] * 0.98   # within 2% of running day high
    # previous day's high (from previous calendar day's full max)
    day_max = df.groupby("_date")["High"].max()
    prev_day_max = day_max.shift(1)
    df["PrevDayHigh"] = df["_date"].map(prev_day_max)
    df["BrkPrevHigh"] = df["Close"] > df["PrevDayHigh"]
    return df


def nifty_day_gap_pct() -> float:
    """Today's ^NSEI gap: (open - prev_close)/prev_close * 100. NaN if unavailable."""
    try:
        df = market_data.download("^NSEI", interval="1d", period="5d")
        if df is None or len(df) < 2:
            return float("nan")
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        last = df.iloc[-1]
        prev = df.iloc[-2]
        return float((last["Open"] - prev["Close"]) / prev["Close"] * 100.0)
    except Exception:
        return float("nan")


def load_fade_universe() -> list:
    """Universe: top-fade-signal NSE stocks + the core liquid names."""
    syms = []
    if os.path.exists(FADE_UNIVERSE_FILE):
        df = pd.read_csv(FADE_UNIVERSE_FILE)
        syms = [str(s).strip() for s in df["symbol"].tolist() if str(s).strip()]
    core = [t for t in INDIAN_TICKERS if not t.startswith("^")]
    return list(dict.fromkeys(syms + core))


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


def _shoot_series(df: pd.DataFrame, dur_min: int, interval: str) -> pd.Series:
    """rolling-low shoot: (close - min(low last dur_min)) / min(low) * 100."""
    dur_bars = max(1, int(np.ceil(dur_min / BAR_MINUTES.get(interval, 15))))
    low_min = df["Low"].rolling(dur_bars, min_periods=1).min()
    with np.errstate(invalid="ignore", divide="ignore"):
        return (df["Close"] - low_min) / low_min * 100.0


def _variant_fired(last, v: dict, gap: float, shoot_val: float, utc_min: int) -> bool:
    """Check all factors for one variant on the last completed candle."""
    close = float(last["Close"])
    if not np.isfinite(close) or close < FADE_MIN_PRICE:
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
    if v.get("dh") and not bool(last.get("NearDayHigh98", False)):
        return False
    if v.get("ph") and not bool(last.get("BrkPrevHigh", False)):
        return False
    if v.get("gap_max") is not None:
        if not np.isfinite(gap) or abs(gap) > v["gap_max"]:
            return False
    wa, wb = WINDOWS.get(v.get("win", "0930_1500"), (240, 570))
    if not (wa <= utc_min < wb):
        return False
    return True


def scan_fade(limit: int = None, dry: bool = False) -> dict:
    """
    Scan universe for FADE SHORT signals across all 35 variants.

    Returns dict compatible with bot.py scan_result format.
    """
    start = datetime.now(IST)
    date_str = start.strftime("%Y-%m-%d")
    time_str = start.strftime("%H:%M:%S IST")

    universe = load_fade_universe()
    if limit:
        universe = universe[:limit]
    print(f"[Fade] Universe: {len(universe)} stocks | {date_str} {time_str}")

    gap = nifty_day_gap_pct()
    if not np.isfinite(gap):
        print("[Fade] WARNING: NIFTY gap unknown (download failed)")

    # Download per-interval (15m shared by S1-S4/S6-S8/S10/G1-G25, 5m for S5/S9)
    intervals_needed = sorted({v["interval"] for v in FADE_VARIANTS})
    period_map = {v["interval"]: v["period"] for v in FADE_VARIANTS}
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
                        ticker_data[(interval, t)] = compute_fade_indicators(df)
                    else:
                        scan_errors += 1
                except Exception:
                    scan_errors += 1
        print(f"[Fade] {interval} data OK: {len([1 for (i, _) in ticker_data if i == interval])} "
              f"/{len(universe)}, errors {scan_errors}")

    # ── per-variant signal detection on last COMPLETED candle (no lookahead) ──
    signals = []
    fired = []
    # cache rolling-low shoot series per (interval, dur_bars) — only 4 unique
    # durations per TF (15m: 2/3/4/6, 5m: 18) instead of 35 recomputes per ticker
    shoot_cache = {}
    for v in FADE_VARIANTS:
        dur_bars = max(1, int(np.ceil(v["dur_min"] / BAR_MINUTES.get(v["interval"], 15))))
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
            # Backtest: "SHORT next bar Open after signal (no delay)". The next
            # completed candle after the signal candle has already opened when the
            # scanner runs — use its OPEN as the fill price (no lookahead).
            if sig_idx + 1 < len(df):
                entry_price = float(df.iloc[sig_idx + 1]["Open"])
            else:
                entry_price = close
            if not np.isfinite(close) or close < FADE_MIN_PRICE:
                continue
            vol_avg = last.get("VolAvg20", np.nan)
            rsi = last.get("RSI14", np.nan)
            vol = float(last["Volume"])
            shoot_val = float(shoot_cache[(interval, dur_bars, t)][sig_idx])
            utc_min = _utc_minutes(df.index[sig_idx])
            is_fired = _variant_fired(last, v, gap, shoot_val, utc_min)

            # reason for non-fired (first failing factor)
            reason = "All factors met" if is_fired else (
                f"Shoot {shoot_val:.2f}% (<{v['shoot_pct']})" if np.isfinite(shoot_val) and shoot_val < v["shoot_pct"]
                else f"Vol {vol/vol_avg:.1f}x (<{v['vol_mult']})" if np.isfinite(vol_avg) and vol_avg > 0 and vol < vol_avg * v["vol_mult"]
                else f"RSI {rsi:.0f} (<{v['rsi_min']})" if np.isfinite(rsi) and rsi < v["rsi_min"]
                else f"No day-high" if v.get("dh") and not bool(last.get("NearDayHigh98", False))
                else f"No prev-high break" if v.get("ph") and not bool(last.get("BrkPrevHigh", False))
                else f"Gap skip {gap:+.2f}%" if v.get("gap_max") is not None and np.isfinite(gap) and abs(gap) > v["gap_max"]
                else f"Window skip (UTC {utc_min // 60:02d}:{utc_min % 60:02d})"
                if not (WINDOWS.get(v.get("win", "0930_1500"), (240, 570))[0] <= utc_min <
                        WINDOWS.get(v.get("win", "0930_1500"), (240, 570))[1])
                else "No signal"
            )
            v_signals.append({
                "rank": v["rank"], "market": "NSE", "region": "INDIAN",
                "ticker": t, "direction": "SHORT",
                "factors": v["factors"], "win_rate": v["win_rate"],
                "trades_count": v["trades_count"], "close": close,
                "entry_price": entry_price,
                "interval": interval,
                "sl_pct": v["sl_pct"], "tp_pct": v["tp_pct"],
                "max_hold_hours": 5,
                "fired": is_fired, "reason": reason,
                "signal_indicators": {
                    "Close": round(close, 2), "Volume": int(vol),
                    "VolAvg20": round(float(vol_avg), 0) if np.isfinite(vol_avg) else None,
                    "RSI14": round(float(rsi), 1) if np.isfinite(rsi) else None,
                    "Shoot_pct": round(float(shoot_val), 2) if np.isfinite(shoot_val) else None,
                    "NearDayHigh": bool(last.get("NearDayHigh98", False)),
                    "BrkPrevHigh": bool(last.get("BrkPrevHigh", False)),
                    "NIFTY_gap_pct": round(float(gap), 2) if np.isfinite(gap) else None,
                },
            })
        # rank fired by signal strength; per-run bound = variant daily cap
        v_fired = [s for s in v_signals if s["fired"]]
        v_fired.sort(key=lambda s: -abs(s["signal_indicators"].get("Shoot_pct") or 0))
        v_fired = v_fired[:v["max_per_day"]]
        signals.extend(v_signals)
        fired.extend(v_fired)
        print(f"[Fade] {v['key']} #{v['rank']} {v['interval']}: "
              f"signals {len(v_signals)}, fired {len(v_fired)} (per-run cap {v['max_per_day']})")

    if dry:
        print("[Fade] DRY RUN — no entries will be made")

    return {
        "mode": "FADE",
        "ticker_data": ticker_data,
        "market_status": {},
        "current_prices": {s["ticker"]: s["close"] for s in fired},
        "ohlc_data": {},
        "all_signals": signals,
        "fired_signals": fired,
        "best_entries": fired,
        "entries": [],
        "skipped_entries": [],
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
    res = scan_fade(limit=a.limit, dry=a.dry)
    print(f"\n=== FADE SCAN (dry={a.dry}) ===")
    print(f"signals: {len(res['all_signals'])}, fired: {len(res['fired_signals'])}")
    for s in res["fired_signals"]:
        print(f"  SHORT {s['ticker']} @ {s['close']} | rank#{s['rank']} {s['interval']} | {s['reason']}")

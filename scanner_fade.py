"""
FREE 3-Market v5.14 — NSE FADE SCANNER (5 variants)
===================================================
"Big Player Exit Fade": when a stock shoots up fast (volume + RSI + prev-high
break / near-day-high) SHORT it — the big player is exiting.

5 validated variants (2-year clean OOS, 1680 NSE stocks, Aug 2024-Jul 2026):
  S1 1h LIVE      shoot 3.5% vol 2.2x RSI65 prev-high gap1.5 SL1.3/TP3.9 2/day  +199%
  S2 1h BALANCED  shoot 3.5% vol 1.8x RSI60 near-high  no-gap SL1.5/TP3.75 2/day +234%
  S3 1h LOOSE     shoot 3.0% vol 1.2x RSI60 none       no-gap SL1.4/TP3.92 2/day +227%
  S4 1h VOL-HEAVY shoot 4.0% vol 2.2x RSI60 prev-high  no-gap SL1.4/TP3.08 2/day +150%
  S5 15m BEST     shoot 2.0% vol 2.2x RSI75 none       gap0.8  SL1.0/TP3.0  5/day +23%

No-lookahead: signal on the LAST COMPLETED candle; entry at that candle's
close (next candle open ≈ current price when scanning after the close).

Usage (called from bot.py --mode=fade, or standalone):
    python scanner_fade.py [--dry] [--limit N]
"""
import os
import pandas as pd
import numpy as np
import pytz
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from config import (
    FADE_UNIVERSE_FILE, FADE_VARIANTS, FADE_ALLOW_SHORT,
    FADE_MIN_PRICE, INDIAN_TICKERS,
)

IST = pytz.timezone("Asia/Kolkata")
# yfinance bar seconds per interval (for completed-candle detection)
BAR_SECONDS = {"1h": 3600, "15m": 900, "5m": 300, "1m": 60}


def wilder_rsi(close: pd.Series, period: int = 14) -> pd.Series:
    """Wilder's RSI — matches the 2-year backtest indicator."""
    delta = close.diff()
    gain = delta.clip(lower=0.0)
    loss = -delta.clip(upper=0.0)
    avg_gain = gain.ewm(alpha=1.0 / period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1.0 / period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def _signal_candle_index(df: pd.DataFrame, interval: str = "1h") -> int:
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
        secs = BAR_SECONDS.get(interval, 3600)
        if (now - last_ts).total_seconds() < secs:
            return len(df) - 2
        return len(df) - 1
    except Exception:
        return len(df) - 1


def compute_fade_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Volume avg20 + RSI14 + prev-day-high-break + near-day-high on any TF."""
    if df is None or len(df) < 40:
        return df
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    df["RSI14"] = wilder_rsi(df["Close"])
    # per calendar day highs
    df["_date"] = df.index.date
    day_high = df.groupby("_date")["High"].transform("max")
    prev_day_high = day_high.groupby(df["_date"]).shift(1)
    df["PrevDayHigh"] = prev_day_high
    df["BrkPrevHigh"] = df["Close"] > df["PrevDayHigh"]
    df["NearDayHigh"] = df["Close"] >= day_high * 0.995   # within 0.5% of day high
    df["Ret1"] = df["Close"].pct_change() * 100.0
    return df


def nifty_day_gap_pct() -> float:
    """Today's ^NSEI gap: (open - prev_close)/prev_close * 100. NaN if unavailable."""
    try:
        df = yf.download("^NSEI", period="5d", interval="1d",
                         progress=False, auto_adjust=False)
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
    """Universe: top-100 fade-signal NSE stocks + the core liquid names."""
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
            df = yf.download(ticker, period=period, interval=interval,
                             progress=False, auto_adjust=False)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            pass
        _time.sleep(1.5 * (attempt + 1))
    return None


def _variant_fired(last, v: dict, gap: float) -> bool:
    """Check all factors for one variant on the last completed candle."""
    close = float(last["Close"])
    if not np.isfinite(close) or close < FADE_MIN_PRICE:
        return False
    vol_avg = last.get("VolAvg20", np.nan)
    rsi = last.get("RSI14", np.nan)
    ret1 = last.get("Ret1", np.nan)
    vol = float(last["Volume"])
    if not (np.isfinite(ret1) and ret1 >= v["shoot_pct"]):
        return False
    if not (np.isfinite(vol_avg) and vol_avg > 0 and vol >= vol_avg * v["vol_mult"]):
        return False
    if not (np.isfinite(rsi) and rsi >= v["rsi_min"]):
        return False
    extra = v.get("extra", "none")
    if extra == "prev_high" and not bool(last.get("BrkPrevHigh", False)):
        return False
    if extra == "near_high" and not bool(last.get("NearDayHigh", False)):
        return False
    if v.get("gap_max") is not None:
        if not np.isfinite(gap) or abs(gap) > v["gap_max"]:
            return False
    return True


def scan_fade(limit: int = None, dry: bool = False) -> dict:
    """
    Scan universe for FADE SHORT signals across all 5 variants.

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

    # Download per-interval (1h shared by S1-S4, 15m for S5)
    intervals_needed = sorted({v["interval"] for v in FADE_VARIANTS})
    period_map = {v["interval"]: v["period"] for v in FADE_VARIANTS}
    ticker_data = {}
    scan_errors = 0
    for interval in intervals_needed:
        period = period_map.get(interval, "3mo")
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
    for v in FADE_VARIANTS:
        v_signals = []
        for (interval, t), df in ticker_data.items():
            if interval != v["interval"]:
                continue
            sig_idx = _signal_candle_index(df, interval)
            last = df.iloc[sig_idx]
            close = float(last["Close"])
            if not np.isfinite(close) or close < FADE_MIN_PRICE:
                continue
            vol_avg = last.get("VolAvg20", np.nan)
            rsi = last.get("RSI14", np.nan)
            ret1 = last.get("Ret1", np.nan)
            vol = float(last["Volume"])
            is_fired = _variant_fired(last, v, gap)

            # reason for non-fired (first failing factor)
            reason = "All factors met" if is_fired else (
                f"Ret1={ret1:.2f}% (<{v['shoot_pct']})" if np.isfinite(ret1) and ret1 < v["shoot_pct"]
                else f"Vol {vol/vol_avg:.1f}x (<{v['vol_mult']})" if np.isfinite(vol_avg) and vol_avg > 0 and vol < vol_avg * v["vol_mult"]
                else f"RSI {rsi:.0f} (<{v['rsi_min']})" if np.isfinite(rsi) and rsi < v["rsi_min"]
                else f"No {v.get('extra', 'filter')}" if v.get("extra", "none") != "none"
                else f"Gap skip {gap:+.2f}%" if v.get("gap_max") is not None and np.isfinite(gap) and abs(gap) > v["gap_max"]
                else "No signal"
            )
            v_signals.append({
                "rank": v["rank"], "market": "NSE", "region": "INDIAN",
                "ticker": t, "direction": "SHORT",
                "factors": v["factors"], "win_rate": v["win_rate"],
                "trades_count": v["trades_count"], "close": close,
                "interval": interval,
                "sl_pct": v["sl_pct"], "tp_pct": v["tp_pct"],
                "max_hold_hours": 5,
                "fired": is_fired, "reason": reason,
                "signal_indicators": {
                    "Close": round(close, 2), "Volume": int(vol),
                    "VolAvg20": round(float(vol_avg), 0) if np.isfinite(vol_avg) else None,
                    "RSI14": round(float(rsi), 1) if np.isfinite(rsi) else None,
                    "Ret1_pct": round(float(ret1), 2) if np.isfinite(ret1) else None,
                    "BrkPrevHigh": bool(last.get("BrkPrevHigh", False)),
                    "NearDayHigh": bool(last.get("NearDayHigh", False)),
                    "NIFTY_gap_pct": round(float(gap), 2) if np.isfinite(gap) else None,
                },
            })
        # rank fired by signal strength; per-run bound = variant daily cap
        v_fired = [s for s in v_signals if s["fired"]]
        v_fired.sort(key=lambda s: -abs(s["signal_indicators"].get("Ret1_pct") or 0))
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

"""
FREE 3-Market v5.12 — NSE 1H FADE SCANNER
==========================================
"Big Player Exit Fade": when a stock shoots up fast (single 1h candle +3.5%
with volume 2.2x and RSI>=65 breaking the previous day's high), SHORT it.

The statistical edge (2-year clean OOS, 1680 NSE stocks, Aug 2024-Jul 2026):
  combo  1h, shoot 3.5%/1bar, vol 2.2x, RSI>=65, prev-day-high-break,
         gap skip <=1.5% (NIFTY), full day 09:30-15:00,
         SL 1.3% / TP 3.9% (RR 3), max 2 trades/day
  OOS    test 3028 signals, win 41.6%, cap2 (1-pos, 2/day) +4.15%/mo
         after 0.1% costs, 9/9 test months positive.

No-lookahead: signal on the LAST COMPLETED 1h candle; entry at that candle's
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
from datetime import datetime, timedelta
from config import (
    FADE_UNIVERSE_FILE, FADE_PERIOD, FADE_INTERVAL, FADE_SHOOT_PCT,
    FADE_VOL_MULT, FADE_RSI_MIN, FADE_GAP_MAX,
    FADE_MAX_TRADES_PER_DAY, FADE_RANK, INDIAN_TICKERS, FADE_MIN_PRICE,
)

IST = pytz.timezone("Asia/Kolkata")

# ── signal helpers ───────────────────────────────────────────────────────────


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


def _signal_candle_index(df: pd.DataFrame) -> int:
    """Index of the LAST COMPLETED 1h candle.

    yfinance can return the still-forming candle as the last row. Signals
    must only use completed candles (no lookahead) — same convention as
    scanner_intraday._signal_candle_index. Entry is at the signal candle's
    close (next candle open ~= current price when scanning after close).
    """
    try:
        if len(df) < 2:
            return len(df) - 1
        now = pd.Timestamp.utcnow()
        last_ts = df.index[-1]
        if last_ts.tzinfo is None:
            last_ts = last_ts.tz_localize("UTC")
        # If the last bar started within the current 1h window, it is still
        # forming → use the previous (completed) bar.
        if (now - last_ts).total_seconds() < 3600:
            return len(df) - 2
        return len(df) - 1
    except Exception:
        return len(df) - 1


def compute_fade_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Volume avg20 + RSI14 + prev-day-high-break on 1h data."""
    if df is None or len(df) < 40:
        return df
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    df["VolAvg20"] = df["Volume"].rolling(20).mean()
    df["RSI14"] = wilder_rsi(df["Close"])
    # previous trading day's high (per calendar day)
    df["_date"] = df.index.date
    day_high = df.groupby("_date")["High"].transform("max")
    prev_day_high = day_high.groupby(df["_date"]).shift(1)
    df["PrevDayHigh"] = prev_day_high
    df["BrkPrevHigh"] = df["Close"] > df["PrevDayHigh"]
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
    # Ensure core liquid NSE names are always included
    core = [t for t in INDIAN_TICKERS if not t.startswith("^")]
    merged = list(dict.fromkeys(syms + core))
    return merged


def _download_1h(ticker: str) -> pd.DataFrame:
    """Download 1h data with retries (yfinance is rate-limit prone)."""
    import time as _time
    for attempt in range(3):
        try:
            df = yf.download(ticker, period=FADE_PERIOD, interval=FADE_INTERVAL,
                             progress=False, auto_adjust=False)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            pass
        _time.sleep(1.5 * (attempt + 1))
    return None


# ── main scan ────────────────────────────────────────────────────────────────


def scan_fade(limit: int = None, dry: bool = False) -> dict:
    """
    Scan universe for 1h fade SHORT signals on the last completed candle.

    Returns dict compatible with bot.py scan_result format.
    """
    start = datetime.now(IST)
    date_str = start.strftime("%Y-%m-%d")
    time_str = start.strftime("%H:%M:%S IST")

    universe = load_fade_universe()
    if limit:
        universe = universe[:limit]
    print(f"[Fade] Universe: {len(universe)} stocks | {date_str} {time_str}")

    # NIFTY gap filter (market-wide skip)
    gap = nifty_day_gap_pct()
    # Fail-safe: if the index gap is unknown (download failure), SKIP entries
    # rather than trade blind on a possible crash/gap day.
    gap_skip = (not np.isfinite(gap)) or abs(gap) > FADE_GAP_MAX
    if not np.isfinite(gap):
        print("[Fade] WARNING: NIFTY gap unknown (download failed) — skipping entries (fail-safe)")
    if gap_skip:
        print(f"[Fade] SKIP ALL: NIFTY gap {gap:+.2f}% > {FADE_GAP_MAX}% threshold")

    ticker_data = {}
    scan_errors = 0
    with ThreadPoolExecutor(max_workers=8) as pool:
        futs = {pool.submit(_download_1h, t): t for t in universe}
        for fut in as_completed(futs):
            t = futs[fut]
            try:
                df = fut.result()
                if df is not None and len(df) > 40:
                    ticker_data[t] = compute_fade_indicators(df)
                else:
                    scan_errors += 1
            except Exception:
                scan_errors += 1

    print(f"[Fade] Data OK: {len(ticker_data)}/{len(universe)}, errors {scan_errors}")

    # ── signal detection on last COMPLETED candle (no lookahead) ──
    signals = []
    for t, df in ticker_data.items():
        sig_idx = _signal_candle_index(df)
        last = df.iloc[sig_idx]
        close = float(last["Close"])
        if not np.isfinite(close) or close < FADE_MIN_PRICE:
            continue
        vol_avg = last.get("VolAvg20", np.nan)
        rsi = last.get("RSI14", np.nan)
        ret1 = last.get("Ret1", np.nan)
        brk = bool(last.get("BrkPrevHigh", False))
        vol = float(last["Volume"])

        fired = (
            not gap_skip
            and np.isfinite(ret1) and ret1 >= FADE_SHOOT_PCT
            and np.isfinite(vol_avg) and vol_avg > 0 and vol >= vol_avg * FADE_VOL_MULT
            and np.isfinite(rsi) and rsi >= FADE_RSI_MIN
            and brk
        )
        reason = "All factors met" if fired else (
            f"Ret1={ret1:.2f}% (<{FADE_SHOOT_PCT})" if np.isfinite(ret1) and ret1 < FADE_SHOOT_PCT
            else f"Vol {vol/vol_avg:.1f}x (<{FADE_VOL_MULT})" if np.isfinite(vol_avg) and vol_avg > 0 and vol < vol_avg * FADE_VOL_MULT
            else f"RSI {rsi:.0f} (<{FADE_RSI_MIN})" if np.isfinite(rsi) and rsi < FADE_RSI_MIN
            else "No prev-high-break" if not brk
            else f"Gap skip {gap:+.2f}%" if gap_skip
            else "No signal"
        )
        signals.append({
            "rank": FADE_RANK, "market": "NSE", "region": "INDIAN",
            "ticker": t, "direction": "SHORT", "factors": "Fade: 1h +3.5%/vol2.2x/RSI65/prev-high",
            "win_rate": 41.61, "trades_count": 3028, "close": close,
            "fired": fired, "reason": reason,
            "signal_indicators": {
                "Close": round(close, 2), "Volume": int(vol),
                "VolAvg20": round(float(vol_avg), 0) if np.isfinite(vol_avg) else None,
                "RSI14": round(float(rsi), 1) if np.isfinite(rsi) else None,
                "Ret1_pct": round(float(ret1), 2) if np.isfinite(ret1) else None,
                "BrkPrevHigh": brk, "NIFTY_gap_pct": round(float(gap), 2) if np.isfinite(gap) else None,
            },
        })

    fired = [s for s in signals if s["fired"]]
    # rank candidates by signal strength; the true per-DAY cap is enforced in
    # bot.py (counts today's FADE_1h entries), this keeps a sane per-run bound
    # so a single cron can't flood entries before the daily counter applies.
    fired.sort(key=lambda s: -abs(s["signal_indicators"].get("Ret1_pct") or 0))
    fired = fired[:FADE_MAX_TRADES_PER_DAY]
    print(f"[Fade] Signals: {len(signals)}, fired: {len(fired)} (per-run cap {FADE_MAX_TRADES_PER_DAY})")
    if dry:
        print("[Fade] DRY RUN — no entries will be made")

    return {
        "mode": "FADE",
        "ticker_data": ticker_data,
        "market_status": {},
        "current_prices": {s["ticker"]: s["close"] for s in signals if s["fired"]},
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
    import sys
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    res = scan_fade(limit=a.limit, dry=a.dry)
    print(f"\n=== FADE SCAN (dry={a.dry}) ===")
    print(f"signals: {len(res['all_signals'])}, fired: {len(res['fired_signals'])}")
    for s in res["fired_signals"]:
        print(f"  SHORT {s['ticker']} @ {s['close']} | {s['reason']}")

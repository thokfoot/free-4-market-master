"""
FREE 3-Market v5.15 — MARKET DATA FALLBACK LAYER
================================================
Single entry point for OHLCV downloads with a resilient provider chain:

  1. yfinance        (primary — standard library path)
  2. Yahoo chart API (direct query1.finance.yahoo.com/v8/finance/chart —
                      bypasses yfinance's crumb/cookie session bug that causes
                      JSONDecodeError / 'symbol may be delisted' when Yahoo
                      rate-limits or invalidates the session cookie)
  3. Binance klines  (crypto only — tickers ending in -USD, e.g. BTC-USD ->
                      BTCUSDT; free public API, no key required)

Every provider returns a normalized DataFrame with columns
  Open, High, Low, Close, Volume
indexed by tz-aware UTC DatetimeIndex (bar timestamps), matching what
yfinance returns — so every consumer (scanner_fade, bot, gap_down, replay,
live_pnl) works unchanged.

Provider selection is automatic: try yfinance; on ANY failure (exception,
empty frame, JSONDecodeError) fall through to the next provider. When a
fallback is used, a warning is printed so the bot log shows the source.
"""
import os
import time
import pandas as pd
import numpy as np

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# period string -> (range, interval) mapping for the direct chart API
# yfinance 'period' (e.g. "60d") works as-is for the chart 'range' param.
# yfinance 'interval' works as-is too ("15m", "5m", "1h", "1d", "1m").
_CRYPTO_SUFFIX = "-USD"


def _empty(interval: str) -> pd.DataFrame:
    return pd.DataFrame(columns=["Open", "High", "Low", "Close", "Volume"],
                        index=pd.DatetimeIndex([], name="Date"))


def _normalize(raw: dict, interval: str) -> pd.DataFrame:
    """Normalize a Yahoo chart-API JSON payload into a yfinance-like frame."""
    res = (raw.get("chart") or {}).get("result") or []
    if not res:
        return _empty(interval)
    ts = res[0].get("timestamp") or []
    q = ((res[0].get("indicators") or {}).get("quote") or [{}])[0]
    if not ts:
        return _empty(interval)
    df = pd.DataFrame({
        "Open": q.get("open"), "High": q.get("high"),
        "Low": q.get("low"), "Close": q.get("close"),
        "Volume": q.get("volume"),
    }, index=pd.to_datetime(ts, unit="s", utc=True))
    df = df.dropna(subset=["Close"])
    df = df[(df[["Open", "High", "Low", "Close"]] > 0).all(axis=1)]
    return df


def _yahoo_chart_direct(ticker: str, interval: str, period: str = None,
                        start=None, end=None) -> pd.DataFrame:
    """Direct Yahoo chart API — bypasses yfinance crumb/cookie session bug.

    Accepts either a `period` range ("5d", "60d", "3mo") or explicit
    start/end timestamps (replay/backfill usage). Prices are split+dividend
    adjusted (adjust=true) so split days never look like -90% crashes.
    """
    import requests
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{ticker}"
    params = {"interval": interval,
              "includePrePost": "false", "events": "div,splits", "adjust": "true"}
    if start is not None or end is not None:
        if start is not None:
            params["period1"] = int(pd.Timestamp(start).timestamp())
        if end is not None:
            params["period2"] = int(pd.Timestamp(end).timestamp())
    else:
        params["range"] = period or "60d"
    for attempt in range(3):
        try:
            r = requests.get(url, params=params, headers=UA, timeout=20)
            if r.status_code != 200:
                time.sleep(1.0)
                continue
            df = _normalize(r.json(), interval)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            time.sleep(1.0)
    return _empty(interval)


def _binance_crypto(ticker: str, interval: str, period: str) -> pd.DataFrame:
    """Binance public klines — crypto only. BTC-USD -> BTCUSDT, etc."""
    import requests
    if not ticker.endswith(_CRYPTO_SUFFIX):
        return _empty(interval)
    sym = ticker.replace("-USD", "USDT")
    iv_map = {"1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
              "1h": "1h", "1d": "1d"}
    iv = iv_map.get(interval)
    if iv is None:
        return _empty(interval)
    # Binance klines limit is 1000 per call; period roughly maps to bars
    n_bars = {"1m": 1440, "5m": 288, "15m": 96, "30m": 48,
              "1h": 24, "1d": 1}.get(interval, 100) * max(1, int(_days(period)))
    n_bars = max(100, min(1000, n_bars))
    url = "https://api.binance.com/api/v3/klines"
    for attempt in range(3):
        try:
            r = requests.get(url, params={"symbol": sym, "interval": iv,
                                          "limit": n_bars}, timeout=20)
            if r.status_code != 200:
                time.sleep(1.0)
                continue
            rows = r.json()
            if not rows:
                return _empty(interval)
            df = pd.DataFrame(rows, columns=[
                "open_time", "Open", "High", "Low", "Close", "Volume",
                "close_time", "qav", "trades", "tbav", "tbq", "ignore"])
            df = df[["open_time", "Open", "High", "Low", "Close", "Volume"]]
            df["Open"] = df["Open"].astype(float)
            df["High"] = df["High"].astype(float)
            df["Low"] = df["Low"].astype(float)
            df["Close"] = df["Close"].astype(float)
            df["Volume"] = df["Volume"].astype(float)
            df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True)
            df = df[["Open", "High", "Low", "Close", "Volume"]]
            return df
        except Exception:
            time.sleep(1.0)
    return _empty(interval)


def _days(period: str) -> int:
    """Approximate calendar days from a yfinance period string (60d, 3mo, 2y)."""
    p = str(period).strip().lower()
    try:
        if p.endswith("d"):
            return int(p[:-1])
        if p.endswith("mo"):
            return int(p[:-2]) * 30
        if p.endswith("y"):
            return int(p[:-1]) * 365
    except Exception:
        pass
    return 60


def download(ticker: str, interval: str = "15m", period: str = "60d",
             start=None, end=None) -> pd.DataFrame:
    """Download OHLCV with automatic provider fallback.

    Accepts yfinance-style kwargs: `period` ("5d", "60d", "3mo") OR
    `start`/`end` (dates/timestamps, replay/backfill usage). Returns a
    normalized yfinance-like DataFrame (Open/High/Low/Close/Volume, tz-aware
    UTC index) or an empty DataFrame when every provider fails — never raises.
    """
    # ── 1) yfinance ──
    try:
        import yfinance as yf
        kw = dict(interval=interval, progress=False, auto_adjust=False, threads=False)
        if start is not None or end is not None:
            if start is not None:
                kw["start"] = pd.Timestamp(start).strftime("%Y-%m-%d")
            if end is not None:
                kw["end"] = pd.Timestamp(end).strftime("%Y-%m-%d")
        else:
            kw["period"] = period
        df = yf.download(ticker, **kw)
        if df is not None and len(df) > 0:
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
            df = df[~df.index.duplicated(keep="first")].sort_index()
            return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        print(f"[MarketData] yfinance failed for {ticker}: {type(e).__name__} {str(e)[:80]}")

    # ── 2) direct Yahoo chart API (bypasses yfinance crumb bug) ──
    df = _yahoo_chart_direct(ticker, interval, period, start=start, end=end)
    if df is not None and len(df) > 0:
        print(f"[MarketData] {ticker} {interval}: used DIRECT Yahoo chart API "
              f"({len(df)} bars)")
        return df

    # ── 3) Binance (crypto only) ──
    df = _binance_crypto(ticker, interval, period)
    if df is not None and len(df) > 0:
        print(f"[MarketData] {ticker} {interval}: used BINANCE API ({len(df)} bars)")
        return df

    print(f"[MarketData] ALL providers failed for {ticker} {interval} {period}")
    return _empty(interval)

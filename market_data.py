"""
FREE 3-Market v5.15 — MARKET DATA FALLBACK LAYER
================================================
Single entry point for OHLCV downloads with a resilient provider chain:

  1. yfinance        (primary — standard library path)
  2. Yahoo chart API (direct query1.finance.yahoo.com/v8/finance/chart —
                      bypasses yfinance's crumb/cookie session bug that causes
                      JSONDecodeError / 'symbol may be delisted' when Yahoo
                      rate-limits or invalidates the session cookie)
  3. Yahoo chart API host rotation (query1 <-> query2 + rotating UAs —
                      defeats shared-runner IP rate limiting, the #1 cause of
                      partial scans like 'Data 39/739')
  4. Nasdaq API (US stocks/ETFs daily OHLCV — api.nasdaq.com, no key,
                      full OHLCV, used when Yahoo is fully down for 1d bars)
  5. Binance klines  (crypto only — tickers ending in -USD, e.g. BTC-USD ->
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
import os, json, threading, time, gzip, hashlib, re
import pandas as pd
import numpy as np

UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Accept": "application/json,text/plain,*/*",
    "Accept-Language": "en-US,en;q=0.9",
}
# Rotating user-agents: shared runner IPs get rate-limited by Yahoo far less
# when each request presents a different client fingerprint.
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:120.0) Gecko/20100101 Firefox/120.0",
]

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
    params = {"interval": interval,
              "includePrePost": "false", "events": "div,splits", "adjust": "true"}
    if start is not None or end is not None:
        if start is not None:
            params["period1"] = int(pd.Timestamp(start).timestamp())
        if end is not None:
            params["period2"] = int(pd.Timestamp(end).timestamp())
    else:
        params["range"] = period or "60d"
    # Rotate query1 <-> query2 hosts AND user-agents: on shared GitHub runner
    # IPs a single host+UA gets rate-limited hard (the old 39/739 partial
    # scans). Each host gets 3 attempts with a fresh UA per attempt.
    for host in ("query1", "query2"):
        url = f"https://{host}.finance.yahoo.com/v8/finance/chart/{ticker}"
        for attempt in range(3):
            try:
                hdr = dict(UA)
                hdr["User-Agent"] = _UA_POOL[(attempt + int(time.time())) % 4]
                r = requests.get(url, params=params, headers=hdr, timeout=20)
                if r.status_code != 200:
                    time.sleep(1.0)
                    continue
                df = _normalize(r.json(), interval)
                if df is not None and len(df) > 0:
                    return df
            except Exception:
                time.sleep(1.0)
    return _empty(interval)


def _nasdaq_daily(ticker: str, period: str = "60d") -> pd.DataFrame:
    """US stock/ETF daily OHLCV from api.nasdaq.com (no key, full OHLCV).

    Used as a non-Yahoo 1d fallback when both Yahoo providers fail. Covers
    plain US symbols (AAPL, QQQ, SPY, ...); NOT applicable to India (.NS),
    crypto (-USD) or indices (^ prefix).
    """
    import requests, re as _re
    try:
        from datetime import datetime as _dt, timedelta as _td
        match = _re.search(r"(\d+)[dD]", period or "")
        days = int(match.group(1)) if match else 60
        fromdate = (_dt.utcnow() - _td(days=days)).strftime("%Y-%m-%d")
        rows_all = []
        for assetclass in ("stocks", "etfs"):
            try:
                r = requests.get(
                    "https://api.nasdaq.com/api/quote/%s/historical" % ticker,
                    params={"assetclass": assetclass, "fromdate": fromdate, "limit": 9999},
                    headers={"User-Agent": UA["User-Agent"],
                             "Accept": "application/json, text/plain, */*"},
                    timeout=20)
                if r.status_code != 200:
                    continue
                tbl = (r.json().get("data") or {}).get("tradesTable") or {}
                rows_all.extend(tbl.get("rows") or [])
                if rows_all:
                    break
            except Exception:
                continue
        if not rows_all:
            return _empty("1d")
        recs = []
        for row in rows_all:
            try:
                dt = pd.to_datetime(row.get("date"), format="%m/%d/%Y", utc=True)
                o = float(str(row.get("open", "")).replace("$", "").replace(",", ""))
                h = float(str(row.get("high", "")).replace("$", "").replace(",", ""))
                lo = float(str(row.get("low", "")).replace("$", "").replace(",", ""))
                c = float(str(row.get("close", "")).replace("$", "").replace(",", ""))
                v = int(float(str(row.get("volume", "0")).replace(",", "").replace("N/A", "0") or 0))
                if o > 0 and h > 0 and lo > 0 and c > 0:
                    recs.append({"Date": dt, "Open": o, "High": h, "Low": lo,
                                 "Close": c, "Volume": v})
            except (TypeError, ValueError):
                continue
        if not recs:
            return _empty("1d")
        df = pd.DataFrame(recs).set_index("Date").sort_index()
        return df[["Open", "High", "Low", "Close", "Volume"]]
    except Exception:
        return _empty("1d")


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


def _archive_audit_bars(ticker: str, interval: str, df: pd.DataFrame) -> None:
    """Persist fetched bars for reproducible future paper-trade audits."""
    if os.environ.get("PAPER_AUDIT_ARCHIVE", "0") != "1" or df is None or len(df) == 0:
        return
    try:
        stamp = pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d")
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", ticker)
        folder = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs", "candle_evidence", stamp)
        os.makedirs(folder, exist_ok=True)
        payload = {
            "ticker": ticker,
            "interval": interval,
            "captured_at_utc": pd.Timestamp.now(tz="UTC").isoformat(),
            "columns": ["Open", "High", "Low", "Close", "Volume"],
            "bars": [[str(ts), *[float(row[c]) for c in ("Open", "High", "Low", "Close", "Volume")]]
                     for ts, row in df.tail(_CACHE_MAX_BARS).iterrows()],
        }
        raw = json.dumps(payload, separators=(",", ":"), default=str).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        path = os.path.join(folder, f"{safe}_{interval}.json.gz")
        if os.path.exists(path):
            return
        with gzip.open(path, "wb") as f:
            f.write(raw)
        with open(path + ".sha256", "w", encoding="ascii") as f:
            f.write(digest)
    except Exception as exc:
        print(f"[MarketData] audit archive failed for {ticker} {interval}: {exc}")


# ─────────────────────────────────────────────────────────────
# Persistent OHLC cache — survives GitHub ephemeral runners via
# the actions/cache "ohlc-cache" steps (NOT committed to git).
#   * fresh hit  -> no network call
#   * success    -> cache updated (bounded to last N bars)
#   * all-fail   -> last cached bars returned as STALE fallback
#                   (rate-limited runner IPs still get data)
# ─────────────────────────────────────────────────────────────
_CACHE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "data", "ohlc_cache.json")
_CACHE_TTL_SEC = {"1d": 26*3600, "1h": 75*60, "30m": 40*60, "15m": 20*60,
                  "10m": 14*60, "5m": 8*60, "3m": 5*60, "1m": 5*60}
_CACHE_MAX_STALE = {"1d": 7*86400, "1h": 86400, "30m": 12*3600, "15m": 4*3600,
                    "10m": 3*3600, "5m": 2*3600, "3m": 3600, "1m": 1800}
_CACHE_MAX_BARS = 210
_cache = None
_cache_lock = threading.RLock()  # reentrant: _load_cache() called inside with-lock
_cache_last_save = 0.0


def _load_cache() -> dict:
    global _cache
    with _cache_lock:
        if _cache is not None:
            return _cache
        _cache = {}
        try:
            if os.path.exists(_CACHE_FILE):
                with open(_CACHE_FILE, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    _cache = {k: v for k, v in data.items()
                              if isinstance(v, dict) and v.get("bars")}
        except Exception:
            _cache = {}
        return _cache


def _save_cache():
    global _cache_last_save
    try:
        os.makedirs(os.path.dirname(_CACHE_FILE), exist_ok=True)
        tmp = _CACHE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_cache, f, separators=(",", ":"))
        os.replace(tmp, _CACHE_FILE)
        _cache_last_save = time.time()
    except Exception as e:
        print(f"[MarketData] cache save failed: {e}")


def _df_to_bars(df) -> list:
    bars = []
    for ts, row in df.iterrows():
        bars.append([str(ts), float(row["Open"]), float(row["High"]),
                     float(row["Low"]), float(row["Close"]), float(row["Volume"])])
    return bars[-_CACHE_MAX_BARS:]


def _bars_to_df(bars, interval) -> pd.DataFrame:
    idx = pd.to_datetime([b[0] for b in bars], utc=True)
    return pd.DataFrame([b[1:] for b in bars],
                        columns=["Open", "High", "Low", "Close", "Volume"],
                        index=idx).sort_index()


def download(ticker: str, interval: str = "15m", period: str = "60d",
             start=None, end=None, force_refresh: bool = False) -> pd.DataFrame:
    """Download OHLCV with automatic provider fallback + persistent cache.

    The cache (data/ohlc_cache.json) is restored/saved by the GitHub Actions
    "ohlc-cache" steps. A fresh cache hit avoids the network entirely; when
    every provider fails, the last cached bars are returned as a STALE
    fallback (bounded by _CACHE_MAX_STALE) so scans still evaluate on
    rate-limited shared runner IPs (was: 39/739 tickers OK).
    """
    cache_key = f"{ticker}|{interval}|{period}"
    ttl = _CACHE_TTL_SEC.get(interval, 600)
    cache = _load_cache()
    now = time.time()
    entry = cache.get(cache_key)

    # ── 0) fresh cache hit (period-based calls only; replay uses start/end) ──
    # force_refresh=True (IPO daily scan) always goes to the network so a
    # listing-dip cross is never masked by a stale cache entry.
    if not force_refresh and start is None and end is None and entry and entry.get("bars"):
        if (now - entry.get("ts", 0)) < ttl:
            df = _bars_to_df(entry["bars"], interval)
            if len(df) > 0:
                print(f"[MarketData] {ticker} {interval}: CACHE hit ({len(df)} bars)")
                return df

    df = None
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
        d = yf.download(ticker, **kw)
        if d is not None and len(d) > 0:
            if isinstance(d.columns, pd.MultiIndex):
                d.columns = d.columns.get_level_values(0)
            d = d[~d.index.duplicated(keep="first")].sort_index()
            df = d[["Open", "High", "Low", "Close", "Volume"]]
    except Exception as e:
        print(f"[MarketData] yfinance failed for {ticker}: {type(e).__name__} {str(e)[:80]}")

    # ── 2) direct Yahoo chart API (bypasses yfinance crumb bug) ──
    if df is None or len(df) == 0:
        df = _yahoo_chart_direct(ticker, interval, period, start=start, end=end)
        if df is not None and len(df) > 0:
            print(f"[MarketData] {ticker} {interval}: used DIRECT Yahoo chart API "
                  f"({len(df)} bars)")

    # ── 2.5) Nasdaq API (US stocks/ETFs, 1d only, non-Yahoo source) ──
    if df is None or len(df) == 0 and interval == "1d"             and not ticker.endswith(".NS") and "-USD" not in ticker             and not ticker.startswith("^"):
        df = _nasdaq_daily(ticker, period)
        if df is not None and len(df) > 0:
            print(f"[MarketData] {ticker} {interval}: used NASDAQ API ({len(df)} bars)")

    # ── 3) Binance (crypto only) ──
    if df is None or len(df) == 0:
        df = _binance_crypto(ticker, interval, period)
        if df is not None and len(df) > 0:
            print(f"[MarketData] {ticker} {interval}: used BINANCE API ({len(df)} bars)")

    # ── 4) persist successful download to cache ──
    if df is not None and len(df) > 0:
        _archive_audit_bars(ticker, interval, df)
        try:
            with _cache_lock:
                c = _load_cache()
                c[cache_key] = {"ts": time.time(), "bars": _df_to_bars(df)}
                if time.time() - _cache_last_save >= 30:
                    _save_cache()
        except Exception as e:
            print(f"[MarketData] cache update failed: {e}")
        return df

    # ── 5) stale cache fallback (rate-limited runner) ──
    if entry and entry.get("bars") and (now - entry.get("ts", 0)) < _CACHE_MAX_STALE.get(interval, 6*3600):
        stale = _bars_to_df(entry["bars"], interval)
        if len(stale) > 0:
            print(f"[MarketData] {ticker} {interval}: STALE cache fallback ({len(stale)} bars)")
            return stale

    print(f"[MarketData] ALL providers failed for {ticker} {interval} {period}")
    return _empty(interval)

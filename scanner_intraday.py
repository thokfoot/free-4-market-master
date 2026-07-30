"""
FREE 3-Market v5.7 — INTRADAY STRATEGY SCANNER
===============================================
Loads 40 verified intraday (1h) strategies from CSV, downloads 1h data
from yfinance, computes indicators (adjust=False), checks pattern conditions
on the latest 1-2 candles, returns fired signals.

Author: Finance Manager
"""
import pandas as pd
import numpy as np
import os, re
from config import (
    INTRADAY_STRATEGY_FILE, TICKER_MAP, INTRADAY_PERIOD, INTRADAY_INTERVAL,
    ALLOW_SHORT, INTRADAY_CAPITAL,
)


def load_intraday_strategies() -> pd.DataFrame:
    """Load the 40 intraday strategies from CSV."""
    if not os.path.exists(INTRADAY_STRATEGY_FILE):
        raise FileNotFoundError(f"Intraday strategy file not found: {INTRADAY_STRATEGY_FILE}")
    df = pd.read_csv(INTRADAY_STRATEGY_FILE, on_bad_lines='warn')
    required = ["Final_Rank", "Market", "Region", "Factors", "Direction", "AvgWin%", "Trades"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing column {col} in intraday strategies CSV")
    df = df.dropna(subset=["Factors"])
    print(f"[IntradayScanner] Loaded {len(df)} intraday strategies from {INTRADAY_STRATEGY_FILE}")
    return df


def get_yf_ticker(csv_market: str) -> str:
    """Map CSV market name to Yahoo Finance ticker."""
    if csv_market in TICKER_MAP:
        return TICKER_MAP[csv_market]
    for key in TICKER_MAP:
        if key.lower() in csv_market.lower():
            return TICKER_MAP[key]
    print(f"[IntradayScanner] WARNING: No ticker mapping for '{csv_market}'")
    return None


def unique_tickers(strategies: pd.DataFrame) -> list:
    """Get unique yfinance tickers to scan for intraday."""
    tickers = set()
    for mkt in strategies["Market"].unique():
        yft = get_yf_ticker(mkt)
        if yft:
            tickers.add(yft)
    return sorted(tickers)


def compute_indicators_1h(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute ALL indicators on 1h dataframe.
    Uses CORRECT formulas: adjust=False for EMA, Wilder's RSI.
    Same as scanner.py but for 1h data.
    """
    if df is None or len(df) < 200:
        return df

    df = df.copy()

    # Handle multi-index columns from yfinance
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)

    df = df.dropna(subset=["Close"])
    if len(df) < 200:
        return df

    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    op = df["Open"]

    # SMAs
    df["SMA20"] = close.rolling(20).mean()
    df["SMA50"] = close.rolling(50).mean()

    # EMAs (adjust=False — CORRECT formula)
    df["EMA9"] = close.ewm(span=9, adjust=False).mean()
    df["EMA20"] = close.ewm(span=20, adjust=False).mean()
    df["EMA50"] = close.ewm(span=50, adjust=False).mean()

    # RSI14 (Wilder's — CORRECT formula)
    delta = close.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = -delta.where(delta < 0, 0.0)
    avg_gain = gain.ewm(alpha=1/14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    df["RSI14"] = 100 - (100 / (1 + rs))

    # Range
    df["Range"] = (high - low) / close

    # Returns
    df["Ret"] = close.pct_change(fill_method=None)

    # 2Red: 2 consecutive red candles
    df["2Red"] = (df["Ret"] < 0) & (df["Ret"].shift(1) < 0)

    # Next_Ret (next 1h return for validation)
    df["Next_Ret"] = close.shift(-1) / close - 1

    df = df.bfill()
    return df


def _resolve_value_1h(row: pd.Series, expr: str) -> float:
    """Resolve a factor expression to a numeric value (same as scanner.py)."""
    col_map = {
        "Price": "Close", "Close": "Close", "Open": "Open",
        "High": "High", "Low": "Low", "Volume": "Volume",
        "SMA20": "SMA20", "SMA50": "SMA50",
        "EMA9": "EMA9", "EMA20": "EMA20", "EMA50": "EMA50",
        "RSI": "RSI14", "RSI14": "RSI14",
        "Range": "Range", "Ret": "Ret",
    }
    if expr.endswith("%"):
        try:
            return float(expr.rstrip("%")) / 100.0
        except:
            return None
    try:
        return float(expr)
    except:
        pass
    clean = expr.strip()
    if clean in col_map:
        col = col_map[clean]
        if col in row.index and pd.notna(row[col]):
            return float(row[col])
    return None


def compute_signal_1h(df: pd.DataFrame, factors_str: str, direction: str) -> bool:
    """
    Check if a strategy's factors are met on the latest COMPLETED 1h candle.
    
    Uses the last COMPLETED candle (not the forming one) to ensure reliable signals.
    A 1h candle is complete when its end time is in the past.
    """
    if df is None or len(df) < 3:
        return False

    # ── Determine which candle to use (last COMPLETED, not forming) ──
    last_candle_time = df.index[-1]
    candle_end = last_candle_time + pd.Timedelta(hours=1)
    # Get timezone-aware current time matching the dataframe's timezone
    now_utc = pd.Timestamp.now(tz='UTC')
    if last_candle_time.tz is not None:
        now_utc = now_utc.tz_convert(last_candle_time.tz)
    else:
        # Candle index is timezone-naive — make now_utc naive for comparison
        now_utc = now_utc.tz_localize(None)
    
    if candle_end > now_utc:
        # Latest candle is still forming — use the last COMPLETED candle
        check_idx = -2
    else:
        # Latest candle is complete — use it
        check_idx = -1
    
    last = df.iloc[check_idx]
    factors = [f.strip() for f in factors_str.split("+")]

    for factor in factors:
        if factor == "2Red":
            if not (last["2Red"] == True):
                return False
            continue

        match = re.match(r"^([A-Za-z0-9_.%]+)([<>])(.+)$", factor)
        if not match:
            return False

        left_str = match.group(1)
        operator = match.group(2)
        right_str = match.group(3)

        left_val = _resolve_value_1h(last, left_str)
        if left_val is None:
            return False
        right_val = _resolve_value_1h(last, right_str)
        if right_val is None:
            return False

        if operator == "<":
            if not (left_val < right_val):
                return False
        elif operator == ">":
            if not (left_val > right_val):
                return False
        else:
            return False

    return True


def scan_intraday_strategies(strategies: pd.DataFrame, ticker_data: dict) -> list:
    """
    Check all intraday strategies against current 1h market data.

    Args:
        strategies: DataFrame from load_intraday_strategies()
        ticker_data: {yf_ticker: computed_1h_df}

    Returns:
        List of fired signals: [{rank, market, ticker, direction, factors, win_rate, region, close, fired}]
    """
    signals = []

    for _, strat in strategies.iterrows():
        rank = int(strat["Final_Rank"])
        market = str(strat["Market"])
        region = str(strat["Region"])
        factors = str(strat["Factors"])
        direction = str(strat["Direction"])
        win_rate = float(strat["AvgWin%"])
        trades_count = int(strat["Trades"])

        yf_ticker = get_yf_ticker(market)
        if not yf_ticker or yf_ticker not in ticker_data:
            signals.append({
                "rank": rank, "market": market, "ticker": yf_ticker or market,
                "direction": direction, "factors": factors,
                "win_rate": win_rate, "trades_count": trades_count,
                "region": region, "close": 0, "fired": False,
                "reason": "No data",
            })
            continue

        df = ticker_data[yf_ticker]
        if df is None or len(df) < 200:
            signals.append({
                "rank": rank, "market": market, "ticker": yf_ticker,
                "direction": direction, "factors": factors,
                "win_rate": win_rate, "trades_count": trades_count,
                "region": region, "close": 0, "fired": False,
                "reason": "Insufficient data",
            })
            continue

        close_price = float(df.iloc[-1]["Close"])
        fired = compute_signal_1h(df, factors, direction)

        # ── Signal Snapshot: Capture indicator values at signal time ──
        signal_indicators = None
        if fired:
            try:
                # Use the same COMPLETED candle that compute_signal_1h uses
                # Replicate the candle selection logic:
                last_candle_time = df.index[-1]
                candle_end = last_candle_time + pd.Timedelta(hours=1)
                now_utc = pd.Timestamp.now(tz='UTC')
                if last_candle_time.tz is not None:
                    now_utc = now_utc.tz_convert(last_candle_time.tz)
                else:
                    now_utc = now_utc.tz_localize(None)
                snap_idx = -2 if candle_end > now_utc else -1
                last = df.iloc[snap_idx]
                
                inds = {"Close", "Open", "High", "Low", "Volume",
                        "SMA20", "SMA50", "EMA9", "EMA20", "EMA50",
                        "RSI14", "Ret", "2Red"}
                snap = {}
                for col in inds:
                    if col in last.index and pd.notna(last[col]):
                        v = last[col]
                        if hasattr(v, 'iloc'):
                            v = float(v.iloc[0])
                        snap[col] = round(float(v), 6)
                signal_indicators = snap
            except Exception as e:
                print(f"[IntradayScanner] Could not capture signal snapshot for {yf_ticker}: {e}")

        signals.append({
            "rank": rank,
            "market": market,
            "ticker": yf_ticker,
            "direction": direction,
            "factors": factors,
            "win_rate": win_rate,
            "trades_count": trades_count,
            "region": region,
            "close": close_price,
            "fired": fired,
            "reason": "All factors met" if fired else "",
            "signal_indicators": signal_indicators,
        })

    return signals


def get_best_intraday_entries(signals: list) -> list:
    """
    From all fired intraday signals, pick one entry per ticker per direction.
    For each ticker, picks the pattern with the highest win rate.
    """
    fired = [s for s in signals if s["fired"]]

    best = {}
    for s in fired:
        key = (s["ticker"], s["direction"])
        if key not in best or s["win_rate"] > best[key]["win_rate"]:
            best[key] = s

    entries = sorted(best.values(), key=lambda x: x["win_rate"], reverse=True)

    # Respect ALLOW_SHORT
    filtered = []
    for e in entries:
        if e["direction"] == "SHORT":
            region_key = e.get("region", "US").upper()
            if region_key == "INDIA":
                region_key = "INDIAN"
            if not ALLOW_SHORT.get(region_key, True):
                print(f"[IntradayScanner] SHORT not allowed for {region_key}, skipping {e['ticker']}")
                continue
        filtered.append(e)

    return filtered

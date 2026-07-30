"""
FREE 3-Market v5.0 — STRATEGY SCANNER
======================================
Loads 81 verified strategies from CSV, computes indicators on live data,
checks all pattern conditions, returns fired signals.
"""

import pandas as pd
import numpy as np
import os, re
from config import STRATEGY_FILE, TICKER_MAP, YF_PERIOD, YF_INTERVAL, ALLOW_SHORT


def load_strategies() -> pd.DataFrame:
    """Load the 81 strategies from CSV."""
    if not os.path.exists(STRATEGY_FILE):
        raise FileNotFoundError(f"Strategy file not found: {STRATEGY_FILE}")
    df = pd.read_csv(STRATEGY_FILE, on_bad_lines='warn')
    # Ensure required columns
    required = ["Final_Rank", "Market", "Region", "Factors", "Direction", "AvgWin%", "Trades"]
    for col in required:
        if col not in df.columns:
            raise ValueError(f"Missing column {col} in strategies CSV")
    df = df.dropna(subset=["Factors"])
    print(f"[Scanner] Loaded {len(df)} strategies from {STRATEGY_FILE}")
    return df


def get_yf_ticker(csv_market: str) -> str:
    """Map CSV market name to Yahoo Finance ticker."""
    # Direct match
    if csv_market in TICKER_MAP:
        return TICKER_MAP[csv_market]
    # Partial match (e.g., "Bank Nifty Yahoo" -> "Bank Nifty")
    for key in TICKER_MAP:
        if key.lower() in csv_market.lower():
            return TICKER_MAP[key]
    print(f"[Scanner] WARNING: No ticker mapping for '{csv_market}'")
    return None


def unique_tickers(strategies: pd.DataFrame) -> list:
    """Get unique yfinance tickers to scan."""
    tickers = set()
    for mkt in strategies["Market"].unique():
        yft = get_yf_ticker(mkt)
        if yft:
            tickers.add(yft)
    return sorted(tickers)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """
    Compute ALL indicators on the dataframe.
    Uses CORRECT formulas: adjust=False for EMA, Wilder's RSI.
    """
    if df is None or len(df) < 60:
        return df
    
    # Work on a copy to avoid SettingWithCopyWarning
    df = df.copy()
    
    # Handle multi-index columns from yfinance
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    
    # Drop rows where Close is NaN (happens for Indian indices on latest day)
    df = df.dropna(subset=["Close"])
    if len(df) < 60:
        return df
    
    close = df["Close"]
    high = df["High"]
    low = df["Low"]
    op = df["Open"]
    vol = df["Volume"]
    
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
    
    # Returns (explicit fill_method=None to suppress FutureWarning)
    df["Ret"] = close.pct_change(fill_method=None)
    
    # 2Red: 2 consecutive red days
    df["2Red"] = (df["Ret"] < 0) & (df["Ret"].shift(1) < 0)
    
    # Next_Ret (for backtest only, not used for live signal)
    df["Next_Ret"] = close.shift(-1) / close - 1
    
    # Forward fill any remaining NaN from indicator computation
    df = df.bfill()
    
    return df


def compute_signal(df: pd.DataFrame, factors_str: str, direction: str) -> bool:
    """
    Check if a strategy's factors are met on the latest candle.
    
    Args:
        df: DataFrame with computed indicators (latest row is current)
        factors_str: e.g. "Price<SMA50+EMA9>EMA20+Range>1.5%"
        direction: "LONG" or "SHORT" (not used for signal, but for logging)
    
    Returns:
        True if all factors are satisfied on latest row
    """
    if df is None or len(df) < 2:
        return False
    
    last = df.iloc[-1]
    
    # Split into individual factor strings
    factors = [f.strip() for f in factors_str.split("+")]
    
    for factor in factors:
        # Handle special factors
        if factor == "2Red":
            # Check if last 2 candles are red
            if not (last["2Red"] == True):
                return False
            continue
        
        # Parse "A<B" or "A>B" format
        # Match pattern: Left <operator> Right
        match = re.match(r"^([A-Za-z0-9_.%]+)([<>])(.+)$", factor)
        if not match:
            print(f"[Scanner] WARNING: Cannot parse factor '{factor}'")
            return False
        
        left_str = match.group(1)
        operator = match.group(2)
        right_str = match.group(3)
        
        # Resolve left value
        left_val = _resolve_value(last, left_str)
        if left_val is None:
            return False
        
        # Resolve right value
        right_val = _resolve_value(last, right_str)
        if right_val is None:
            return False
        
        # Compare
        if operator == "<":
            if not (left_val < right_val):
                return False
        elif operator == ">":
            if not (left_val > right_val):
                return False
        else:
            return False
    
    return True


def _resolve_value(row: pd.Series, expr: str) -> float:
    """
    Resolve a factor expression to a numeric value.
    
    Examples:
        "Price" -> row["Close"]
        "SMA50" -> row["SMA50"]
        "EMA9" -> row["EMA9"]
        "1.5%" -> 0.015
        "65" -> 65.0
        "Close" -> row["Close"]
        "Open" -> row["Open"]
    """
    # Column name aliases
    col_map = {
        "Price": "Close",
        "Close": "Close",
        "Open": "Open",
        "High": "High",
        "Low": "Low",
        "Volume": "Volume",
        "SMA20": "SMA20",
        "SMA50": "SMA50",
        "EMA9": "EMA9",
        "EMA20": "EMA20",
        "EMA50": "EMA50",
        "RSI": "RSI14",
        "RSI14": "RSI14",
        "Range": "Range",
        "Ret": "Ret",
    }
    
    # Check if it's a percentage value (ends with %)
    if expr.endswith("%"):
        try:
            num = float(expr.rstrip("%"))
            return num / 100.0
        except:
            return None
    
    # Check if it's a numeric literal
    try:
        return float(expr)
    except:
        pass
    
    # Check if it's a column alias
    clean = expr.strip()
    if clean in col_map:
        col = col_map[clean]
        if col in row.index and pd.notna(row[col]):
            return float(row[col])
    
    return None


def scan_strategies(strategies: pd.DataFrame, ticker_data: dict) -> list:
    """
    Check all strategies against current market data.
    
    Args:
        strategies: DataFrame from load_strategies()
        ticker_data: {yf_ticker: computed_df}
    
    Returns:
        List of fired signals: [{
            "rank": int,
            "market": str,
            "ticker": str,
            "direction": str,
            "factors": str,
            "win_rate": float,
            "trades_count": int,
            "region": str,
            "close": float,
            "fired": bool,
        }]
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
        
        # Get yfinance ticker
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
        if df is None or len(df) < 60:
            signals.append({
                "rank": rank, "market": market, "ticker": yf_ticker,
                "direction": direction, "factors": factors,
                "win_rate": win_rate, "trades_count": trades_count,
                "region": region, "close": 0, "fired": False,
                "reason": "Insufficient data",
            })
            continue
        
        close_price = float(df.iloc[-1]["Close"])
        fired = compute_signal(df, factors, direction)
        
        # ── Signal Snapshot: Capture indicator values at signal time ──
        # This is saved permanently so entries are verifiable forever
        # (even after yfinance adjusts historical data).
        signal_indicators = None
        if fired:
            try:
                last = df.iloc[-1]
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
                print(f"[Scanner] Could not capture signal snapshot for {yf_ticker}: {e}")
        
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


def get_best_entries(signals: list) -> list:
    """
    From all fired signals, pick one entry per ticker per day.
    For each ticker, picks the pattern with the highest win rate.
    
    Returns:
        List of entry dicts: [{ticker, direction, close, win_rate, rank, market, region, factors}]
    """
    # Filter only fired signals
    fired = [s for s in signals if s["fired"]]
    
    # Group by ticker + direction (since a ticker can have both LONG and SHORT)
    best = {}
    for s in fired:
        key = (s["ticker"], s["direction"])
        if key not in best or s["win_rate"] > best[key]["win_rate"]:
            best[key] = s
    
    # Sort by win rate descending
    entries = sorted(best.values(), key=lambda x: x["win_rate"], reverse=True)
    
    # Respect ALLOW_SHORT settings
    filtered = []
    for e in entries:
        if e["direction"] == "SHORT":
            region_key = e.get("region", "US").upper()
            if region_key == "INDIA":
                region_key = "INDIAN"
            if not ALLOW_SHORT.get(region_key, True):
                print(f"[Scanner] SHORT not allowed for {region_key}, skipping {e['ticker']}")
                continue
        filtered.append(e)
    
    return filtered

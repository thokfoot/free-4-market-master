"""
FREE 3-Market v5.10 — GAP-DOWN INTRADAY SCANNER
================================================
Scans Indian stocks for gap-down mean reversion signals.
2 strategies:
  A: f_gap_down + f_52wk_low (near 252-period low) → SL 0.3%, TP 1.0%, 5min hold
  B: f_gap_down (single factor)                  → SL 0.5%, TP 1.0%, 5min hold

Data: 1-minute OHLCV via yf.Ticker().history() (NOT yf.download())
Holding: 5 minutes per trade
Market: Indian (NIFTY 50 + NEXT 50 + BANKNIFTY = 97 tickers)

Shift-1 rule: signal based on PREVIOUS candle's factors.
Entry at NEXT candle's open.
"""

import yfinance as yf
import pandas as pd
import time
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
import pytz
from config import (
    INDIAN_TICKERS, TICKER_MAP, GAP_DOWN_PERIOD_DAYS,
    GAP_DOWN_MIN_DATA, GAP_DOWN_MAX_HOLD_MINUTES,
    GAP_DOWN_A_SL_PCT, GAP_DOWN_A_TP_PCT,
    GAP_DOWN_B_SL_PCT, GAP_DOWN_B_TP_PCT,
)
import market_data

IST = pytz.timezone("Asia/Kolkata")


def download_1m_data(ticker: str, period_days: int = None) -> pd.DataFrame:
    """
    Download 1-minute OHLCV data using yf.Ticker().history().
    
    CRITICAL: Uses Ticker().history() NOT yf.download() because
    yf.download() shares internal state across calls and can return
    wrong prices (cross-contamination bug).
    """
    if period_days is None:
        period_days = GAP_DOWN_PERIOD_DAYS
    # yfinance first (Ticker().history avoids the cross-contamination bug),
    # then fall back to market_data (direct Yahoo chart API) when yfinance
    # is down (JSONDecodeError / cookie issue) or returns empty.    df = None
    try:
        t = yf.Ticker(ticker)
        end = datetime.now()
        start = end - timedelta(days=period_days)
        df = t.history(start=start.strftime("%Y-%m-%d"),
                       end=end.strftime("%Y-%m-%d"),
                       interval="1m", auto_adjust=True)
    except Exception as e:
        print(f"[GapDown] yfinance failed {ticker}: {e} - trying fallback")
        df = None
    if df is None or df.empty:
        df = market_data.download(ticker, interval="1m", period="7d")
        if df is None or len(df) == 0:
            return None
    
    # Flatten MultiIndex columns if present
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    
    # Drop duplicates and sort
    df = df[~df.index.duplicated(keep='first')]
    df = df.sort_index()
    
    return df



def _is_fresh(df: pd.DataFrame, max_staleness_minutes: int = 90) -> bool:
    """
    Return True if the last bar of intraday data belongs to the CURRENT
    IST trading day and is at most max_staleness_minutes old.

    Confirmed root cause 2026-08-11: yfinance 1m data for NSE was a full
    day behind at 09:53 IST, so the scanner computed the PREVIOUS day's
    gap (PFC -1.29%, RECLTD -1.74%, ABFRL -4.18% = Aug-10 gaps) and fired
    signals at Aug-10 prices (entry above the real Aug-11 day high) while
    the live market had already moved. All 7 stale entries were SL-hit /
    expired -> Rs 23,945 loss.
    """
    if df is None or len(df) == 0:
        return False
    last_ts = df.index[-1]
    if getattr(last_ts, 'tzinfo', None) is None:
        last_ist = IST.localize(last_ts)
    else:
        last_ist = last_ts.astimezone(IST)
    now_ist = datetime.now(IST)
    if last_ist.date() != now_ist.date():
        return False
    if (now_ist - last_ist).total_seconds() > max_staleness_minutes * 60:
        return False
    return True

def calculate_factors(df: pd.DataFrame) -> pd.DataFrame:
    """
    Calculate gap-down and price-level factors for Indian 1m data.

    Factors:
      - ind_gap_pct: (day_open / prev_day_close - 1) * 100
      - f_gap_down: 1 if gap_pct < -0.5 else 0
      - f_52wk_low: 1 if close <= 252-period low * 1.01 else 0
      
    NOTE: A gap is the day's open vs the PRIOR DAY's close. The previous
    implementation compared each 1m candle's open to the prior 1m candle's
    close — that is just the per-minute return, which is almost never < -0.5%,
    so the strategy never fired (0 signals in all historical runs).
    """
    df = df.copy()
    
    # ── Daily open gap vs previous day's close ──
    df['_day'] = df.index.date
    day_open = df.groupby('_day')['Open'].first()
    day_close = df.groupby('_day')['Close'].last()
    prev_day_close = day_close.shift(1)
    day_gap_pct = (day_open / prev_day_close - 1) * 100
    # Map the (constant) gap of the day back onto every bar of that day
    df['ind_gap_pct'] = df['_day'].map(day_gap_pct)
    df['f_gap_down'] = (df['ind_gap_pct'] < -0.5).astype(int)
    
    # f_52wk_low: close within 1% of 252-period low
    # Uses available data up to 252 periods
    lookback = min(len(df), 252)
    if lookback >= 20:
        df['low_252'] = df['Close'].rolling(window=lookback, min_periods=lookback).min()
        df['f_52wk_low'] = (df['Close'] <= df['low_252'] * 1.01).astype(int)
    else:
        df['f_52wk_low'] = 0
    
    df = df.drop(columns=['_day'])
    return df


def check_strategy_signals(ticker: str, factors_df: pd.DataFrame) -> list:
    """
    Check if either gap-down strategy triggers on the latest data.
    
    Shift-1 rule (prevents look-ahead bias):
      - Candle N: factors calculated (f_gap_down, f_52wk_low)
      - Candle N+1: ENTER at open if Candle N's factors satisfy conditions
    
    Returns list of signal dicts:
      [{ticker, strategy, direction, entry_price, sl, tp, gap_pct, timestamp, max_hold_minutes}]
    """
    if factors_df is None or len(factors_df) < 3:
        return []
    
    prev = factors_df.iloc[-2]   # Previous completed candle factors
    curr = factors_df.iloc[-1]   # Current candle (use its open for entry)
    
    signals = []
    entry_price = float(curr['Open'])
    
    # Strategy A: f_gap_down + f_52wk_low (near 252-period low)
    if prev['f_gap_down'] == 1 and prev['f_52wk_low'] == 1:
        signals.append({
            'ticker': ticker,
            'strategy': 'gap_down_52wk_low',
            'direction': 'LONG',
            'entry_price': entry_price,
            'sl': round(entry_price * (1 - GAP_DOWN_A_SL_PCT), 2),
            'tp': round(entry_price * (1 + GAP_DOWN_A_TP_PCT), 2),
            'gap_pct': round(float(prev['ind_gap_pct']), 2),
            'timestamp': curr.name,
            'max_hold_minutes': GAP_DOWN_MAX_HOLD_MINUTES,
        })
    
    # Strategy B: f_gap_down (single factor — any gap down >0.5%)
    elif prev['f_gap_down'] == 1:
        signals.append({
            'ticker': ticker,
            'strategy': 'gap_down_single',
            'direction': 'LONG',
            'entry_price': entry_price,
            'sl': round(entry_price * (1 - GAP_DOWN_B_SL_PCT), 2),
            'tp': round(entry_price * (1 + GAP_DOWN_B_TP_PCT), 2),
            'gap_pct': round(float(prev['ind_gap_pct']), 2),
            'timestamp': curr.name,
            'max_hold_minutes': GAP_DOWN_MAX_HOLD_MINUTES,
        })
    
    return signals


def scan_gap_down_ticker(ticker: str) -> list:
    """
    Full scan pipeline for one Indian ticker:
    1. Download 1m data
    2. Calculate factors
    3. Check signals
    4. Return entries (or empty list)
    """
    df = download_1m_data(ticker, GAP_DOWN_PERIOD_DAYS)
    if df is None or len(df) < GAP_DOWN_MIN_DATA:
        return []
    if not _is_fresh(df):
        last_bar = df.index[-1] if len(df) else '?'
        print(f"[GapDown] SKIP {ticker}: stale intraday data (last bar {last_bar}), "
              f"not today's session - no signal fired")
        return []
    
    factors = calculate_factors(df)
    signals = check_strategy_signals(ticker, factors)
    return signals


def scan_all_gap_down(progress_interval: int = 10, max_workers: int = 6) -> list:
    """
    Scan ALL Indian tickers for gap-down signals.
    Runs the full pipeline on each ticker with a thread pool — the sequential
    version took ~4 min, which meant GitHub Actions scheduled runs (which are
    routinely delayed by minutes/hours and can be dropped under load) rarely
    completed in time. Parallelizing keeps each run under ~90s.
    
    Args:
        progress_interval: Print progress every N tickers
        max_workers: Number of concurrent yfinance downloads
        
    Returns:
        List of signal dicts with entry params
    """
    all_entries = []
    tickers = INDIAN_TICKERS
    total = len(tickers)
    
    print(f"[GapDown] Scanning {total} Indian tickers for gap-down signals "
          f"({max_workers} workers)...")
    
    done = 0
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(scan_gap_down_ticker, t): t for t in tickers}
        for fut in as_completed(futures):
            ticker = futures[fut]
            done += 1
            try:
                signals = fut.result()
            except Exception as e:
                print(f"[GapDown] Scan error {ticker}: {e}")
                signals = []
            for s in signals:
                all_entries.append(s)
                print(f"[GapDown] SIGNAL: {s['ticker']} {s['strategy']} "
                      f"@{s['entry_price']} SL={s['sl']} TP={s['tp']} "
                      f"Gap={s['gap_pct']}%")
            if done % progress_interval == 0:
                print(f"[GapDown] Progress: {done}/{total} tickers, "
                      f"{len(all_entries)} signals found")
    
    print(f"[GapDown] Complete: {total}/{total} tickers, {len(all_entries)} signals")
    return all_entries


def get_current_ohlc(ticker: str) -> dict:
    """
    Get current day's intraday OHLC for an Indian ticker.
    Used by update_trades() for SL/TP checking.
    
    Returns:
        {"close": c, "high": h, "low": l} or None
    """
    try:
        df = download_1m_data(ticker, period_days=1)
        if df is not None and len(df) >= 3:
            last = df.iloc[-1]
            daily_high = float(df['High'].max())
            daily_low = float(df['Low'].min())
            return {
                "close": float(last['Close']),
                "high": daily_high,
                "low": daily_low,
                "date": str(df.index[-1].date()),
            }
    except Exception as e:
        print(f"[GapDown] OHLC error {ticker}: {e}")
    return None

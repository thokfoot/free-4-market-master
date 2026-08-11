"""Regression tests for the gap-down stale-data guard.

Confirmed bug 2026-08-11: at 09:53 IST the gap-down scanner used
PREVIOUS-day 1m data (yfinance NSE 1m was a day behind). It computed
Aug-10 gaps at Aug-10 prices and fired 7 signals into the live Aug-11
market -> Rs 23,945 loss (all SL-hit/expired), then re-entered the same
7 tickers 4 min later for another Rs 7,397 loss.

Guard: _is_fresh() must reject any data whose last bar is not from the
current IST trading day.
"""
from datetime import datetime, timedelta

import pandas as pd
import pytz

import scanner_gap_down as sgd

IST = pytz.timezone("Asia/Kolkata")


def _df_with_last_bar(dt_ist):
    """Build a minimal 1m OHLCV frame ending at the given IST timestamp."""
    idx = pd.DatetimeIndex([dt_ist - timedelta(minutes=2), dt_ist])
    df = pd.DataFrame(
        {
            "Open": [100.0, 99.5],
            "High": [101.0, 100.0],
            "Low": [99.0, 98.5],
            "Close": [99.5, 99.0],
            "Volume": [1000, 1200],
        },
        index=idx,
    )
    return df


def test_fresh_today_data_passes():
    now = datetime.now(IST)
    df = _df_with_last_bar(now - timedelta(minutes=5))
    assert sgd._is_fresh(df) is True


def test_previous_day_data_rejected():
    """The exact 2026-08-11 failure: last bar is yesterday's 15:30 IST."""
    now = datetime.now(IST)
    yesterday_1530 = now - timedelta(days=1)
    yesterday_1530 = yesterday_1530.replace(hour=15, minute=30, second=0, microsecond=0)
    df = _df_with_last_bar(yesterday_1530)
    assert sgd._is_fresh(df) is False


def test_fresh_but_too_old_rejected():
    now = datetime.now(IST)
    df = _df_with_last_bar(now - timedelta(minutes=120))
    assert sgd._is_fresh(df) is False


def test_empty_df_rejected():
    assert sgd._is_fresh(None) is False
    assert sgd._is_fresh(pd.DataFrame()) is False


def test_utc_index_normalized_to_ist():
    """Data from yfinance is tz-aware UTC; IST date must still match today."""
    now_utc = datetime.now(pytz.UTC)
    idx = pd.DatetimeIndex([now_utc - timedelta(minutes=2), now_utc])
    df = pd.DataFrame(
        {"Open": [100.0, 99.5], "High": [101.0, 100.0],
         "Low": [99.0, 98.5], "Close": [99.5, 99.0], "Volume": [1000, 1200]},
        index=idx,
    )
    # Between 00:00-05:29 IST the UTC date is yesterday; IST date is today.
    # Guard compares against IST date, so a same-UTC-day frame must pass.
    assert sgd._is_fresh(df) is True


def test_naive_index_localized_as_ist():
    now = datetime.now(IST).replace(tzinfo=None)
    df = _df_with_last_bar(now - timedelta(minutes=3))
    assert sgd._is_fresh(df) is True

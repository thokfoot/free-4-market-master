"""
Regression tests for the v5.10 audit fixes (2026-08-05).

Covers:
    1. rebuild_portfolio_from_csv() — portfolio.json becomes a pure function of
       paper_trades.csv (single source of truth) so bot.py and live_pnl_updater.py
       can never drift or double-count. GAP_DOWN_1m draws from the INTRADAY bucket.
    2. Re-entry allowed — the intraday re-entry guard (24h gap) was REMOVED so
       every fired signal is paper-entered (unlimited evaluation of strategies).
    3. scanner_gap_down.calculate_factors — a "gap" is the day's open vs the
       PRIOR DAY's close, not vs the prior 1-minute candle's close.
    4. scanner_intraday entry price — must be the close of the COMPLETED signal
       candle, not the still-forming candle.
"""

import numpy as np
import pandas as pd
import pytest
import pytz
from datetime import datetime, timezone

import paper_trader as pt
from paper_trader import enter_trade, update_trades

IST = pytz.timezone("Asia/Kolkata")


def _set_time(monkeypatch, naive_dt):
    """Freeze paper_trader.datetime to a specific IST naive datetime."""
    class FrozenDT:
        _FROZEN_NAIVE = naive_dt

        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return IST.localize(cls._FROZEN_NAIVE)
            return cls._FROZEN_NAIVE

        @classmethod
        def utcnow(cls):
            return cls._FROZEN_NAIVE.replace(tzinfo=timezone.utc)

        @classmethod
        def strptime(cls, date_string, fmt):
            return datetime.strptime(date_string, fmt)

    monkeypatch.setattr("paper_trader.datetime", FrozenDT)
    return naive_dt


# ======================================================================
# 1. rebuild_portfolio_from_csv() — single source of truth
# ======================================================================

def test_rebuild_portfolio_from_csv_single_source_of_truth(test_env, monkeypatch):
    """Portfolio counters/capital are recomputed purely from the CSV rows."""
    rows = [
        # US swing CLOSED win → US bucket
        {"Date": "2026-01-13", "Time_IST": "10:00:00 IST", "Mode": "US", "Ticker": "SPY",
         "Direction": "LONG", "TimeFrame": "SWING_1d", "Entry_Price": 100.0, "Qty": 10,
         "SL": 98.0, "Target": 104.0, "MaxHold": 5, "Exit_Price": 102.0,
         "Exit_Time": "12:00:00 IST", "P&L": 19.8, "P&L_%": 2.0, "Status": "CLOSED",
         "Pattern_Rank": 1, "Expected_WinRate": 60.0, "Pattern_Factors": "X",
         "Reason": "#1 X", "Signal_Indicators": ""},
        # INDIAN swing CLOSED loss → INDIAN bucket
        {"Date": "2026-01-13", "Time_IST": "11:00:00 IST", "Mode": "INDIAN", "Ticker": "^NSEI",
         "Direction": "LONG", "TimeFrame": "SWING_1d", "Entry_Price": 20000.0, "Qty": 1,
         "SL": 19000.0, "Target": 21000.0, "MaxHold": 5, "Exit_Price": 19000.0,
         "Exit_Time": "12:00:00 IST", "P&L": -1002.0, "P&L_%": -5.0, "Status": "CLOSED",
         "Pattern_Rank": 2, "Expected_WinRate": 60.0, "Pattern_Factors": "Y",
         "Reason": "#2 Y", "Signal_Indicators": ""},
        # INTRADAY_1h CLOSED loss (Mode=US) → INTRADAY bucket
        {"Date": "2026-01-14", "Time_IST": "10:00:00 IST", "Mode": "US", "Ticker": "QQQ",
         "Direction": "LONG", "TimeFrame": "INTRADAY_1h", "Entry_Price": 400.0, "Qty": 10,
         "SL": 396.0, "Target": 408.0, "MaxHold": 6, "Exit_Price": 396.0,
         "Exit_Time": "11:00:00 IST", "P&L": -40.0, "P&L_%": -1.0, "Status": "CLOSED",
         "Pattern_Rank": 3, "Expected_WinRate": 60.0, "Pattern_Factors": "Z",
         "Reason": "#3 Z", "Signal_Indicators": ""},
        # GAP_DOWN_1m CLOSED loss (Mode=INDIAN) → INTRADAY bucket
        {"Date": "2026-01-14", "Time_IST": "10:30:00 IST", "Mode": "INDIAN", "Ticker": "RELIANCE.NS",
         "Direction": "LONG", "TimeFrame": "GAP_DOWN_1m", "Entry_Price": 2500.0, "Qty": 1,
         "SL": 2492.5, "Target": 2525.0, "MaxHold": 5, "Exit_Price": 2492.5,
         "Exit_Time": "10:35:00 IST", "P&L": -9.0, "P&L_%": -0.3, "Status": "CLOSED",
         "Pattern_Rank": 997, "Expected_WinRate": 75.0,
         "Pattern_Factors": "f_gap_down + f_52wk_low",
         "Reason": "#997 f_gap_down + f_52wk_low", "Signal_Indicators": ""},
        # OPEN swing position → must NOT touch capital, must appear in open_positions
        {"Date": "2026-01-15", "Time_IST": "09:00:00 IST", "Mode": "CRYPTO", "Ticker": "ETH-USD",
         "Direction": "LONG", "TimeFrame": "SWING_1d", "Entry_Price": 3000.0, "Qty": 1,
         "SL": 2940.0, "Target": 3120.0, "MaxHold": 5, "Exit_Price": "",
         "Exit_Time": "", "P&L": "", "P&L_%": "", "Status": "OPEN",
         "Pattern_Rank": 4, "Expected_WinRate": 60.0, "Pattern_Factors": "W",
         "Reason": "#4 W", "Signal_Indicators": ""},
    ]
    pd.DataFrame(rows).to_csv(pt.PAPER_FILE, index=False)

    port = pt.rebuild_portfolio_from_csv()

    assert port["closed_count"] == 4
    assert port["total_wins"] == 1
    assert port["total_losses"] == 3
    assert port["total_pnl"] == pytest.approx(19.8 - 1002.0 - 40.0 - 9.0)
    # Swing capital buckets
    assert port["capital_by_market"]["US"] == pytest.approx(100000 + 19.8)
    assert port["capital_by_market"]["INDIAN"] == pytest.approx(100000 - 1002.0)
    # OPEN trade does not touch capital
    assert port["capital_by_market"]["CRYPTO"] == pytest.approx(100000.0)
    # Both intraday trades (INTRADAY_1h + GAP_DOWN_1m) draw from the INTRADAY bucket
    assert port["capital_by_market"]["INTRADAY"] == pytest.approx(100000 - 40.0 - 9.0)
    assert port["total_capital"] == pytest.approx(sum(port["capital_by_market"].values()))
    # open_positions mirrors the OPEN row
    assert len(port["open_positions"]) == 1
    assert port["open_positions"][0]["Ticker"] == "ETH-USD"


def test_rebuild_portfolio_skips_closed_row_with_missing_pnl(test_env, monkeypatch):
    """A CLOSED row with a blank P&L is skipped (with warning), not counted."""
    rows = [
        {"Date": "2026-01-13", "Time_IST": "10:00:00 IST", "Mode": "US", "Ticker": "SPY",
         "Direction": "LONG", "TimeFrame": "SWING_1d", "Entry_Price": 100.0, "Qty": 10,
         "SL": 98.0, "Target": 104.0, "MaxHold": 5, "Exit_Price": 102.0,
         "Exit_Time": "12:00:00 IST", "P&L": "", "P&L_%": "", "Status": "CLOSED",
         "Pattern_Rank": 1, "Expected_WinRate": 60.0, "Pattern_Factors": "X",
         "Reason": "#1 X", "Signal_Indicators": ""},
        # Literal "nan" (legacy corruption artifact) must also be skipped
        {"Date": "2026-01-14", "Time_IST": "10:00:00 IST", "Mode": "US", "Ticker": "QQQ",
         "Direction": "LONG", "TimeFrame": "SWING_1d", "Entry_Price": 400.0, "Qty": 10,
         "SL": 396.0, "Target": 408.0, "MaxHold": 5, "Exit_Price": 396.0,
         "Exit_Time": "11:00:00 IST", "P&L": "nan", "P&L_%": "nan", "Status": "CLOSED",
         "Pattern_Rank": 2, "Expected_WinRate": 60.0, "Pattern_Factors": "Y",
         "Reason": "#2 Y", "Signal_Indicators": ""},
    ]
    pd.DataFrame(rows).to_csv(pt.PAPER_FILE, index=False)
    port = pt.rebuild_portfolio_from_csv()
    assert port["closed_count"] == 0
    assert port["total_pnl"] == 0.0


def test_rebuild_open_positions_keep_empty_exit_fields(test_env):
    """OPEN rows must keep '' for exit fields — pd.read_csv blank cells become
    float NaN, which would otherwise be written as bare NaN (invalid strict
    JSON) into portfolio.json."""
    rows = [
        {"Date": "2026-01-15", "Time_IST": "09:00:00 IST", "Mode": "CRYPTO",
         "Ticker": "ETH-USD", "Direction": "LONG", "TimeFrame": "SWING_1d",
         "Entry_Price": 3000.0, "Qty": 1, "SL": 2940.0, "Target": 3120.0,
         "MaxHold": 5, "Exit_Price": "", "Exit_Time": "", "P&L": "",
         "P&L_%": "", "Status": "OPEN", "Pattern_Rank": 4,
         "Expected_WinRate": 60.0, "Pattern_Factors": "W",
         "Reason": "#4 W", "Signal_Indicators": ""},
    ]
    pd.DataFrame(rows).to_csv(pt.PAPER_FILE, index=False)

    port = pt.rebuild_portfolio_from_csv()

    pos = port["open_positions"][0]
    assert pos["Exit_Price"] == ""
    assert pos["Exit_Time"] == ""
    assert pos["P&L"] == ""
    assert pos["P&L_%"] == ""
    # Entry fields survive as real numbers
    assert pos["Entry_Price"] == pytest.approx(3000.0)
    assert pos["Qty"] == 1

    # And the on-disk JSON must be valid strict JSON (no bare NaN tokens)
    raw = open(pt.PORTFOLIO_FILE, encoding="utf-8").read()
    assert "NaN" not in raw
    import json as _json
    _json.loads(raw)  # strict parser must accept it


def test_rebuild_zero_pnl_is_neither_win_nor_loss(test_env):
    """A breakeven (P&L == 0) closed trade counts as closed but not as a
    win or a loss — e.g. a manual duplicate-cleanup close at entry price."""
    rows = [
        {"Date": "2026-01-15", "Time_IST": "10:00:00 IST", "Mode": "US",
         "Ticker": "XLF", "Direction": "SHORT", "TimeFrame": "INTRADAY_1h",
         "Entry_Price": 57.86, "Qty": 1513, "SL": 58.44, "Target": 56.7,
         "MaxHold": 6, "Exit_Price": 57.86, "Exit_Time": "11:00:00 IST",
         "P&L": 0.0, "P&L_%": 0.0, "Status": "CLOSED", "Pattern_Rank": 38,
         "Expected_WinRate": 60.47, "Pattern_Factors": "RSI>65+2Red",
         "Reason": "#38ID RSI>65+2Red | Duplicate cleanup", "Signal_Indicators": ""},
    ]
    pd.DataFrame(rows).to_csv(pt.PAPER_FILE, index=False)

    port = pt.rebuild_portfolio_from_csv()

    assert port["closed_count"] == 1
    assert port["total_wins"] == 0
    assert port["total_losses"] == 0
    assert port["total_pnl"] == 0.0
    assert len(port["open_positions"]) == 0


# ======================================================================
# 2. Re-entry allowed (re-entry guard removed for paper-trade evaluation)
# ======================================================================

def test_immediate_intraday_reentry_allowed(test_env, monkeypatch):
    """A persistent intraday signal may re-enter immediately (no 24h guard)."""
    _set_time(monkeypatch, datetime(2026, 1, 15, 10, 30, 0))

    t1 = enter_trade("US", "DIA", "LONG", 400.0, "Test", pattern_rank=36,
                     expected_win_rate=67.86, pattern_factors="P", tf="INTRADAY_1h")
    assert t1 is not None, "first intraday entry should succeed"

    # Close it via SL hit
    msgs = update_trades({"DIA": {"close": 396.0, "high": 401.0, "low": 394.0,
                                  "date": "2026-01-15"}})
    assert len(msgs) == 1, f"expected SL exit, got {msgs}"

    # Immediate re-entry of the SAME ticker+rank while the signal still fires
    t2 = enter_trade("US", "DIA", "LONG", 400.0, "Test", pattern_rank=36,
                     expected_win_rate=67.86, pattern_factors="P", tf="INTRADAY_1h")
    assert t2 is not None, "re-entry must be allowed (re-entry guard removed)"

    # Opposite-direction swing on the same ticker is still fine
    t3 = enter_trade("US", "DIA", "SHORT", 400.0, "Test", pattern_rank=3,
                     expected_win_rate=60.0, pattern_factors="Q", tf="SWING_1d")
    assert t3 is not None, "swing entry must not be blocked"


# ======================================================================
# 3. scanner_gap_down — daily gap vs previous day's close
# ======================================================================

def test_gap_down_factor_uses_daily_gap_not_minute_gap():
    import scanner_gap_down as sg

    idx = pd.to_datetime([
        "2026-01-13 09:16:00+05:30", "2026-01-13 09:17:00+05:30",
        "2026-01-14 09:16:00+05:30", "2026-01-14 09:17:00+05:30",
    ])
    df = pd.DataFrame({
        "Open": [100.0, 100.1, 99.2, 99.3],
        "High": [100.5, 100.4, 99.6, 99.5],
        "Low": [99.8, 99.9, 98.9, 99.0],
        "Close": [100.2, 100.1, 99.1, 99.2],
        "Volume": [1000, 1000, 1000, 1000],
    }, index=idx)

    f = sg.calculate_factors(df)

    # Day 1 has no previous day → no gap, f_gap_down=0 on both bars
    assert f.iloc[0]["f_gap_down"] == 0
    assert f.iloc[1]["f_gap_down"] == 0

    # Day 2 opens 99.2 vs day-1 close 100.1 → gap -0.90% < -0.5%
    # Per-minute opens (99.2→99.3) are only +0.1% — the OLD buggy calc would
    # never flag this as a gap-down. The daily-gap calc must fire on EVERY
    # day-2 bar (the gap is a property of the day, constant until next open).
    assert f.iloc[2]["ind_gap_pct"] == pytest.approx((99.2 / 100.1 - 1) * 100)
    assert f.iloc[2]["f_gap_down"] == 1
    assert f.iloc[3]["f_gap_down"] == 1


# ======================================================================
# 4. scanner_intraday — entry price = completed signal candle close
# ======================================================================

def test_signal_candle_index_completed_vs_forming():
    import scanner_intraday as si

    now = pd.Timestamp.now(tz="UTC")
    # Latest candle still forming (ends in the future) → use -2 (completed)
    idx_form = pd.date_range(end=now + pd.Timedelta(minutes=30), periods=4, freq="h")
    df_form = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx_form)
    assert si._signal_candle_index(df_form) == -2

    # Latest candle fully complete (ended >= 1h ago) → use -1
    idx_done = pd.date_range(end=now - pd.Timedelta(hours=2), periods=4, freq="h")
    df_done = pd.DataFrame({"Close": [1.0, 2.0, 3.0, 4.0]}, index=idx_done)
    assert si._signal_candle_index(df_done) == -1


def test_intraday_entry_price_uses_completed_candle(test_env, monkeypatch):
    import scanner_intraday as si

    # Build 1h data whose latest candle is still forming
    now = pd.Timestamp.now(tz="UTC")
    idx = pd.date_range(end=now + pd.Timedelta(minutes=30), periods=210, freq="h")
    rng = np.random.default_rng(42)
    closes = 100.0 + np.cumsum(rng.normal(0, 0.5, 210))
    df = pd.DataFrame({
        "Open": closes, "High": closes + 1.0, "Low": closes - 1.0,
        "Close": closes, "Volume": [1_000_000] * 210,
    }, index=idx)
    df = si.compute_indicators_1h(df)

    strategies = pd.DataFrame([{
        "Final_Rank": 5, "Market": "QQQ", "Region": "US",
        "Factors": "Close>0", "Direction": "LONG", "AvgWin%": 60.0, "Trades": 100,
    }])
    signals = si.scan_intraday_strategies(strategies, {"QQQ": df})
    fired = [s for s in signals if s["fired"]]
    assert len(fired) == 1

    # Entry close must match the COMPLETED candle (index -2), not the forming one
    expected = float(df.iloc[-2]["Close"])
    assert fired[0]["close"] == pytest.approx(expected)

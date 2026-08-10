"""
Regression tests for the same-day PRE-ENTRY low false-exit bug.

Real case (trade #46, 2026-08-06): ^NDX LONG entered 20:23:19 IST
(14:53 UTC) at 29470.96, SL 29170.42, Target 30054.37, INTRADAY_1h.
The only 1h bar whose LOW (29128.05) touched SL was the 13:30 UTC bar —
it ENDED at 14:30 UTC, BEFORE the entry. No post-entry bar ever touched SL,
yet the bot exited 53 minutes later with "SL Hit (intraday)" at 29164.59.

Root cause: after the bar-level post-entry check (which correctly found
nothing), the AGGREGATE fallback still compared the FULL-SESSION daily_low
(pre-entry bars included) against SL → false exit.

Fix: when bars are available, the aggregate fallback is restricted to
POST-ENTRY bars only (paper_trader._post_entry_ohlc) — a same-day pre-entry
low can never stop out a position. Mirrors live_pnl_updater's has_post_entry.

These tests:
  1. NO false exit when only a PRE-ENTRY bar touches SL (bug reproduction)
  2. STILL exits when a POST-ENTRY bar touches SL (guard must not block real SL)
  3. Non-US (crypto/India) with bars → aggregate unchanged (no regression)
"""

import json
import pandas as pd
import pytz
from datetime import datetime, timezone

import paper_trader
from paper_trader import enter_trade, update_trades, load_portfolio

IST = pytz.timezone("Asia/Kolkata")


# ======================================================================
# Time helper (same pattern as test_regression_fixes._set_time)
# ======================================================================

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
# Bug reproduction: pre-entry low must NOT trigger SL exit
# ======================================================================

def _enter_ndx_146(test_env, monkeypatch):
    """Enter the exact #46 ^NDX intraday trade (entered 14:53 UTC Aug 6)."""
    entry_dt_ist = datetime(2026, 8, 6, 20, 23, 19)
    _set_time(monkeypatch, entry_dt_ist)
    t = enter_trade(
        "US", "^NDX", "LONG", 29470.96,
        "#25ID Close>Open+2Red",
        pattern_rank=25, expected_win_rate=55.0,
        pattern_factors="Close>Open+2Red", tf="INTRADAY_1h",
        sl_override=29170.42, tp_override=30054.37,
        max_hold_override=6,
    )
    assert t is not None, "Failed to enter test trade"
    return t


def _ndx_bars(pre_entry_low=29128.05, post_entry_low=29300.0):
    """1h bars for Aug 6: pre-entry 13:30 bar low < SL; post-entry above SL."""
    return [
        (pd.Timestamp("2026-08-06 13:30:00", tz="UTC"), 29530.0, pre_entry_low, 29470.96),  # pre-entry
        (pd.Timestamp("2026-08-06 14:30:00", tz="UTC"), 29500.0, 29380.0, 29480.0),        # forming at entry
        (pd.Timestamp("2026-08-06 15:30:00", tz="UTC"), 29510.0, post_entry_low, 29490.0),  # post-entry
    ]


def test_no_false_sl_on_same_day_pre_entry_low(test_env, monkeypatch):
    """Bug reproduction: full-session low (pre-entry) must NOT exit the trade."""
    _enter_ndx_146(test_env, monkeypatch)

    # Next scan: 21:16 IST (15:46 UTC) — 53 min after entry
    _set_time(monkeypatch, datetime(2026, 8, 6, 21, 16, 19))

    # Aggregates as bot.py supplies them: full-session low 29128.05 < SL 29170.42
    ohlc = {
        "close": 29490.0,
        "high": 29530.0,
        "low": 29128.05,          # pre-entry bar's low — was the false trigger
        "date": "2026-08-06",
        "bars": _ndx_bars(),      # post-entry lows all above SL
    }

    msgs = update_trades({"^NDX": ohlc})
    assert len(msgs) == 0, f"No exit expected (pre-entry low), got: {msgs}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    assert len(df[df["Status"].astype(str) == "OPEN"]) == 1, \
        "Position must remain OPEN after a pre-entry low below SL"
    port = load_portfolio()
    assert len(port.get("open_positions", [])) == 1


def test_still_exits_on_post_entry_sl(test_env, monkeypatch):
    """Guard must NOT block real SL: a POST-ENTRY low touching SL still exits."""
    _enter_ndx_146(test_env, monkeypatch)
    _set_time(monkeypatch, datetime(2026, 8, 6, 21, 16, 19))

    # Post-entry 15:30 bar low 29150 < SL 29170.42 → genuine SL hit
    ohlc = {
        "close": 29120.0,
        "high": 29510.0,
        "low": 29128.05,          # pre-entry low (irrelevant — post-entry hit exists)
        "date": "2026-08-06",
        "bars": _ndx_bars(post_entry_low=29150.0),
    }

    msgs = update_trades({"^NDX": ohlc})
    assert len(msgs) == 1, f"Expected 1 SL exit, got: {msgs}"
    assert "SL Hit" in msgs[0], f"Expected SL Hit, got: {msgs[0]}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"].astype(str) == "CLOSED"]
    assert len(closed) == 1
    port = load_portfolio()
    assert len(port.get("open_positions", [])) == 0


def test_still_exits_on_post_entry_target(test_env, monkeypatch):
    """Guard must NOT block real TP: a POST-ENTRY high touching TP still exits."""
    t = _enter_ndx_146(test_env, monkeypatch)
    _set_time(monkeypatch, datetime(2026, 8, 6, 21, 16, 19))

    # Post-entry 15:30 bar high 30100 > Target 30054.37 → genuine TP hit
    bars = [
        (pd.Timestamp("2026-08-06 13:30:00", tz="UTC"), 29530.0, 29128.05, 29470.96),
        (pd.Timestamp("2026-08-06 14:30:00", tz="UTC"), 29500.0, 29380.0, 29480.0),
        (pd.Timestamp("2026-08-06 15:30:00", tz="UTC"), 30100.0, 29400.0, 30060.0),
    ]
    ohlc = {
        "close": 30060.0,
        "high": 30100.0,
        "low": 29128.05,
        "date": "2026-08-06",
        "bars": bars,
    }

    msgs = update_trades({"^NDX": ohlc})
    assert len(msgs) == 1, f"Expected 1 TP exit, got: {msgs}"
    assert "Target" in msgs[0], f"Expected Target Hit, got: {msgs[0]}"


def test_non_us_bars_unchanged(test_env, monkeypatch):
    """Non-US mode with bars: aggregate fallback unchanged (no false holds)."""
    # Crypto trade — aggregate low must still trigger SL (no bar-level restriction)
    _set_time(monkeypatch, datetime(2026, 8, 6, 12, 0, 0))
    t = enter_trade(
        "CRYPTO", "TRX-USD", "LONG", 0.328,
        "Test crypto", pattern_rank=1, expected_win_rate=66.0,
        pattern_factors="Close>Open", tf="INTRADAY_1h",
        sl_override=0.3215, tp_override=0.3412,
        max_hold_override=6,
    )
    assert t is not None
    _set_time(monkeypatch, datetime(2026, 8, 6, 13, 0, 0))

    ohlc = {
        "close": 0.3250,
        "high": 0.3300,
        "low": 0.3200,             # < SL 0.3215 → must still exit
        "date": "2026-08-06",
        "bars": [
            (pd.Timestamp("2026-08-06 10:00:00", tz="UTC"), 0.3300, 0.3200, 0.3250),
        ],
    }
    msgs = update_trades({"TRX-USD": ohlc})
    assert len(msgs) == 1, f"Non-US aggregate SL must still fire, got: {msgs}"
    assert "SL Hit" in msgs[0]

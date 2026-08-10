"""
Regression tests: the same-day PRE-ENTRY low false-exit guard now covers
ALL markets (not just US).

Trade #46 (2026-08-06, ^NDX US intraday) proved the bug class: a position
entered mid-session can be falsely stopped out by the SIGNAL candle's own
low (which prints before the entry time). The fix (paper_trader
_bars_sl_tp + _post_entry_ohlc) restricts SL/TP to POST-ENTRY bars only.

These tests prove the SAME protection applies to:
  - CRYPTO intraday (24/7 market, entry + max_hold hours live window)
  - INDIAN intraday (same non-US live window as the replay engine)
while real post-entry SL/TP hits still exit.
"""

import pandas as pd
import pytz
from datetime import datetime, timezone

import paper_trader
from paper_trader import enter_trade, update_trades, load_portfolio

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
# CRYPTO intraday (Mode=CRYPTO) — pre-entry low must NOT false-exit
# ======================================================================

def _enter_crypto(test_env, monkeypatch):
    """BTC-USD LONG entered 12:00 IST (06:30 UTC) Aug 6."""
    _set_time(monkeypatch, datetime(2026, 8, 6, 12, 0, 0))
    t = enter_trade(
        "CRYPTO", "BTC-USD", "LONG", 60000.0,
        "Test crypto", pattern_rank=1, expected_win_rate=66.0,
        pattern_factors="Close>Open", tf="INTRADAY_1h",
        sl_override=58800.0, tp_override=62400.0,
        max_hold_override=6,
    )
    assert t is not None, "Failed to enter crypto test trade"
    return t


def _crypto_bars():
    """1h bars: pre-entry 05:00 low 58500 < SL; post-entry above SL."""
    return [
        (pd.Timestamp("2026-08-06 05:00:00", tz="UTC"), 60200.0, 58500.0, 60050.0),  # pre-entry
        (pd.Timestamp("2026-08-06 06:30:00", tz="UTC"), 60100.0, 59900.0, 60000.0),  # forming at entry
        (pd.Timestamp("2026-08-06 07:00:00", tz="UTC"), 60150.0, 59950.0, 60100.0),  # post-entry
    ]


def test_crypto_no_false_sl_on_pre_entry_low(test_env, monkeypatch):
    """Crypto: full-session low (pre-entry) must NOT stop out the position."""
    _enter_crypto(test_env, monkeypatch)

    # Next scan: 13:00 IST (07:30 UTC) — 1h after entry
    _set_time(monkeypatch, datetime(2026, 8, 6, 13, 0, 0))

    ohlc = {
        "close": 60100.0,
        "high": 60200.0,
        "low": 58500.0,             # pre-entry bar's low — was the false trigger
        "date": "2026-08-06",
        "bars": _crypto_bars(),     # post-entry lows all above SL 58800
    }

    msgs = update_trades({"BTC-USD": ohlc})
    assert len(msgs) == 0, f"No exit expected (crypto pre-entry low), got: {msgs}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    assert len(df[df["Status"].astype(str) == "OPEN"]) == 1, \
        "Crypto position must remain OPEN after a pre-entry low below SL"
    port = load_portfolio()
    assert len(port.get("open_positions", [])) == 1


def test_crypto_still_exits_on_post_entry_sl(test_env, monkeypatch):
    """Crypto: a POST-ENTRY low touching SL still exits normally."""
    _enter_crypto(test_env, monkeypatch)
    _set_time(monkeypatch, datetime(2026, 8, 6, 13, 0, 0))

    bars = [
        (pd.Timestamp("2026-08-06 05:00:00", tz="UTC"), 60200.0, 58500.0, 60050.0),  # pre-entry
        (pd.Timestamp("2026-08-06 06:30:00", tz="UTC"), 60100.0, 59900.0, 60000.0),  # forming at entry
        (pd.Timestamp("2026-08-06 07:00:00", tz="UTC"), 60150.0, 58750.0, 58800.0),  # post-entry < SL
    ]
    ohlc = {
        "close": 58800.0,
        "high": 60200.0,
        "low": 58500.0,             # pre-entry low (irrelevant — post-entry hit exists)
        "date": "2026-08-06",
        "bars": bars,
    }

    msgs = update_trades({"BTC-USD": ohlc})
    assert len(msgs) == 1, f"Expected 1 SL exit, got: {msgs}"
    assert "SL Hit" in msgs[0], f"Expected SL Hit, got: {msgs[0]}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"].astype(str) == "CLOSED"]
    assert len(closed) == 1


# ======================================================================
# INDIAN intraday (Mode=INDIAN) — same protection as crypto
# ======================================================================

def test_india_no_false_sl_on_pre_entry_low(test_env, monkeypatch):
    """India: same-day pre-entry low must NOT stop out an intraday position."""
    # Entered 10:30 IST (05:00 UTC) Aug 6
    _set_time(monkeypatch, datetime(2026, 8, 6, 10, 30, 0))
    t = enter_trade(
        "INDIAN", "RELIANCE.NS", "LONG", 3000.0,
        "Test india", pattern_rank=1, expected_win_rate=66.0,
        pattern_factors="Close>Open", tf="INTRADAY_1h",
        sl_override=2940.0, tp_override=3120.0,
        max_hold_override=6,
    )
    assert t is not None, "Failed to enter india test trade"

    # Next scan: 11:30 IST (06:00 UTC)
    _set_time(monkeypatch, datetime(2026, 8, 6, 11, 30, 0))

    bars = [
        (pd.Timestamp("2026-08-06 04:00:00", tz="UTC"), 3010.0, 2920.0, 3005.0),  # pre-entry low < SL
        (pd.Timestamp("2026-08-06 05:30:00", tz="UTC"), 3005.0, 2990.0, 3000.0),  # post-entry above SL
    ]
    ohlc = {
        "close": 3000.0,
        "high": 3010.0,
        "low": 2920.0,              # pre-entry low below SL 2940
        "date": "2026-08-06",
        "bars": bars,
    }

    msgs = update_trades({"RELIANCE.NS": ohlc})
    assert len(msgs) == 0, f"No exit expected (india pre-entry low), got: {msgs}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    assert len(df[df["Status"].astype(str) == "OPEN"]) == 1


def test_india_still_exits_on_post_entry_sl(test_env, monkeypatch):
    """India: a POST-ENTRY low touching SL still exits normally."""
    _set_time(monkeypatch, datetime(2026, 8, 6, 10, 30, 0))
    t = enter_trade(
        "INDIAN", "RELIANCE.NS", "LONG", 3000.0,
        "Test india", pattern_rank=1, expected_win_rate=66.0,
        pattern_factors="Close>Open", tf="INTRADAY_1h",
        sl_override=2940.0, tp_override=3120.0,
        max_hold_override=6,
    )
    assert t is not None

    _set_time(monkeypatch, datetime(2026, 8, 6, 11, 30, 0))

    bars = [
        (pd.Timestamp("2026-08-06 04:00:00", tz="UTC"), 3010.0, 2920.0, 3005.0),  # pre-entry low < SL
        (pd.Timestamp("2026-08-06 05:30:00", tz="UTC"), 3005.0, 2935.0, 2940.0),  # post-entry < SL
    ]
    ohlc = {
        "close": 2940.0,
        "high": 3010.0,
        "low": 2920.0,
        "date": "2026-08-06",
        "bars": bars,
    }

    msgs = update_trades({"RELIANCE.NS": ohlc})
    assert len(msgs) == 1, f"Expected 1 SL exit, got: {msgs}"
    assert "SL Hit" in msgs[0], f"Expected SL Hit, got: {msgs[0]}"


# ======================================================================
# CRYPTO SWING_1d — session-date parity (replay engine uses NY session)
# ======================================================================

def test_crypto_swing_no_false_sl_on_entry_session_bar(test_env, monkeypatch):
    """Crypto swing: the ENTRY-session daily bar's low must not stop out."""
    # Entered 00:30 IST Aug 6 (= Aug 5 19:00 UTC = Aug 5 15:00 ET session)
    _set_time(monkeypatch, datetime(2026, 8, 6, 0, 30, 0))
    t = enter_trade(
        "CRYPTO", "ETH-USD", "LONG", 3000.0,
        "Test eth swing", pattern_rank=1, expected_win_rate=66.0,
        pattern_factors="EMA9>EMA20", tf="SWING_1d",
        sl_override=2880.0, tp_override=3240.0,
        max_hold_override=5,
    )
    assert t is not None, "Failed to enter crypto swing test trade"

    # Next scan: next day 00:30 IST Aug 7
    _set_time(monkeypatch, datetime(2026, 8, 7, 0, 30, 0))

    # Aug 5 daily bar low 2850 < SL (entry session — must NOT exit)
    # Aug 6 daily bar low 2950 > SL (post-entry — above SL, also no exit)
    bars = [
        (pd.Timestamp("2026-08-05 00:00:00", tz="UTC"), 3100.0, 2850.0, 3000.0),
        (pd.Timestamp("2026-08-06 00:00:00", tz="UTC"), 3050.0, 2950.0, 3020.0),
    ]
    ohlc = {
        "close": 3020.0,
        "high": 3100.0,
        "low": 2850.0,               # entry-session bar's low — pre-entry
        "date": "2026-08-07",
        "bars": bars,
    }

    msgs = update_trades({"ETH-USD": ohlc})
    assert len(msgs) == 0, f"No exit expected (crypto swing entry-session low), got: {msgs}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    assert len(df[df["Status"].astype(str) == "OPEN"]) == 1


def test_crypto_swing_still_exits_on_post_entry_sl(test_env, monkeypatch):
    """Crypto swing: a POST-ENTRY daily bar low touching SL still exits."""
    _set_time(monkeypatch, datetime(2026, 8, 6, 0, 30, 0))
    t = enter_trade(
        "CRYPTO", "ETH-USD", "LONG", 3000.0,
        "Test eth swing", pattern_rank=1, expected_win_rate=66.0,
        pattern_factors="EMA9>EMA20", tf="SWING_1d",
        sl_override=2880.0, tp_override=3240.0,
        max_hold_override=5,
    )
    assert t is not None

    _set_time(monkeypatch, datetime(2026, 8, 7, 0, 30, 0))

    # Aug 6 daily bar low 2870 < SL 2880 (post-entry session) -> genuine SL
    bars = [
        (pd.Timestamp("2026-08-05 00:00:00", tz="UTC"), 3100.0, 2850.0, 3000.0),
        (pd.Timestamp("2026-08-06 00:00:00", tz="UTC"), 3050.0, 2870.0, 2880.0),
    ]
    ohlc = {
        "close": 2880.0,
        "high": 3100.0,
        "low": 2850.0,
        "date": "2026-08-07",
        "bars": bars,
    }

    msgs = update_trades({"ETH-USD": ohlc})
    assert len(msgs) == 1, f"Expected 1 SL exit, got: {msgs}"
    assert "SL Hit" in msgs[0], f"Expected SL Hit, got: {msgs[0]}"

"""
Tests for replay_engine.py — data integrity backtester + catch-up replay.
"""
import json
import os

import pandas as pd
import pytest

import paper_trader
import replay_engine
from replay_engine import (
    _already_entered, _close_trade, _find_exit, _missed_scan_times,
    _parse_entry_utc, _verify_entry_prices, replay_exits,
)


def _row(**overrides):
    base = {
        "Date": "2026-08-06",
        "Time_IST": "22:00:00 IST",
        "Mode": "US",
        "Ticker": "SPY",
        "Direction": "LONG",
        "TimeFrame": "INTRADAY_1h",
        "Entry_Price": 100.0,
        "Qty": 10,
        "SL": 98.0,
        "Target": 102.0,
        "MaxHold": 6,
        "Exit_Price": "",
        "Exit_Time": "",
        "P&L": "",
        "P&L_%": "",
        "Status": "OPEN",
        "Pattern_Rank": 1,
        "Expected_WinRate": 55.0,
        "Pattern_Factors": "Price>SMA20",
        "Reason": "#1ID test",
        "Signal_Indicators": "",
    }
    base.update(overrides)
    return base


# =====================================================================
# _parse_entry_utc
# =====================================================================

def test_parse_entry_utc_ist_to_utc():
    r = _row(Date="2026-08-06", Time_IST="22:00:00 IST")
    assert str(_parse_entry_utc(r)) == "2026-08-06 16:30:00+00:00"


def test_parse_entry_utc_falls_back_to_date():
    r = _row(Date="2026-08-06", Time_IST="")
    # midnight IST = 18:30 UTC on the PREVIOUS day
    assert str(_parse_entry_utc(r)) == "2026-08-05 18:30:00+00:00"


# =====================================================================
# _find_exit — bar-level first-touch + MaxHold
# =====================================================================

def test_find_exit_long_sl_hit():
    r = _row(Direction="LONG", SL=98.0, Target=102.0)
    entry_utc = _parse_entry_utc(r)
    bars = [(entry_utc + pd.Timedelta(hours=1), 101.0, 97.5, 100.0),
            (entry_utc + pd.Timedelta(hours=2), 103.0, 100.0, 102.0)]
    hit = _find_exit(r, bars, entry_utc + pd.Timedelta(hours=3))
    assert hit is not None
    ep, reason, ts = hit
    assert ep == 98.0
    assert reason == "SL Hit (intraday)"
    assert ts == entry_utc + pd.Timedelta(hours=1)


def test_find_exit_short_tp_hit():
    r = _row(Direction="SHORT", SL=102.0, Target=98.0)
    entry_utc = _parse_entry_utc(r)
    bars = [(entry_utc + pd.Timedelta(hours=1), 101.0, 97.5, 99.0)]
    hit = _find_exit(r, bars, entry_utc + pd.Timedelta(hours=2))
    assert hit is not None
    ep, reason, ts = hit
    assert ep == 98.0
    assert reason == "Target Hit"


def test_find_exit_no_trigger():
    r = _row(Direction="LONG", SL=98.0, Target=102.0)
    entry_utc = _parse_entry_utc(r)
    bars = [(entry_utc + pd.Timedelta(hours=1), 101.0, 99.0, 100.5),
            (entry_utc + pd.Timedelta(hours=2), 101.5, 99.5, 101.0)]
    assert _find_exit(r, bars, entry_utc + pd.Timedelta(hours=3)) is None


def test_find_exit_ignores_pre_entry_bar():
    r = _row(Direction="LONG", SL=98.0, Target=102.0)
    entry_utc = _parse_entry_utc(r)
    # a pre-entry bar would stop us out if not filtered — must be ignored
    bars = [(entry_utc - pd.Timedelta(hours=1), 100.0, 97.0, 99.0),
            (entry_utc + pd.Timedelta(hours=1), 101.0, 99.5, 100.5)]
    assert _find_exit(r, bars, entry_utc + pd.Timedelta(hours=2)) is None


def test_find_exit_ignores_corrupt_zero_low_bar():
    """A corrupt bar (high/low = 0, as yfinance sometimes returns) must NOT
    trigger a stop-out. Mirrors update_trades' OHLC validity guard."""
    r = _row(Direction="LONG", SL=98.0, Target=102.0)
    entry_utc = _parse_entry_utc(r)
    corrupt = [(entry_utc + pd.Timedelta(hours=1), 0.0, 0.0, 101.0),
               (entry_utc + pd.Timedelta(hours=2), 101.0, 99.5, 100.5)]
    assert _find_exit(r, corrupt, entry_utc + pd.Timedelta(hours=3)) is None


def test_find_exit_crypto_wallclock_expiry():
    r = _row(Mode="CRYPTO", Direction="LONG", SL=98.0, Target=102.0, MaxHold=12)
    entry_utc = _parse_entry_utc(r)
    bars = [(entry_utc + pd.Timedelta(hours=1), 101.0, 99.0, 100.5),
            (entry_utc + pd.Timedelta(hours=11), 101.0, 99.0, 100.0)]
    hit = _find_exit(r, bars, entry_utc + pd.Timedelta(hours=13))
    assert hit is not None
    ep, reason, ts = hit
    assert reason == "Expiry"
    assert ep == 100.0
    assert ts == entry_utc + pd.Timedelta(hours=11)


def test_find_exit_swing_expiry_5d():
    r = _row(TimeFrame="SWING_1d", Date="2026-08-01", Time_IST="10:00:00 IST",
             sl=90.0, target=110.0, MaxHold=5)
    entry_utc = _parse_entry_utc(r)
    bars = []
    for day in range(2, 8):
        ts = pd.Timestamp(f"2026-08-{day:02d}").tz_localize("UTC")
        bars.append((ts, 100.0, 99.0, 100.0))
    hit = _find_exit(r, bars, pd.Timestamp("2026-08-08").tz_localize("UTC"))
    assert hit is not None
    ep, reason, ts = hit
    assert reason == "Expiry 5d"
    assert ep == 100.0


# =====================================================================
# _close_trade — mirrors update_trades (charges, IST timestamp)
# =====================================================================

def test_close_trade_pnl_and_timestamp(isolated_fs):
    df = pd.DataFrame([_row()])
    replay_engine._close_trade(
        df, 0, 102.0, "Target Hit", pd.Timestamp("2026-08-06T17:30:00Z"))
    row = df.iloc[0]
    assert row["Status"] == "CLOSED"
    assert "Target Hit" in row["Reason"]
    assert row["Exit_Time"] == "2026-08-06 23:00:00 IST"
    # LONG 100 -> 102 with 0.02% exit slippage (fill 101.98), qty 10 =>
    # gross 19.80; US charges 0.02% RT on notional 1000 => 0.20 => net 19.60
    assert row["P&L"] == pytest.approx(19.60, abs=0.01)


def test_close_trade_short_pnl(isolated_fs):
    df = pd.DataFrame([_row(Direction="SHORT", Entry_Price=100.0, Qty=10,
                            SL=102.0, Target=98.0)])
    replay_engine._close_trade(
        df, 0, 98.0, "Target Hit", pd.Timestamp("2026-08-06T17:30:00Z"))
    # SHORT exit 98 with 0.02% slippage (fill 98.02) => gross 19.80 - 0.20
    assert df.iloc[0]["P&L"] == pytest.approx(19.60, abs=0.01)


# =====================================================================
# _missed_scan_times
# =====================================================================

def test_missed_scan_times_outage_window():
    start = pd.Timestamp("2026-08-06T15:40:31Z")
    end = pd.Timestamp("2026-08-07T00:09:23Z")
    times = _missed_scan_times(start, end)
    assert [str(t)[11:16] for t in times] == ["16:30", "18:00", "19:30", "20:30"]


def test_missed_scan_times_morning_daily():
    start = pd.Timestamp("2026-08-07T00:00:00Z")
    end = pd.Timestamp("2026-08-07T05:00:00Z")
    times = _missed_scan_times(start, end)
    assert len(times) == 1
    assert str(times[0])[11:16] == "01:00"


# =====================================================================
# _already_entered — dedupe
# =====================================================================

def test_already_entered_duplicate_in_window(isolated_fs):
    df = pd.DataFrame([_row()])  # SPY LONG, entered 16:30 UTC (22:00 IST)
    window_start = pd.Timestamp("2026-08-06T15:40:31Z")
    scan = pd.Timestamp("2026-08-06T18:00:00Z")
    assert _already_entered("SPY", "LONG", df, scan, window_start, set())


def test_already_entered_fresh_ticker_allowed(isolated_fs):
    df = pd.DataFrame([_row()])
    window_start = pd.Timestamp("2026-08-06T15:40:31Z")
    scan = pd.Timestamp("2026-08-06T18:00:00Z")
    assert not _already_entered("QQQ", "LONG", df, scan, window_start, set())


# =====================================================================
# _verify_entry_prices — impossible-fill detection
# =====================================================================

def test_verify_entry_prices_fixes_impossible_long(isolated_fs, monkeypatch):
    df = pd.DataFrame([_row(Entry_Price=50.0)])
    entry_utc = _parse_entry_utc(df.iloc[0])
    bars = [(entry_utc + pd.Timedelta(hours=1), 101.0, 99.5, 100.0)]
    monkeypatch.setattr(replay_engine, "_fetch_bars", lambda *a, **k: bars)
    report = {"entry_anomalies": []}
    _verify_entry_prices(df, pd.Timestamp("2026-08-06T20:00:00Z"), report)
    assert len(report["entry_anomalies"]) == 1
    assert df.iloc[0]["Entry_Price"] == 99.5  # fixed to entry-bar low


def test_verify_entry_prices_keeps_valid_entry(isolated_fs, monkeypatch):
    df = pd.DataFrame([_row(Entry_Price=100.0)])
    entry_utc = _parse_entry_utc(df.iloc[0])
    bars = [(entry_utc + pd.Timedelta(hours=1), 101.0, 99.5, 100.0)]
    monkeypatch.setattr(replay_engine, "_fetch_bars", lambda *a, **k: bars)
    report = {"entry_anomalies": []}
    _verify_entry_prices(df, pd.Timestamp("2026-08-06T20:00:00Z"), report)
    assert len(report["entry_anomalies"]) == 0
    assert df.iloc[0]["Entry_Price"] == 100.0


# =====================================================================
# replay_exits — end to end with mocked bars
# =====================================================================

def test_replay_exits_closes_sl_hit(isolated_fs, monkeypatch):
    df = pd.DataFrame([_row(Direction="LONG", SL=98.0, Target=102.0)])
    df.to_csv(paper_trader.PAPER_FILE, index=False)
    entry_utc = _parse_entry_utc(df.iloc[0])
    bars = [(entry_utc + pd.Timedelta(hours=1), 101.0, 97.0, 99.0)]
    monkeypatch.setattr(replay_engine, "_fetch_bars", lambda *a, **k: bars)
    closed = replay_exits(entry_utc + pd.Timedelta(hours=2), fetch=True)
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "SL Hit (intraday)"
    # SL 98 with 0.02% slippage (fill 97.98): (97.98-100)*10 - 0.20 charges
    assert closed[0]["pnl"] == pytest.approx(-20.40, abs=0.01)
    # CSV + portfolio rebuilt
    df2 = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    assert df2.iloc[0]["Status"] == "CLOSED"
    port = paper_trader.load_portfolio()
    assert port["closed_count"] == 1


def test_replay_exits_no_exit_keeps_open(isolated_fs, monkeypatch):
    df = pd.DataFrame([_row(Direction="LONG", SL=98.0, Target=102.0)])
    df.to_csv(paper_trader.PAPER_FILE, index=False)
    entry_utc = _parse_entry_utc(df.iloc[0])
    bars = [(entry_utc + pd.Timedelta(hours=1), 101.0, 99.5, 100.5)]
    monkeypatch.setattr(replay_engine, "_fetch_bars", lambda *a, **k: bars)
    closed = replay_exits(entry_utc + pd.Timedelta(hours=2), fetch=True)
    assert closed == []
    df2 = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    assert df2.iloc[0]["Status"] == "OPEN"


def test_catchup_dry_run_no_fetch(isolated_fs, monkeypatch):
    """Catch-up plumbing works without network (no entries/exits possible)."""
    import strategy_report
    monkeypatch.setattr(strategy_report, "generate_strategy_report",
                        lambda *a, **k: None)
    report = replay_engine.run_catchup("2026-08-06T15:40:31Z",
                                       "2026-08-07T00:09:23Z", fetch=False)
    assert report["mode"] == "catchup"
    assert len(report["scans_replayed"]) == 4
    # no data fetched => nothing entered/closed
    assert all(len(s.get("entered", [])) == 0 for s in report["scans_replayed"])
    assert report["exits_closed"] == []

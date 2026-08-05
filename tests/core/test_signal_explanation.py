"""
Tests for signal explanation logging (2026-08-05).

Every strategy signal now carries:
    - reason: "All factors met" when fired, else the FIRST failing factor
      with the actual indicator values ("Price<EMA50: 200 !< 173.3")
    - signal_indicators: a snapshot of indicator values at scan time for
      fired AND non-fired signals, so "why did this strategy not fire" is
      permanently auditable against exact scan-time values.
"""

import numpy as np
import pandas as pd

import scanner as sc
import scanner_intraday as si


def _make_swing_df():
    """90 days of monotonically rising closes (Close > SMA50 > EMA50)."""
    idx = pd.date_range(end="2026-01-15", periods=90, freq="D")
    closes = np.linspace(100.0, 200.0, 90)
    df = pd.DataFrame({
        "Open": closes, "High": closes + 1.0, "Low": closes - 1.0,
        "Close": closes, "Volume": [1_000_000] * 90,
    }, index=idx)
    return sc.compute_indicators(df)


def test_explain_signal_fired():
    df = _make_swing_df()
    fired, reason = sc.explain_signal(df, "Price>SMA50", "LONG")
    assert fired is True
    assert reason == "All factors met"


def test_explain_signal_not_fired_reason_has_values():
    df = _make_swing_df()
    fired, reason = sc.explain_signal(df, "Price<EMA50", "LONG")
    assert fired is False
    # Reason must name the failing factor AND the actual values
    assert "!<" in reason
    assert "Price<EMA50" in reason
    close = float(df.iloc[-1]["Close"])
    assert f"{close:.4g}" in reason


def test_explain_signal_2red_failure_reason():
    df = _make_swing_df()
    # Rising closes → no 2 consecutive red candles → factor fails
    fired, reason = sc.explain_signal(df, "2Red", "LONG")
    assert fired is False
    assert reason.startswith("2Red failed")


def test_compute_signal_still_returns_bool():
    df = _make_swing_df()
    assert sc.compute_signal(df, "Price>SMA50", "LONG") is True
    assert sc.compute_signal(df, "Price<EMA50", "LONG") is False


def test_scan_swing_non_fired_has_reason_and_indicators():
    df = _make_swing_df()
    strategies = pd.DataFrame([
        {"Final_Rank": 1, "Market": "SPY", "Region": "US",
         "Factors": "Price>SMA50", "Direction": "LONG", "AvgWin%": 60.0, "Trades": 100},
        {"Final_Rank": 2, "Market": "SPY", "Region": "US",
         "Factors": "Price<EMA50", "Direction": "LONG", "AvgWin%": 61.0, "Trades": 90},
    ])
    signals = sc.scan_strategies(strategies, {"SPY": df})
    assert len(signals) == 2

    fired = [s for s in signals if s["fired"]]
    non_fired = [s for s in signals if not s["fired"]]
    assert len(fired) == 1
    assert len(non_fired) == 1

    # Fired: reason "All factors met" + indicator snapshot
    assert fired[0]["reason"] == "All factors met"
    assert fired[0]["signal_indicators"] is not None
    assert "Close" in fired[0]["signal_indicators"]

    # Non-fired: explicit failing-factor reason + indicator snapshot
    nf = non_fired[0]
    assert nf["reason"] and "!<" in nf["reason"]
    assert nf["signal_indicators"] is not None
    assert "Close" in nf["signal_indicators"]
    assert "SMA50" in nf["signal_indicators"]


def _make_intraday_df():
    """210 1h candles of monotonically rising closes (latest still forming)."""
    now = pd.Timestamp.now(tz="UTC")
    idx = pd.date_range(end=now + pd.Timedelta(minutes=30), periods=210, freq="h")
    closes = np.linspace(100.0, 200.0, 210)
    df = pd.DataFrame({
        "Open": closes, "High": closes + 1.0, "Low": closes - 1.0,
        "Close": closes, "Volume": [1_000_000] * 210,
    }, index=idx)
    return si.compute_indicators_1h(df)


def test_explain_signal_1h_fired_and_not():
    df = _make_intraday_df()
    fired, reason = si.explain_signal_1h(df, "Price>SMA50", "LONG")
    assert fired is True
    assert reason == "All factors met"

    nf, nreason = si.explain_signal_1h(df, "Price<EMA50", "LONG")
    assert nf is False
    assert "!<" in nreason


def test_compute_signal_1h_still_returns_bool():
    df = _make_intraday_df()
    assert si.compute_signal_1h(df, "Price>SMA50", "LONG") is True
    assert si.compute_signal_1h(df, "Price<EMA50", "LONG") is False


def test_scan_intraday_non_fired_has_reason_and_indicators():
    df = _make_intraday_df()
    strategies = pd.DataFrame([
        {"Final_Rank": 5, "Market": "QQQ", "Region": "US",
         "Factors": "Price<EMA50", "Direction": "LONG", "AvgWin%": 60.0, "Trades": 100},
    ])
    signals = si.scan_intraday_strategies(strategies, {"QQQ": df})
    assert len(signals) == 1
    s = signals[0]
    assert s["fired"] is False
    assert s["reason"] and "!<" in s["reason"]
    assert s["signal_indicators"] is not None
    assert "Close" in s["signal_indicators"]

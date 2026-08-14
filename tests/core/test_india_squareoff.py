"""
Tests for India same-day square-off at 15:30 IST (v5.20+).

Covers:
    - Indian FADE_1h position squares off at/after 15:30 IST
      (reason "Expiry Square Off 15:30 IST")
    - Indian FADE_1h position does NOT square off before 15:30 IST
      (falls back to MaxHold hour logic)
    - Indian GAP_DOWN_1m squares off at 15:30 (via same rule)
    - CRYPTO intraday does NOT square off at 15:30 IST (24/7 market)
    - US intraday does NOT square off via India rule (uses session logic)

The frozen_time fixture (2026-01-15 10:30 IST) is advanced by mutating
FrozenDateTime._FROZEN_NAIVE, which paper_trader.datetime.now(IST) reads.
"""

import pytest
import pandas as pd
from paper_trader import enter_trade, update_trades, load_portfolio
from tests.fixtures.sample_data import build_ohlc_data


def _set_frozen(test_env, naive_dt):
    """Advance the frozen clock to a specific naive datetime (IST)."""
    # test_env is the frozen_time fixture output (naive datetime); the
    # FrozenDateTime class is reachable via paper_trader.datetime
    import paper_trader
    paper_trader.datetime._FROZEN_NAIVE = naive_dt


def _open_india_trades(test_env):
    """Enter Indian FADE + GAP_DOWN trades at a pre-close time."""
    _set_frozen(test_env, pd.Timestamp("2026-01-15 11:32:34").to_pydatetime())
    t1 = enter_trade("INDIAN", "TESTFADE.NS", "SHORT", 100.00,
                     "Fade S5 test", pattern_rank=904, expected_win_rate=41.68,
                     pattern_factors="Fade S5: 5m +1.5%", tf="FADE_1h",
                     sl_override=101.0, tp_override=97.0, max_hold_override=5)
    t2 = enter_trade("INDIAN", "TESTGAP.NS", "LONG", 50.00,
                     "gap down test", pattern_rank=998, expected_win_rate=45.0,
                     pattern_factors="gap_down_single", tf="GAP_DOWN_1m",
                     sl_override=49.85, tp_override=50.5, max_hold_override=5)
    assert t1 is not None and t2 is not None
    return t1, t2


def test_india_fade_squares_off_after_close(test_env):
    """Indian FADE_1h at 17:03 IST → square off at market close."""
    _open_india_trades(test_env)
    _set_frozen(test_env, pd.Timestamp("2026-01-15 17:03:17").to_pydatetime())
    ohlc = build_ohlc_data("TESTFADE.NS", lambda: {"close": 99.0, "high": 100.5, "low": 98.5})
    msgs = update_trades(ohlc)
    assert len(msgs) == 1, f"Expected 1 exit, got {len(msgs)}: {msgs}"
    assert "Square Off 15:30 IST" in msgs[0]

    import paper_trader
    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    row = df[df["Ticker"] == "TESTFADE.NS"].iloc[0]
    assert row["Status"] == "CLOSED"
    assert "Square Off 15:30 IST" in str(row["Reason"])


def test_india_fade_not_squared_before_close(test_env):
    """Indian FADE_1h at 14:00 IST (before close) → NOT squared off by the rule."""
    t1, _ = _open_india_trades(test_env)
    _set_frozen(test_env, pd.Timestamp("2026-01-15 14:00:00").to_pydatetime())
    # 11:32 + 5h MaxHold = 16:32, so at 14:00 neither square-off nor MaxHold fires
    ohlc = build_ohlc_data("TESTFADE.NS", lambda: {"close": 100.0, "high": 100.5, "low": 99.0})
    msgs = update_trades(ohlc)
    assert len(msgs) == 0, f"Expected no exit before close, got {msgs}"


def test_india_fade_squares_off_at_exact_close(test_env):
    """Indian FADE_1h at exactly 15:30:00 IST → squares off."""
    _open_india_trades(test_env)
    _set_frozen(test_env, pd.Timestamp("2026-01-15 15:30:00").to_pydatetime())
    ohlc = build_ohlc_data("TESTFADE.NS", lambda: {"close": 99.0, "high": 100.5, "low": 98.5})
    msgs = update_trades(ohlc)
    assert len(msgs) == 1
    assert "Square Off 15:30 IST" in msgs[0]


def test_india_gapdown_squares_off_after_close(test_env):
    """Indian GAP_DOWN_1m at 17:03 IST → squares off too."""
    _open_india_trades(test_env)
    _set_frozen(test_env, pd.Timestamp("2026-01-15 17:03:17").to_pydatetime())
    ohlc = build_ohlc_data("TESTGAP.NS", lambda: {"close": 50.1, "high": 50.4, "low": 49.9})
    msgs = update_trades(ohlc)
    assert len(msgs) == 1
    assert "Square Off 15:30 IST" in msgs[0]


def test_crypto_intraday_not_squared_at_india_close(test_env):
    """CRYPTO INTRADAY_1h at 17:03 IST → NOT square-off (24/7 market)."""
    _set_frozen(test_env, pd.Timestamp("2026-01-15 11:32:34").to_pydatetime())
    tc = enter_trade("CRYPTO", "BTC-USD", "SHORT", 50000.0,
                     "crypto intraday", pattern_rank=1, expected_win_rate=60.0,
                     pattern_factors="EMA9>EMA20", tf="INTRADAY_1h",
                     sl_override=51000.0, tp_override=48000.0, max_hold_override=6)
    assert tc is not None
    _set_frozen(test_env, pd.Timestamp("2026-01-15 17:03:17").to_pydatetime())
    ohlc = build_ohlc_data("BTC-USD", lambda: {"close": 49500.0, "high": 50100.0, "low": 49300.0})
    msgs = update_trades(ohlc)
    # 11:32 + 6h = 17:32 — at 17:03 the MaxHold hasn't fired either, and no
    # India square-off applies → expect no exit
    assert len(msgs) == 0, f"CRYPTO must not square off at India close, got {msgs}"


def test_us_intraday_not_squared_by_india_rule(test_env):
    """US INTRADAY_1h at 17:03 IST → NOT square-off via India rule."""
    _set_frozen(test_env, pd.Timestamp("2026-01-15 11:32:34").to_pydatetime())
    tu = enter_trade("US", "SPY", "LONG", 450.0,
                     "us intraday", pattern_rank=3, expected_win_rate=60.0,
                     pattern_factors="Price>SMA20", tf="INTRADAY_1h",
                     sl_override=445.0, tp_override=465.0, max_hold_override=6)
    assert tu is not None
    _set_frozen(test_env, pd.Timestamp("2026-01-15 17:03:17").to_pydatetime())
    ohlc = build_ohlc_data("SPY", lambda: {"close": 452.0, "high": 453.0, "low": 448.0})
    msgs = update_trades(ohlc)
    # US session logic applies; at 17:03 IST (11:33 UTC) US market not in session
    # hold budget, no exit expected via India rule
    assert all("Square Off 15:30 IST" not in m for m in msgs), f"US must not use India rule: {msgs}"

"""
Tests for paper_trader.enter_trade() — trade lifecycle, entry rules.

Covers:
    - Duplicate detection (same ticker + direction)
    - Different direction on same ticker (allowed)
    - Many concurrent positions allowed (100-cap, not 5)
    - Intraday/swing pool independence
    - SL/TP rate differences per market mode
    - Intraday tighter SL/TP rates
    - Pattern rank = None (no #Rank prefix in Reason)
    - Qty = 0 via calculate_qty (SL == entry → returns 0)
    - All 20 CSV columns present after entry
    - Capital unchanged on entry (no debit until exit)
    - SHORT entry (both swing and intraday)

Uses existing test_env fixture (frozen time + isolated filesystem + mocks).
Does NOT modify production code or existing fixtures.
"""

import pytest
import pandas as pd
from paper_trader import enter_trade, load_portfolio, COLUMNS


# ======================================================================
# Duplicate Detection
# ======================================================================

def test_duplicate_ticker_direction_rejected(test_env):
    """Same ticker + direction already open → enter_trade returns None."""
    t1 = enter_trade("US", "SPY", "LONG", 450.00, "Test entry 1",
                     pattern_rank=1, expected_win_rate=60.0,
                     pattern_factors="Test", tf="SWING_1d")
    assert t1 is not None
    assert t1["Status"] == "OPEN"

    t2 = enter_trade("US", "SPY", "LONG", 455.00, "Duplicate attempt",
                     pattern_rank=2, expected_win_rate=60.0,
                     pattern_factors="Test", tf="SWING_1d")
    assert t2 is None, "Duplicate ticker+direction should be rejected"


def test_opposite_direction_allowed(test_env):
    """Same ticker, opposite direction → allowed (not a duplicate)."""
    t1 = enter_trade("US", "SPY", "LONG", 450.00, "Long entry",
                     pattern_rank=1, expected_win_rate=60.0,
                     pattern_factors="Test", tf="SWING_1d")
    assert t1 is not None

    t2 = enter_trade("US", "SPY", "SHORT", 455.00, "Short entry",
                     pattern_rank=2, expected_win_rate=60.0,
                     pattern_factors="Test", tf="SWING_1d")
    assert t2 is not None, "Opposite direction should be allowed"
    assert t2["Direction"] == "SHORT"
    assert t2["Ticker"] == "SPY"


def test_duplicate_different_ticker_allowed(test_env):
    """Different ticker, same direction → allowed."""
    t1 = enter_trade("US", "SPY", "LONG", 450.00, "SPY entry",
                     pattern_rank=1, expected_win_rate=60.0,
                     pattern_factors="Test", tf="SWING_1d")
    assert t1 is not None

    t2 = enter_trade("US", "QQQ", "LONG", 500.00, "QQQ entry",
                     pattern_rank=2, expected_win_rate=60.0,
                     pattern_factors="Test", tf="SWING_1d")
    assert t2 is not None, "Different ticker should be allowed"
    assert t2["Ticker"] == "QQQ"


# ======================================================================
# Concurrent Positions (100 total cap — high enough for every signal)
# ======================================================================

def test_many_concurrent_swing_allowed(test_env):
    """5+ open swing positions → next entry still succeeds (cap is 100)."""
    tickers = ["SPY", "QQQ", "IWM", "DIA", "^GSPC", "XLK"]
    for i, t in enumerate(tickers, start=1):
        tr = enter_trade("US", t, "LONG", 450.00, f"Entry {t}",
                         pattern_rank=i, expected_win_rate=60.0,
                         pattern_factors="Test", tf="SWING_1d")
        assert tr is not None, f"Entry #{i} ({t}) should succeed"
        assert tr["Ticker"] == t

    # 7th entry on a fresh ticker — must also succeed (well under the 100 cap)
    t7 = enter_trade("US", "IWB", "LONG", 250.00, "Beyond old cap entry",
                     pattern_rank=99, expected_win_rate=60.0,
                     pattern_factors="Test", tf="SWING_1d")
    assert t7 is not None, "7th swing entry should succeed (cap is 100)"


def test_many_concurrent_intraday_allowed(test_env):
    """3+ open intraday positions → 4th intraday still succeeds (cap is 100)."""
    tickers = ["SPY", "QQQ", "IWM"]
    for i, t in enumerate(tickers, start=1):
        tr = enter_trade("US", t, "LONG", 450.00, f"Intraday {t}",
                         pattern_rank=i, expected_win_rate=60.0,
                         pattern_factors="Test", tf="INTRADAY_1h")
        assert tr is not None, f"Intraday #{i} ({t}) should succeed"
        assert tr["TimeFrame"] == "INTRADAY_1h"

    # 4th intraday entry — must succeed (cap is 100)
    t4 = enter_trade("US", "DIA", "LONG", 350.00, "Beyond old intraday cap",
                     pattern_rank=99, expected_win_rate=60.0,
                     pattern_factors="Test", tf="INTRADAY_1h")
    assert t4 is not None, "4th intraday entry should succeed (cap is 100)"


def test_100_total_position_cap(test_env):
    """100 open positions → 101st returns None."""
    for i in range(1, 101):
        tr = enter_trade("US", f"T{i}", "LONG", 450.00, f"Entry {i}",
                         pattern_rank=i, expected_win_rate=60.0,
                         pattern_factors="Test", tf="SWING_1d")
        assert tr is not None, f"Entry #{i} should succeed"

    # 101st entry on a fresh ticker — must be rejected (cap = 100)
    t101 = enter_trade("US", "T101", "LONG", 450.00, "Over cap",
                       pattern_rank=101, expected_win_rate=60.0,
                       pattern_factors="Test", tf="SWING_1d")
    assert t101 is None, "101st entry should be rejected (cap = 100)"


def test_intraday_separate_pool_from_swing(test_env):
    """3 intraday open → swing entry still works (separate pools)."""
    tickers = ["SPY", "QQQ", "IWM"]
    for t in tickers:
        tr = enter_trade("US", t, "LONG", 450.00, f"Intraday {t}",
                         pattern_rank=1, expected_win_rate=60.0,
                         pattern_factors="Test", tf="INTRADAY_1h")
        assert tr is not None

    # Swing entry should still work despite 3 intraday positions
    swing = enter_trade("US", "XLK", "LONG", 200.00, "Swing after intraday",
                        pattern_rank=2, expected_win_rate=60.0,
                        pattern_factors="Test", tf="SWING_1d")
    assert swing is not None, "Swing entry should work (separate pool from intraday)"
    assert swing["TimeFrame"] == "SWING_1d"


def test_swing_full_intraday_still_allowed(test_env):
    """5 swing open → intraday entry still works (separate pools)."""
    swing_tickers = ["SPY", "QQQ", "IWM", "DIA", "^GSPC"]
    for t in swing_tickers:
        tr = enter_trade("US", t, "LONG", 450.00, f"Swing {t}",
                         pattern_rank=1, expected_win_rate=60.0,
                         pattern_factors="Test", tf="SWING_1d")
        assert tr is not None

    # Intraday entry should still work despite 5 swing positions
    intra = enter_trade("US", "XLK", "LONG", 200.00, "Intraday after swing",
                        pattern_rank=2, expected_win_rate=60.0,
                        pattern_factors="Test", tf="INTRADAY_1h")
    assert intra is not None, "Intraday entry should work (separate pool from swing)"
    assert intra["TimeFrame"] == "INTRADAY_1h"


# ======================================================================
# SL/TP Rate Verification
# ======================================================================

def test_swing_sl_tp_rates(test_env):
    """LONG swing US → SL = entry * 0.98, TP = entry * 1.04 (2% SL, 4% TP)."""
    entry_price = 100.00
    t = enter_trade("US", "SPY", "LONG", entry_price, "Test SL/TP",
                    pattern_rank=1, expected_win_rate=60.0,
                    pattern_factors="Test", tf="SWING_1d")
    assert t is not None
    expected_sl = 98.00   # 100 * (1 - 0.02)
    expected_tp = 104.00  # 100 * (1 + 0.04)
    assert t["SL"] == expected_sl, f"Expected SL={expected_sl}, got {t['SL']}"
    assert t["Target"] == expected_tp, f"Expected TP={expected_tp}, got {t['Target']}"
    assert t["MaxHold"] == 5  # MAX_HOLD_DAYS


def test_swing_sl_tp_rates_short(test_env):
    """SHORT swing US → SL = entry * 1.02, TP = entry * 0.96."""
    entry_price = 100.00
    t = enter_trade("US", "SPY", "SHORT", entry_price, "Test short SL/TP",
                    pattern_rank=1, expected_win_rate=60.0,
                    pattern_factors="Test", tf="SWING_1d")
    assert t is not None
    expected_sl = 102.00   # 100 * (1 + 0.02)
    expected_tp = 96.00    # 100 * (1 - 0.04)
    assert t["SL"] == expected_sl, f"Expected SL={expected_sl}, got {t['SL']}"
    assert t["Target"] == expected_tp, f"Expected TP={expected_tp}, got {t['Target']}"


def test_intraday_sl_tp_rates(test_env):
    """LONG intraday US → SL = entry * 0.99, TP = entry * 1.02 (1% SL, 2% TP)."""
    entry_price = 100.00
    t = enter_trade("US", "SPY", "LONG", entry_price, "Test intraday SL/TP",
                    pattern_rank=1, expected_win_rate=60.0,
                    pattern_factors="Test", tf="INTRADAY_1h")
    assert t is not None
    expected_sl = 99.00    # 100 * (1 - 0.01)
    expected_tp = 102.00   # 100 * (1 + 0.02)
    assert t["SL"] == expected_sl, f"Expected SL={expected_sl}, got {t['SL']}"
    assert t["Target"] == expected_tp, f"Expected TP={expected_tp}, got {t['Target']}"


def test_intraday_sl_tp_crypto(test_env):
    """LONG intraday CRYPTO → SL = entry * 0.985, TP = entry * 1.03 (1.5% SL, 3% TP)."""
    entry_price = 100.00
    t = enter_trade("CRYPTO", "BTC-USD", "LONG", entry_price, "Crypto intraday",
                    pattern_rank=1, expected_win_rate=60.0,
                    pattern_factors="Test", tf="INTRADAY_1h")
    assert t is not None
    expected_sl = 98.50    # 100 * (1 - 0.015)
    expected_tp = 103.00   # 100 * (1 + 0.03)
    assert t["SL"] == expected_sl, f"Expected SL={expected_sl}, got {t['SL']}"
    assert t["Target"] == expected_tp, f"Expected TP={expected_tp}, got {t['Target']}"


# ======================================================================
# Pattern Rank / Reason Formatting
# ======================================================================

def test_entry_no_pattern_rank(test_env):
    """Pattern_rank = None → Reason has no #Rank prefix."""
    t = enter_trade("US", "SPY", "LONG", 450.00, "Manual signal",
                    pattern_rank=None, tf="SWING_1d")
    assert t is not None
    assert t["Pattern_Rank"] == "", "Pattern_Rank should be empty string"
    assert t["Reason"] == "Manual signal", (
        f"Reason should not have #Rank prefix: '{t['Reason']}'"
    )


# ======================================================================
# Edge Cases
# ======================================================================

def test_entry_computes_positive_qty(test_env):
    """Basic entry always produces Qty > 0 (SL differs from entry price)."""
    from paper_trader import calculate_qty
    # Prove that SL=entry gives qty=0 via calculate_qty
    zero_qty = calculate_qty(100.00, 100.00, "US", "SWING_1d")
    assert zero_qty == 0, "calculate_qty should return 0 when SL == entry"
    # Now prove enter_trade produces positive qty for a normal case
    t = enter_trade("US", "SPY", "LONG", 450.00, "Normal entry",
                    pattern_rank=1, expected_win_rate=60.0,
                    pattern_factors="Test", tf="SWING_1d")
    assert t is not None
    assert t["Qty"] > 0


@pytest.mark.parametrize("mode,ticker", [
    ("US", "SPY"),
    ("CRYPTO", "BTC-USD"),
])
def test_entry_csv_all_columns(test_env, mode, ticker):
    """After entry, paper_trades.csv contains all 20 columns with correct values."""
    t = enter_trade(mode, ticker, "LONG", 100.00, f"Test {mode}",
                    pattern_rank=10, expected_win_rate=65.0,
                    pattern_factors="Test>Factors",
                    tf="SWING_1d")
    import paper_trader
    assert t is not None
    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    assert len(df) == 1
    for col in COLUMNS:
        assert col in df.columns, f"Column '{col}' missing from CSV"
    row = df.iloc[0]
    assert row["Ticker"] == ticker
    assert row["Status"] == "OPEN"
    assert row["Direction"] == "LONG"
    # pandas may read numeric csv values as float/int, so compare via str()
    assert str(row["Pattern_Rank"]) == "10", f"Expected Pattern_Rank=10, got {row['Pattern_Rank']!r}"
    assert row["Expected_WinRate"] == "" or float(row["Expected_WinRate"]) == 65.0, (
        f"Expected_WinRate mismatch: {row['Expected_WinRate']!r}"
    )
    assert row["TimeFrame"] == "SWING_1d"


# ======================================================================
# Capital Management on Entry
# ======================================================================

def test_capital_unchanged_on_entry(test_env):
    """Capital is NOT debited on entry — only updated on exit."""
    port_before = load_portfolio()
    cap_before = dict(port_before["capital_by_market"])
    total_before = port_before["total_pnl"]

    t = enter_trade("US", "SPY", "LONG", 450.00, "Test capital unchanged",
                    pattern_rank=1, expected_win_rate=60.0,
                    pattern_factors="Test", tf="SWING_1d")
    assert t is not None

    port_after = load_portfolio()
    assert port_after["capital_by_market"] == cap_before, (
        f"Capital changed on entry: {cap_before} → {port_after['capital_by_market']}"
    )
    assert port_after["total_pnl"] == total_before, "Total P&L changed on entry"
    assert len(port_after["open_positions"]) == 1


# ======================================================================
# SHORT Entries
# ======================================================================

def test_short_swing_entry(test_env):
    """SHORT swing entry creates correct SL/TP and direction."""
    t = enter_trade("US", "QQQ", "SHORT", 500.00, "Short swing entry",
                    pattern_rank=5, expected_win_rate=60.0,
                    pattern_factors="Price<SMA50+EMA9>EMA20",
                    tf="SWING_1d")
    assert t is not None
    assert t["Direction"] == "SHORT"
    assert t["Ticker"] == "QQQ"
    assert t["SL"] > t["Entry_Price"], (
        f"SHORT SL ({t['SL']}) should be above entry ({t['Entry_Price']})"
    )
    assert t["Target"] < t["Entry_Price"], (
        f"SHORT TP ({t['Target']}) should be below entry ({t['Entry_Price']})"
    )


def test_short_intraday_entry(test_env):
    """SHORT intraday entry uses tight SL/TP and correct TimeFrame."""
    t = enter_trade("US", "IWM", "SHORT", 225.00, "Short intraday entry",
                    pattern_rank=8, expected_win_rate=64.44,
                    pattern_factors="Price>SMA20+Range>1.5%",
                    tf="INTRADAY_1h")
    assert t is not None
    assert t["Direction"] == "SHORT"
    assert t["TimeFrame"] == "INTRADAY_1h"
    # Intraday SHORT SL: entry * 1.01 (1% of 225 = 2.25)
    assert t["SL"] == pytest.approx(227.25, abs=0.01), (
        f"Expected SL ~227.25, got {t['SL']}"
    )
    # Intraday SHORT TP: entry * 0.98 (2% of 225 = 4.50)
    assert t["Target"] == pytest.approx(220.50, abs=0.01), (
        f"Expected TP ~220.50, got {t['Target']}"
    )

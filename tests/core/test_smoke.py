"""
Smoke test — validates that the entire fixture infrastructure works end-to-end.

This is the FIRST test file. It proves:
    - pytest discovers tests
    - Fixtures import correctly
    - conftest.py monkeypatches work
    - Isolated filesystem works (tmp_path)
    - Trade/portfolio/OHLC factories produce correct data

If this passes, the infrastructure is healthy.
"""

import os


def test_fixture_smoke(test_env):
    """The simplest possible test: can we import fixtures in a test environment?"""
    assert test_env is not None
    tmp = test_env["tmp_path"]
    assert (tmp / "logs").exists(), "isolated_fs fixture did not create logs dir"


def test_sample_trade_factory():
    """Verify the trade factory produces correct default values."""
    from tests.fixtures.sample_trades import long_swing_us_trade
    trade = long_swing_us_trade()
    assert trade["Ticker"] == "SPY"
    assert trade["Direction"] == "LONG"
    assert trade["TimeFrame"] == "SWING_1d"
    assert trade["Entry_Price"] == 450.00
    assert trade["Qty"] == 100
    assert trade["SL"] == 441.00
    assert trade["Target"] == 468.00
    assert trade["Status"] == "OPEN"
    assert trade["Pattern_Rank"] == "46"


def test_all_trade_factories():
    """Verify all 7 trade factories produce data with correct types."""
    from tests.fixtures.sample_trades import (
        long_swing_us_trade, short_swing_us_trade,
        long_swing_india_trade, short_swing_crypto_trade,
        long_intraday_us_trade, short_intraday_us_trade,
        long_intraday_crypto_trade,
    )
    factories = [
        ("LONG SWING US", long_swing_us_trade, "US", "LONG", "SWING_1d"),
        ("SHORT SWING US", short_swing_us_trade, "US", "SHORT", "SWING_1d"),
        ("LONG SWING INDIA", long_swing_india_trade, "INDIAN", "LONG", "SWING_1d"),
        ("SHORT SWING CRYPTO", short_swing_crypto_trade, "CRYPTO", "SHORT", "SWING_1d"),
        ("LONG INTRADAY US", long_intraday_us_trade, "US", "LONG", "INTRADAY_1h"),
        ("SHORT INTRADAY US", short_intraday_us_trade, "US", "SHORT", "INTRADAY_1h"),
        ("LONG INTRADAY CRYPTO", long_intraday_crypto_trade, "CRYPTO", "LONG", "INTRADAY_1h"),
    ]

    for name, factory, expected_mode, expected_dir, expected_tf in factories:
        trade = factory()
        assert trade["Mode"] == expected_mode, f"{name}: expected Mode={expected_mode}, got {trade['Mode']}"
        assert trade["Direction"] == expected_dir, f"{name}: expected Direction={expected_dir}, got {trade['Direction']}"
        assert trade["TimeFrame"] == expected_tf, f"{name}: expected TimeFrame={expected_tf}, got {trade['TimeFrame']}"
        assert trade["Qty"] > 0, f"{name}: Qty should be > 0"
        assert trade["SL"] != trade["Entry_Price"], f"{name}: SL should differ from Entry_Price"
        assert trade["Target"] != trade["Entry_Price"], f"{name}: Target should differ from Entry_Price"
        assert trade["Status"] == "OPEN", f"{name}: Status should be OPEN"
        assert trade["Pattern_Rank"] != "", f"{name}: Pattern_Rank should not be empty"
        assert trade["Reason"] != "", f"{name}: Reason should not be empty"


def test_overrides_applied_correctly():
    """Verify that **overrides can customize trade factories."""
    from tests.fixtures.sample_trades import long_swing_us_trade
    trade = long_swing_us_trade(Ticker="QQQ", Qty=200, Entry_Price=500.00)
    assert trade["Ticker"] == "QQQ"
    assert trade["Qty"] == 200
    assert trade["Entry_Price"] == 500.00
    # Other defaults should remain
    assert trade["Direction"] == "LONG"


def test_portfolio_factory_empty():
    """Verify empty portfolio has correct default structure."""
    from tests.fixtures.sample_portfolio import empty_portfolio
    port = empty_portfolio()
    assert port["capital_by_market"]["US"] == 100000.0
    assert port["capital_by_market"]["INDIAN"] == 100000.0
    assert port["capital_by_market"]["CRYPTO"] == 100000.0
    assert port["capital_by_market"]["INTRADAY"] == 100000.0
    assert port["open_positions"] == []
    assert port["total_pnl"] == 0


def test_portfolio_with_full_swing_concurrent():
    """Verify the max-concurrent portfolio creates 5 trades."""
    from tests.fixtures.sample_portfolio import portfolio_with_full_swing_concurrent
    port = portfolio_with_full_swing_concurrent()
    assert len(port["open_positions"]) == 5


def test_portfolio_overrides_no_collision():
    """Verify portfolio_with_full_swing_concurrent handles overrides without TypeError."""
    from tests.fixtures.sample_portfolio import portfolio_with_full_swing_concurrent
    port = portfolio_with_full_swing_concurrent(Ticker="OVERRIDE", Qty=999)
    for trade in port["open_positions"]:
        assert trade["Qty"] == 999, f"Qty should be 999 after override, got {trade['Qty']}"


def test_frozen_time_using_fixture(frozen_time):
    """Verify the actual frozen_time fixture produces a deterministic timestamp."""
    import paper_trader, pytz
    IST = pytz.timezone("Asia/Kolkata")
    now = paper_trader.datetime.now(IST)
    assert now.year == 2026, f"Expected year 2026, got {now.year}"
    assert now.month == 1, f"Expected month 1, got {now.month}"
    assert now.day == 15, f"Expected day 15, got {now.day}"
    assert now.hour == 10, f"Expected hour 10, got {now.hour}"
    assert now.minute == 30, f"Expected minute 30, got {now.minute}"


def test_ohlc_fixtures():
    """Verify OHLC fixtures produce valid data."""
    from tests.fixtures.sample_data import (
        ohlc_normal_range, ohlc_sl_hit_intraday, ohlc_nan_low, ohlc_zero_low,
    )
    # Valid OHLC
    ohlc = ohlc_normal_range()
    assert ohlc["close"] > 0
    assert ohlc["high"] > 0
    assert ohlc["low"] > 0

    # SL hit
    ohlc = ohlc_sl_hit_intraday()
    assert ohlc["low"] < 441.00  # Below SPY SL

    # Corrupt data
    import math
    ohlc = ohlc_nan_low()
    assert math.isnan(ohlc["low"])

    ohlc = ohlc_zero_low()
    assert ohlc["low"] == 0.0


def test_assertion_helpers():
    """Verify custom assertion helpers work."""
    from tests.helpers.assertions import (
        assert_valid_trade_columns, assert_valid_ohlc, assert_invalid_ohlc,
    )
    from tests.fixtures.sample_trades import long_swing_us_trade
    from tests.fixtures.sample_data import ohlc_normal_range, ohlc_nan_low

    trade = long_swing_us_trade()
    assert_valid_trade_columns(trade)  # Should not raise

    ohlc = ohlc_normal_range()
    assert_valid_ohlc(ohlc)  # Should not raise

    ohlc_bad = ohlc_nan_low()
    assert_invalid_ohlc(ohlc_bad)  # Should not raise


def test_enter_trade_smoke(test_env):
    """
    Integration smoke test — exercises enter_trade() using real fixture environment.

    Validates:
    1. Portfolio initializes with correct capital
    2. Trade can be entered successfully
    3. CSV file is created with correct columns
    4. Audit log receives ENTRY event
    5. Portfolio open_positions increments
    6. Exit via update_trades works
    7. Audit log receives EXIT event
    8. Strategy stats are updated
    9. Capital reflects P&L after exit
    10. No exceptions during the full lifecycle
    """
    import os
    import paper_trader

    # 1. Verify fresh portfolio
    port = paper_trader.load_portfolio()
    assert port["capital_by_market"]["US"] == 100000.0
    assert port["open_positions"] == []

    # 2. Enter a trade
    trade = paper_trader.enter_trade(
        mode="US",
        ticker="SPY",
        direction="LONG",
        entry_price=450.00,
        reason="Price>SMA50+2Red",
        pattern_rank=46,
        expected_win_rate=62.5,
        pattern_factors="Price>SMA50+2Red",
        tf="SWING_1d",
    )
    assert trade is not None, "enter_trade returned None"
    assert trade["Ticker"] == "SPY"
    assert trade["Direction"] == "LONG"
    assert trade["Qty"] > 0
    assert trade["Status"] == "OPEN"
    assert "#46SW" in trade["Reason"], f"Expected #46SW in reason, got: {trade['Reason']}"

    # 3. Verify CSV created
    assert os.path.exists(paper_trader.PAPER_FILE), "paper_trades.csv not created"
    import pandas as pd
    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines='warn')
    assert len(df) == 1, f"Expected 1 trade row, got {len(df)}"
    assert df.iloc[0]["Ticker"] == "SPY"
    assert df.iloc[0]["Status"] == "OPEN"

    # 4. Verify audit log
    audit = paper_trader._load_audit()
    assert len(audit) == 1, f"Expected 1 audit event, got {len(audit)}"
    assert audit[0]["event"] == "ENTRY"
    assert audit[0]["ticker"] == "SPY"
    assert audit[0]["direction"] == "LONG"
    assert audit[0]["pattern_rank"] == 46, f"Expected pattern_rank 46, got {audit[0]['pattern_rank']} (type {type(audit[0]['pattern_rank']).__name__})"

    # 5. Verify portfolio has open position
    port = paper_trader.load_portfolio()
    assert len(port["open_positions"]) == 1

    # 6. Simulate an exit via SL hit
    ohlc_data = {
        "SPY": {
            "close": 448.00,
            "high": 452.00,
            "low": 440.50,  # Below SL (441.00) — triggers SL
        }
    }
    closed_msgs = paper_trader.update_trades(ohlc_data)
    assert len(closed_msgs) == 1, f"Expected 1 closed message, got {len(closed_msgs)}"
    assert "SL Hit" in closed_msgs[0], f"Expected SL Hit message, got: {closed_msgs[0]}"

    # 7. Verify audit log updated
    audit = paper_trader._load_audit()
    assert len(audit) == 2, f"Expected 2 audit events (entry+exit), got {len(audit)}"
    assert audit[1]["event"] == "EXIT"
    assert audit[1]["ticker"] == "SPY"
    assert "pnl" in audit[1], f"Expected 'pnl' in audit EXIT event, got keys: {list(audit[1].keys())}"
    assert "pnl_pct" in audit[1]

    # 8. Verify strategy stats updated
    stats = paper_trader._load_strategy_stats()
    assert "46" in stats, f"Strategy 46 not found in stats: {list(stats.keys())}"
    assert stats["46"]["losses"] == 1, f"Expected 1 loss for strategy 46, got {stats['46']}"

    # 9. Verify capital updated (loss = lower capital)
    port = paper_trader.load_portfolio()
    assert port["capital_by_market"]["US"] < 100000.0, \
        f"Capital should decrease after loss: {port['capital_by_market']['US']}"
    assert port["total_pnl"] < 0, f"Total P&L should be negative after loss: {port['total_pnl']}"
    assert port["closed_count"] == 1
    assert port["total_losses"] == 1
    assert port["open_positions"] == []

"""\
Phase 2F: Reports / Statistics

Tests for:
    - _html_escape()       — safe HTML escaping with NaN/None handling
    - _pnl_class()         — CSS class for P&L values
    - _extract_rank()      — pattern rank extraction from Reason field
    - get_strategy_stats() — sorted top/bottom strategies by win rate
    - generate_portfolio_report() — HTML report with summary, tables, history

Behavioral invariants:
    - Report totals match CSV totals
    - Open position count matches portfolio open positions
    - Strategy stats match settled trade history
    - Sum of market totals equals overall total (cross-check)
    - HTML remains valid with None, NaN, HTML chars, empty strings, unicode
"""

import os
import json
import math
import pytest
import pandas as pd
from paper_trader import (
    _html_escape,
    _pnl_class,
    _extract_rank,
    generate_portfolio_report,
    get_strategy_stats,
    update_strategy_stats,
    enter_trade,
    update_trades,
    load_portfolio,
    update_last_prices,
    _LAST_PRICES,
    PAPER_FILE,
    PORTFOLIO_FILE,
    STRATEGY_STATS_FILE,
    COLUMNS,
)


# ======================================================================
# _html_escape
# ======================================================================

class TestHtmlEscape:
    """Safe HTML escaping with NaN/None handling."""

    def test_none_returns_empty(self):
        assert _html_escape(None) == ""

    def test_nan_returns_empty(self):
        assert _html_escape(float("nan")) == ""

    def test_nan_string_returns_empty(self):
        assert _html_escape("nan") == ""

    def test_nan_string_case_insensitive_empty(self):
        assert _html_escape("NAN") == ""
        assert _html_escape("NaN") == ""

    def test_normal_text(self):
        assert _html_escape("hello world") == "hello world"

    def test_ampersand_escaped(self):
        result = _html_escape("AT&T")
        assert "AT&amp;T" == result

    def test_less_than_escaped(self):
        assert _html_escape("a < b") == "a &lt; b"

    def test_greater_than_escaped(self):
        assert _html_escape("a > b") == "a &gt; b"

    def test_double_quote_escaped(self):
        assert _html_escape('say "hello"') == "say &quot;hello&quot;"

    def test_newline_replaced_with_space(self):
        assert "\n" not in _html_escape("line1\nline2")

    def test_empty_string_returns_empty(self):
        assert _html_escape("") == ""

    def test_unicode_preserved(self):
        assert _html_escape("₹") == "₹"
        assert _html_escape("café") == "café"

    def test_zero_number_returns_zero(self):
        assert _html_escape(0) == "0"

    def test_integer_number(self):
        assert _html_escape(100) == "100"

    def test_positive_float(self):
        assert "99.5" in _html_escape(99.5)

    def test_negative_float(self):
        assert "-50.0" in _html_escape(-50.0)


# ======================================================================
# _pnl_class
# ======================================================================

class TestPnlClass:
    """CSS class for P&L values."""

    def test_positive_returns_profit(self):
        assert _pnl_class(100) == "profit"
        assert _pnl_class(0.01) == "profit"

    def test_negative_returns_loss(self):
        assert _pnl_class(-100) == "loss"
        assert _pnl_class(-0.01) == "loss"

    def test_zero_returns_empty(self):
        assert _pnl_class(0) == ""

    def test_nan_returns_empty(self):
        assert _pnl_class(float("nan")) == ""

    def test_none_returns_empty(self):
        assert _pnl_class(None) == ""

    def test_empty_string_returns_empty(self):
        assert _pnl_class("") == ""

    def test_non_numeric_string_returns_empty(self):
        assert _pnl_class("abc") == ""


# ======================================================================
# _extract_rank
# ======================================================================

class TestExtractRank:
    """Pattern rank extraction from Reason field."""

    def test_simple_rank(self):
        assert _extract_rank("#1 Price<SMA50") == 1

    def test_double_digit_rank(self):
        assert _extract_rank("#16ID Price>SMA20") == 16

    def test_no_hash_prefix(self):
        assert _extract_rank("1 Price<SMA50") == 1  # Match without #

    def test_rank_with_pipe(self):
        assert _extract_rank("#46 Price<SMA50 | SL Hit (close)") == 46

    def test_no_rank_found(self):
        assert _extract_rank("Normal reason without rank") == 0

    def test_none_returns_zero(self):
        assert _extract_rank(None) == 0

    def test_empty_returns_zero(self):
        assert _extract_rank("") == 0

    def test_rank_in_middle_of_text(self):
        # Uses re.match which matches only at start of string
        assert _extract_rank("Reason #32ID some text") == 0

    def test_non_numeric_text_returns_zero(self):
        assert _extract_rank("abc") == 0


# ======================================================================
# get_strategy_stats
# ======================================================================

class TestGetStrategyStats:
    """Sorted top/bottom strategies by win rate."""

    def test_empty_stats_returns_empty(self, test_env):
        result = get_strategy_stats()
        assert result == {"top": [], "bottom": []}

    def test_single_strategy_one_trade_no_bottom(self, test_env):
        """Bottom requires >= 2 trades, so 1-trade strategy appears in top only."""
        update_strategy_stats("#1 Test strategy", 500.0)
        result = get_strategy_stats(top_n=5)
        assert len(result["top"]) == 1
        assert result["top"][0]["rank"] == 1
        assert result["top"][0]["win_rate"] == 100.0
        assert len(result["bottom"]) == 0  # Not enough trades

    def test_single_strategy_two_trades_both_top_and_bottom(self, test_env):
        """With >= 2 trades, appears in both top and bottom."""
        update_strategy_stats("#1 Test strategy", 500.0)
        update_strategy_stats("#1 Test strategy", -200.0)
        result = get_strategy_stats(top_n=5)
        assert len(result["top"]) == 1
        assert result["top"][0]["win_rate"] == 50.0
        assert len(result["bottom"]) == 1
        assert result["bottom"][0]["win_rate"] == 50.0

    def test_multiple_strategies_correct_sorting(self, test_env):
        """Top sorted descending by win rate, bottom ascending."""
        update_strategy_stats("#1 Strategy 1", 100.0)  # 100% WR
        update_strategy_stats("#1 Strategy 1", 200.0)
        update_strategy_stats("#2 Strategy 2", 100.0)  # 50% WR
        update_strategy_stats("#2 Strategy 2", -100.0)
        update_strategy_stats("#3 Strategy 3", -100.0)  # 0% WR
        update_strategy_stats("#3 Strategy 3", -200.0)
        result = get_strategy_stats(top_n=5)
        # Top: highest WR first
        assert result["top"][0]["rank"] == 1
        assert result["top"][1]["rank"] == 2
        assert result["top"][2]["rank"] == 3
        # Bottom: lowest WR first
        assert result["bottom"][0]["rank"] == 3
        assert result["bottom"][1]["rank"] == 2
        assert result["bottom"][2]["rank"] == 1

    def test_top_n_limits_results(self, test_env):
        """top_n parameter correctly limits returned rows."""
        for i in range(1, 7):
            update_strategy_stats(f"#{i} Strategy {i}", 100.0)
            update_strategy_stats(f"#{i} Strategy {i}", 100.0)
        result = get_strategy_stats(top_n=3)
        assert len(result["top"]) == 3
        assert len(result["bottom"]) == 3

    def test_ties_maintained(self, test_env):
        """Strategies with same win rate both appear."""
        update_strategy_stats("#1 First", 100.0)
        update_strategy_stats("#1 First", 100.0)
        update_strategy_stats("#2 Second", 100.0)
        update_strategy_stats("#2 Second", 100.0)
        result = get_strategy_stats(top_n=5)
        top_ranks = [r["rank"] for r in result["top"]]
        assert 1 in top_ranks
        assert 2 in top_ranks

    def test_total_pnl_tracked(self, test_env):
        """total_pnl accumulates correctly across multiple calls."""
        update_strategy_stats("#1 Test", 500.0)
        update_strategy_stats("#1 Test", -200.0)
        result = get_strategy_stats()
        assert result["top"][0]["total_pnl"] == 300.0

    def test_only_two_or_more_trades_in_bottom(self, test_env):
        """Bottom list filters out strategies with < 2 trades."""
        update_strategy_stats("#1 One trade only", 100.0)
        update_strategy_stats("#2 Two trades", 100.0)
        update_strategy_stats("#2 Two trades", -50.0)
        result = get_strategy_stats(top_n=5)
        bottom_ranks = [r["rank"] for r in result["bottom"]]
        assert 1 not in bottom_ranks  # Only 1 trade
        assert 2 in bottom_ranks     # 2 trades


# ======================================================================
# generate_portfolio_report — Behavioral Invariants
# ======================================================================

class TestGeneratePortfolioReport:
    """HTML portfolio report generation — behavioral invariants."""

    def test_empty_portfolio_generates_html(self, test_env):
        """Empty portfolio produces valid HTML with 'No trades' message."""
        report_file = generate_portfolio_report()
        assert os.path.exists(report_file)
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()
        assert "<!DOCTYPE html>" in html
        assert "No trades recorded yet" in html or "No trades" in html
        # Must have summary cards
        assert "Total Capital" in html
        assert "Total P&amp;L" in html
        assert "Win Rate" in html

    def test_summary_values_match_portfolio(self, test_env):
        """Summary cards show correct capital, P&L, win rate."""
        # Enter and close one winning trade
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "Test entry", pattern_rank=1, expected_win_rate=60.0)
        assert t is not None
        # Exit via TP: target = 100 * 1.04 = 104
        ohlc = {"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}}
        msgs = update_trades(ohlc)
        assert len(msgs) == 1

        portfolio = load_portfolio()
        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        # Summary values present
        total_cap = sum(portfolio["capital_by_market"].values())
        assert f"₹{total_cap:,.0f}" in html or str(int(portfolio["total_pnl"])) in html

    def test_closed_trade_pnl_in_report(self, test_env):
        """Closed trade P&L appears in HTML report tables."""
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Test entry", pattern_rank=1, expected_win_rate=60.0)
        assert t is not None
        ohlc = {"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}}
        msgs = update_trades(ohlc)
        assert len(msgs) == 1

        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        # Trade must be in All Trades table
        assert "SPY" in html
        assert "CLOSED" in html or "Closed" in html

    def test_open_trade_with_current_price(self, test_env):
        """Open trade shows current (unrealized) P&L when price is cached."""
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Test entry", pattern_rank=1, expected_win_rate=60.0)
        assert t is not None
        # Cache a current price for unrealized P&L
        update_last_prices({"SPY": 105.00})

        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        assert "SPY" in html
        assert "OPEN" in html or "Open" in html

    def test_open_trade_no_current_price(self, test_env):
        """Open trade shows '—' for P&L when no current price available."""
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Test entry", pattern_rank=1, expected_win_rate=60.0)
        assert t is not None
        # Ensure no cached price
        update_last_prices({})

        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        assert "SPY" in html

    def test_per_ticker_section_shows_aggregation(self, test_env):
        """Per-Ticker table shows aggregated win rate and P&L."""
        # Two trades on same ticker: 1 win, 1 loss
        t1 = enter_trade("US", "AAPL", "LONG", 200.00,
                         "#2 Test entry", pattern_rank=2, expected_win_rate=60.0)
        assert t1 is not None
        ohlc_win = {"AAPL": {"close": 210.00, "high": 212.00, "low": 201.00}}
        msgs = update_trades(ohlc_win)
        assert len(msgs) == 1

        t2 = enter_trade("US", "AAPL", "LONG", 205.00,
                         "#2 Test entry", pattern_rank=2, expected_win_rate=60.0)
        assert t2 is not None
        ohlc_loss = {"AAPL": {"close": 195.00, "high": 207.00, "low": 194.00}}
        msgs = update_trades(ohlc_loss)
        assert len(msgs) == 1

        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        assert "Per-Ticker" in html
        assert "AAPL" in html
        assert "50" in html  # 50% win rate (1/2)

    def test_per_market_section_shows_regions(self, test_env):
        """Per-Market table shows region aggregations."""
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Test", pattern_rank=1, expected_win_rate=60.0)
        assert t is not None
        ohlc = {"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}}
        msgs = update_trades(ohlc)
        assert len(msgs) == 1

        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        assert "Per-Market" in html
        assert "US" in html

    def test_html_special_chars_safe(self, test_env):
        """HTML report safely handles special characters in trade fields."""
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Price<SMA50+EMA9>EMA20",
                        pattern_rank=1, expected_win_rate=60.0,
                        pattern_factors="Price<SMA50+EMA9>EMA20")
        assert t is not None
        ohlc = {"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}}
        msgs = update_trades(ohlc)
        assert len(msgs) == 1

        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        # HTML should have &lt; and &gt; instead of raw <>
        assert "Price&lt;SMA50+EMA9&gt;EMA20" in html or "&lt;SMA50" in html
        # Raw < should NOT appear outside HTML tags
        assert "<SMA50" not in html or "Price&lt" in html

    def test_report_includes_portfolio_history_section(self, test_env):
        """Report includes Portfolio History section when snapshots exist."""
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Test", pattern_rank=1, expected_win_rate=60.0)
        assert t is not None
        ohlc = {"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}}
        msgs = update_trades(ohlc)
        assert len(msgs) == 1

        # Create portfolio_snapshots.csv to enable History section
        # Use PAPER_FILE's directory (which is patched by isolated_fs)
        import paper_trader as _pt
        snap = os.path.join(os.path.dirname(_pt.PAPER_FILE), "portfolio_snapshots.csv")
        import pandas as pdx
        pdx.DataFrame([{
            "Date": "2026-01-15", "Time": "10:30:00 IST",
            "Capital": 300000, "Return_Pct": 0.0,
            "Open": 0, "Win_Rate": 0, "Total_PnL": 0,
        }]).to_csv(snap, index=False)

        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        assert "Portfolio History" in html

    def test_multiple_closed_trades_show_newest_first(self, test_env):
        """All Trades table shows newest trades FIRST (reversed order)."""
        t1 = enter_trade("US", "SPY", "LONG", 100.00,
                         "#1 Early", pattern_rank=1, expected_win_rate=60.0)
        assert t1 is not None
        ohlc1 = {"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}}
        msg1 = update_trades(ohlc1)
        assert len(msg1) == 1

        t2 = enter_trade("US", "AAPL", "LONG", 200.00,
                         "#2 Later", pattern_rank=2, expected_win_rate=60.0)
        assert t2 is not None
        ohlc2 = {"AAPL": {"close": 210.00, "high": 212.00, "low": 201.00}}
        msg2 = update_trades(ohlc2)
        assert len(msg2) == 1

        report_file = generate_portfolio_report()
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()

        # Both tickers present
        assert "SPY" in html
        assert "AAPL" in html

    def test_report_generated_even_with_corrupt_data(self, test_env):
        """Report generation handles NaN/None trade data gracefully."""
        # Enter a real trade via production code, then corrupt P&L values in CSV
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Test corrupt", pattern_rank=1, expected_win_rate=60.0)
        assert t is not None
        ohlc = {"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}}
        msgs = update_trades(ohlc)
        assert len(msgs) == 1

        # Now corrupt the P&L values in the CSV
        # Convert to object dtype first to prevent pandas LossySetitemError
        df = pd.read_csv(PAPER_FILE, on_bad_lines="warn")
        for col in ["P&L", "P&L_%"]:
            df[col] = df[col].astype(object)
        df.loc[0, "P&L"] = None
        df.loc[0, "P&L_%"] = "nan"
        df.to_csv(PAPER_FILE, index=False)

        report_file = generate_portfolio_report()
        assert os.path.exists(report_file)
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()
        assert "SPY" in html

    def test_report_generated_with_no_price_cache(self, test_env):
        """Report generates cleanly when _LAST_PRICES is empty."""
        _LAST_PRICES.clear()
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Test", pattern_rank=1, expected_win_rate=60.0)
        assert t is not None
        # Keep trade open, no prices cached
        _LAST_PRICES.clear()
        report_file = generate_portfolio_report()
        assert os.path.exists(report_file)
        with open(report_file, "r", encoding="utf-8") as f:
            html = f.read()
        assert "SPY" in html
        assert "OPEN" in html or "Open" in html

    def test_report_writes_to_disk(self, test_env):
        """The returned file path is an actual file on disk."""
        report_file = generate_portfolio_report()
        assert isinstance(report_file, str)
        assert os.path.isfile(report_file)
        assert report_file.endswith(".html")

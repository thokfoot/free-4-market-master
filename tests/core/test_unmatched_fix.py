"""Regression tests for the 'Unmatched Trades' root-cause fix.

Historical bug (2026-08-11): strategy_report.xlsx reported 21 unmatched
trades because:
1. CSV Market aliases ('XLK_Tech', 'XLF_Fin') were only resolved via exact
   TICKER_MAP lookup, while the scanners use fuzzy substring matching.
   (7 trades: XLK #3/#28/#75, XLF #38)
2. Gap-down rows (997/998) have CSV TF='1m' but were hard-coded to
   INTRADAY_1h, and their Market/Ticker is the region placeholder 'INDIAN'
   while trades carry real tickers (PFC.NS...). (14 trades)

These tests lock the fix: every trade in paper_trades.csv must resolve to
a strategy definition (unmatched == 0), and region placeholders must NOT
be fuzzy-matched into unrelated tickers (e.g. 'INDIAN' -> 'DIA').
"""
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import strategy_report as sr


class TestResolveTicker:
    def test_exact_match(self):
        assert sr._resolve_ticker("QQQ") == "QQQ"
        assert sr._resolve_ticker("Nasdaq100") == "^NDX"
        assert sr._resolve_ticker("SP500") == "^GSPC"

    def test_fuzzy_alias_xlk(self):
        assert sr._resolve_ticker("XLK_Tech") == "XLK"

    def test_fuzzy_alias_xlf(self):
        assert sr._resolve_ticker("XLF_Fin") == "XLF"

    def test_region_placeholder_not_fuzzy_matched(self):
        # 'DIA' is a substring of 'INDIAN' ("inDIA n") - must NOT resolve.
        assert sr._resolve_ticker("INDIAN") == "INDIAN"
        assert sr._resolve_ticker("CRYPTO") == "CRYPTO"

    def test_unknown_market_passthrough(self):
        assert sr._resolve_ticker("NOT_A_TICKER") == "NOT_A_TICKER"


class TestResolveTf:
    def test_gapdown_tf(self):
        assert sr._resolve_tf("SWING_1d", "1m") == "GAP_DOWN_1m"

    def test_intraday_tf(self):
        assert sr._resolve_tf("SWING_1d", "1h") == "INTRADAY_1h"

    def test_swing_tf(self):
        assert sr._resolve_tf("SWING_1d", "1d_5y") == "SWING_1d"

    def test_missing_tf_falls_back(self):
        assert sr._resolve_tf("SWING_1d", "") == "SWING_1d"
        assert sr._resolve_tf("SWING_1d", None) == "SWING_1d"


class TestMatchDefGapDown:
    def _defs(self):
        # One gap-down def per rank with region placeholder ticker
        return [
            {"tf": "GAP_DOWN_1m", "rank": 997, "ticker": "INDIAN",
             "direction": "LONG", "factors": "f_gap_down< -0.5% + f_52wk_low"},
            {"tf": "GAP_DOWN_1m", "rank": 998, "ticker": "INDIAN",
             "direction": "LONG", "factors": "f_gap_down< -0.5%"},
        ]

    def test_rank997_matches_real_ticker(self):
        d = sr._match_def(self._defs(), "GAP_DOWN_1m", 997, "PFC.NS", "LONG", "")
        assert d is not None
        assert d["rank"] == 997

    def test_rank998_matches_real_ticker(self):
        d = sr._match_def(self._defs(), "GAP_DOWN_1m", 998, "ABFRL.NS", "LONG", "")
        assert d is not None
        assert d["rank"] == 998

    def test_wrong_direction_not_matched(self):
        d = sr._match_def(self._defs(), "GAP_DOWN_1m", 997, "PFC.NS", "SHORT", "")
        assert d is None

    def test_wrong_tf_not_matched(self):
        d = sr._match_def(self._defs(), "INTRADAY_1h", 997, "PFC.NS", "LONG", "")
        assert d is None


class TestRealDataZeroUnmatched:
    """End-to-end: every trade in the live CSV must resolve to a def."""

    def test_zero_unmatched_on_live_data(self):
        defs = sr._load_strategy_defs()
        trades = sr._load_trades()
        assert len(trades) > 0, "expected live paper_trades.csv to exist"

        unmatched = 0
        for t in trades:
            tf = str(t.get("TimeFrame", "SWING_1d"))
            try:
                rank = int(float(t.get("Pattern_Rank", 0)))
            except (ValueError, TypeError):
                rank = 0
            ticker = str(t.get("Ticker", ""))
            direction = str(t.get("Direction", "LONG")).upper()
            factors = str(t.get("Pattern_Factors", "")).strip()
            if sr._match_def(defs, tf, rank, ticker, direction, factors) is None:
                unmatched += 1
        assert unmatched == 0, f"{unmatched} trades still unmatched"

    def test_gapdown_defs_have_gapdown_tf(self):
        defs = sr._load_strategy_defs()
        gd = [d for d in defs if d["rank"] in (997, 998)]
        assert len(gd) >= 1, "expected at least one gap-down strategy def"
        for d in gd:
            assert d["tf"] == "GAP_DOWN_1m"
            assert d["ticker"] == "INDIAN"

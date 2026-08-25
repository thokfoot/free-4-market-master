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


class TestMatchDefIPO:
    def test_ipo_placeholder_matches_real_ticker(self):
        defs = [
            {"tf": "IPO_1d", "rank": 936, "ticker": "NSE",
             "direction": "LONG", "factors": "IPO DIP"},
        ]
        matched = sr._match_def(defs, "IPO_1d", 936, "KUSUMGAR.NS", "LONG", "IPO DIP: listing-high -10% dip")
        assert matched is not None
        assert matched["rank"] == 936


class TestRealDataZeroUnmatched:
    """End-to-end: every trade in the live CSV must resolve to a def."""

    # Strategies removed on 2026-08-18 (weak/negative independent backtest,
    # better variants for the same tickers already deployed). Their historical
    # trades can no longer match a def — that is expected.
    RETIRED = {
        ("SWING_1d", 46, "QQQ", "LONG"),
        ("INTRADAY_1h", 30, "IWM", "LONG"),
        ("INTRADAY_1h", 38, "XLF", "SHORT"),
        ("SWING_1d", 1, "XLC", "LONG"),
        ("SWING_1d", 76, "DIA", "LONG"),
        ("SWING_1d", 5, "XLY", "LONG"),
        ("IPO_1d", 936, "BLS.NS", "LONG"),
        ("FADE_1h", 900, "AVROIND.NS", "SHORT"),
        ("FADE_1h", 900, "CALSOFT.NS", "SHORT"),
        ("FADE_1h", 900, "KRN.NS", "SHORT"),
        ("FADE_1h", 900, "BLISSGVS.NS", "SHORT"),
        ("FADE_1h", 900, "NACLIND.NS", "SHORT"),
        ("FADE_1h", 900, "NETWEB.NS", "SHORT"),
        ("FADE_1h", 900, "MODISONLTD.NS", "SHORT"),
        ("FADE_1h", 900, "POCL.NS", "SHORT"),
        ("FADE_1h", 900, "RPEL.NS", "SHORT"),
        ("FADE_1h", 900, "IDEA.NS", "SHORT"),
        ("FADE_1h", 907, "KERNEX.NS", "SHORT"),
        ("FADE_1h", 991, "V1", "SHORT"),
        # ── v5.27 (2026-08-25): index strategies removed/ETF-swapped and
        # consistent losers dropped by etf_retest.py + deploy. Their
        # historical ledger trades can never match a current def.
        ("INTRADAY_1h", 16, "^GSPC", "LONG"),   # SP500 -> SPY swap (old fills)
        ("INTRADAY_1h", 6, "^NDX", "LONG"),     # Nasdaq100 -> QQQ swap
        ("SWING_1d", 49, "^NDX", "LONG"),
        ("SWING_1d", 45, "^NDX", "LONG"),
        ("SWING_1d", 64, "^NDX", "LONG"),
        ("SWING_1d", 31, "^NDX", "LONG"),
        ("INTRADAY_1h", 25, "^NDX", "LONG"),    # + consistent loser
        ("SWING_1d", 3, "^SOX", "LONG"),        # PHLX_Semi: SOXX transfer WEAK
        ("SWING_1d", 46, "^SOX", "LONG"),
        ("SWING_1d", 43, "^SOX", "LONG"),
        ("SWING_1d", 47, "^SOX", "LONG"),
        ("SWING_1d", 3, "^NYA", "LONG"),        # NYSE_Comp -> VTI swap
        ("SWING_1d", 2, "^DJT", "LONG"),        # Dow_Trans: no proxy
        ("INTRADAY_1h", 1, "TRX-USD", "SHORT"), # consistent losers (v5.27)
        ("INTRADAY_1h", 1, "BTC-USD", "SHORT"),
        ("INTRADAY_1h", 2, "LINK-USD", "SHORT"),
        ("SWING_1d", 41, "ADA-USD", "SHORT"),
        ("SWING_1d", 4, "XLI", "LONG"),
        ("SWING_1d", 5, "XLC", "LONG"),
        ("SWING_1d", 75, "XLK", "LONG"),
        ("SWING_1d", 7, "AVAX-USD", "LONG"),
    }

    def test_zero_unmatched_on_live_data(self):
        defs = sr._load_strategy_defs()
        trades = sr._load_trades()
        assert len(trades) > 0, "expected live paper_trades.csv to exist"

        unmatched = []
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
                if (tf, rank, ticker, direction) not in self.RETIRED:
                    unmatched.append((tf, rank, ticker, direction, factors[:30]))
        assert unmatched == [], f"{len(unmatched)} trades still unmatched: {unmatched}"

    def test_gapdown_defs_have_gapdown_tf(self):
        defs = sr._load_strategy_defs()
        gd = [d for d in defs if d["rank"] in (997, 998)]
        assert len(gd) >= 1, "expected at least one gap-down strategy def"
        for d in gd:
            assert d["tf"] == "GAP_DOWN_1m"
            assert d["ticker"] == "INDIAN"

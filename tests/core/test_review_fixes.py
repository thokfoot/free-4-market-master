"""
Regression tests for bugs found in the 2026-08-29 code review.

Covers:
    Bug 1 (HIGH): bot.run_gap_down_scan referenced GAP_DOWN_A/B_EXPECTED_WIN_RATE
                  which were NOT imported into bot.py -> every gap-down entry
                  raised NameError (swallowed) and gap-down trades never entered.
                  Fix: import both constants in bot.py.
                  Test: bot module exposes both names (import would fail before fix).

    Bug 2 (HIGH): config.get_region() misclassified every Indian .NS/.BO ticker
                  as "US" (only ^NSE/^BSESN prefixes mapped to INDIAN), so
                  scanner drop_incomplete_last_bar used the US daily-close time
                  for Indian stocks -> permanent 1-day lag on Indian swing bars.
                  Fix: .NS/.BO suffix -> INDIAN.
                  Test: get_region geometry for INDIAN/US/CRYPTO/index tickers.

    Bug 3 (perf): bot.run_swing_scan/run_intraday_scan recomputed _ohlc_bars for
                  every ticker on every outer iteration (O(N^2)). The loops were
                  flattened; behavior is preserved via the testable sentinel helper.

    Bug 4a (HIGH): the once-per-day swing-scan sentinel was saved even when the
                   full scan was entirely blocked by closed markets (e.g. the
                   pre-market 06:30 IST run), so the later post-open run was
                   skipped and Indian/US swing entries never fired.
                   Fix: only save the sentinel when the scan could actually fill
                   (bot._swing_scan_should_save_sentinel).
                   Test: helper returns False when all candidates are
                   MARKET_CLOSED-blocked, True otherwise.
"""

import bot
import config

from bot import _swing_scan_should_save_sentinel


# ======================================================================
# Bug 1: gap-down expected-win-rate constants reachable in bot namespace
# ======================================================================

def test_bot_exposes_gap_down_expected_win_rates():
    """Regression: bot.py must import GAP_DOWN_A/B_EXPECTED_WIN_RATE so
    run_gap_down_scan's entry branch no longer raises NameError.

    Before the fix the import was missing; importing bot still succeeded but
    `bot.GAP_DOWN_A_EXPECTED_WIN_RATE` raised AttributeError at scan time.
    """
    a = getattr(bot, "GAP_DOWN_A_EXPECTED_WIN_RATE", None)
    b = getattr(bot, "GAP_DOWN_B_EXPECTED_WIN_RATE", None)
    assert a is not None, "GAP_DOWN_A_EXPECTED_WIN_RATE not resolvable in bot namespace"
    assert b is not None, "GAP_DOWN_B_EXPECTED_WIN_RATE not resolvable in bot namespace"
    # Sanity: they are numeric win rates sourced from config
    assert isinstance(a, (int, float))
    assert isinstance(b, (int, float))


def test_gap_down_win_rates_match_config():
    """The values imported into bot must match config's canonical definitions."""
    assert bot.GAP_DOWN_A_EXPECTED_WIN_RATE == config.GAP_DOWN_A_EXPECTED_WIN_RATE
    assert bot.GAP_DOWN_B_EXPECTED_WIN_RATE == config.GAP_DOWN_B_EXPECTED_WIN_RATE


# ======================================================================
# Bug 2: get_region classifies Indian .NS/.BO tickers as INDIAN
# ======================================================================

def test_get_region_indian_ns_suffix():
    """Regression: Indian NSE tickers (.NS) must be region INDIAN, not US."""
    assert config.get_region("RELIANCE.NS") == "INDIAN"
    assert config.get_region("BANKBEES.NS") == "INDIAN"
    assert config.get_region("TATAMOTORS.NS") == "INDIAN"


def test_get_region_indian_bo_suffix():
    """BSE tickers (.BO) must likewise be INDIAN."""
    assert config.get_region("AXISBANK.BO") == "INDIAN"


def test_get_region_non_indian_unchanged():
    """US ETFs/stocks, crypto and Indian index prefixes stay unchanged."""
    assert config.get_region("QQQ") == "US"
    assert config.get_region("SPY") == "US"
    assert config.get_region("BTC-USD") == "CRYPTO"
    assert config.get_region("^NSEI") == "INDIAN"
    assert config.get_region("^GSPC") == "US"


# ======================================================================
# Bug 4a: swing-scan sentinel saved only when the scan could actually fill
# ======================================================================

def test_sentinel_saves_when_not_all_market_closed():
    """Normal case: at least one real (non-market-closed) outcome -> save."""
    # Scenario A: an entry succeeded
    assert _swing_scan_should_save_sentinel(
        best_entries=[{"ticker": "BTC-USD", "direction": "LONG"}],
        entries=[{"ticker": "BTC-USD"}],
        skipped_entries=[],  # a skipped-region note
    ) is True

    # Scenario B: candidates skipped for a NON-market reason (e.g. sizing)
    assert _swing_scan_should_save_sentinel(
        best_entries=[{"ticker": "QQQ"}],
        entries=[],
        skipped_entries=[{"reason": "Rejected (position sizing / unknown)"}],
    ) is True

    # Scenario C: no candidates at all -> nothing to fill, save as normal
    assert _swing_scan_should_save_sentinel(
        best_entries=[], entries=[], skipped_entries=[]
    ) is True


def test_sentinel_defers_when_all_market_closed():
    """Regression: a full scan where EVERY candidate was rejected purely
    because its market was closed must NOT consume the once-per-day sentinel,
    so a later post-open run can still scan and fill."""
    assert _swing_scan_should_save_sentinel(
        best_entries=[{"ticker": "RELIANCE.NS"}, {"ticker": "QQQ"}],
        entries=[],
        skipped_entries=[
            {"reason": "MARKET_CLOSED: INDIAN not tradable now (off-session)"},
            {"reason": "MARKET_CLOSED: US not tradable now (off-session)"},
        ],
    ) is False

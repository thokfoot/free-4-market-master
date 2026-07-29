"""
Tests for paper_trader.calculate_qty() — position sizing.

Covers:
    - All 3 markets: US, INDIAN, CRYPTO
    - All 2 timeframes: SWING_1d, INTRADAY_1h
    - All 2 directions: LONG and SHORT (qty is direction-independent)
    - Hard caps: entry<0.1 (50k), entry<1 (10k), entry>100 (5k)
    - Edge cases: zero SL distance, integer floor, fractional SL
    - Parametrized: 6 market+tf combinations

Uses existing fixtures from Phase 2A — no modifications to production code.
"""

import pytest
from paper_trader import calculate_qty


# ======================================================================
# Named test cases
# ======================================================================

def test_long_swing_us(isolated_fs):
    """LONG Swing US: SPY @ 500, SL 490 → standard risk sizing, no cap."""
    qty = calculate_qty(500.00, 490.00, "US", "SWING_1d")
    # risk_per_share=10, risk_amt=1000, qty=int(1000/10)=100
    # entry>100 → min(100,5000)=100, max(1,100)=100
    assert qty == 100


def test_long_swing_india(isolated_fs):
    """LONG Swing India: ^BSESN @ 2500, SL 2450 → higher rupee value, fewer shares."""
    qty = calculate_qty(2500.00, 2450.00, "INDIAN", "SWING_1d")
    # risk_per_share=50, risk_amt=1000, qty=int(1000/50)=20
    # entry>100 → min(20,5000)=20, max(1,20)=20
    assert qty == 20


def test_long_swing_crypto_low_price(isolated_fs):
    """LONG Swing Crypto: ADA @ 0.16, SL 0.1568 → low price, high qty, capped."""
    qty = calculate_qty(0.16, 0.1568, "CRYPTO", "SWING_1d")
    # risk_per_share=0.0032, risk_amt=1000, qty=int(1000/0.0032)=312500
    # entry<1 → min(312500,10000)=10000, max(1,10000)=10000
    assert qty == 10000


def test_short_swing_us(isolated_fs):
    """SHORT Swing US: QQQ @ 100, SL 102 → qty independent of direction."""
    qty = calculate_qty(100.00, 102.00, "US", "SWING_1d")
    # risk_per_share=2, risk_amt=1000, qty=int(1000/2)=500
    # entry=100 → no cap applies (not <0.1, not <1, not >100)
    # max(1,500)=500
    assert qty == 500


def test_short_swing_crypto_capped(isolated_fs):
    """SHORT Swing Crypto: ADA @ 0.50, SL 0.51 → capped under entry<1 rule."""
    qty = calculate_qty(0.50, 0.51, "CRYPTO", "SWING_1d")
    # risk_per_share=0.01, risk_amt=1000, qty=int(1000/0.01)=100000
    # entry<1 → min(100000,10000)=10000, max(1,10000)=10000
    assert qty == 10000


def test_long_intraday_us(isolated_fs):
    """LONG Intraday US: uses INTRADAY capital pool (separate from US)."""
    qty = calculate_qty(200.00, 198.00, "US", "INTRADAY_1h")
    # tf=INTRADAY_1h → uses INTRADAY capital (100000)
    # risk_per_share=2, risk_amt=1000, qty=int(1000/2)=500
    # entry>100 → min(500,5000)=500, max(1,500)=500
    assert qty == 500


def test_short_intraday_crypto(isolated_fs):
    """SHORT Intraday Crypto: no hard cap applies at entry=1.50."""
    qty = calculate_qty(1.50, 1.53, "CRYPTO", "INTRADAY_1h")
    # risk_per_share=0.03, risk_amt=1000, qty=int(1000/0.03)=33333
    # entry=1.50 → not <0.1, not <1, not >100 → no cap
    # max(1,33333)=33333
    assert qty == 33333


def test_zero_risk_distance(isolated_fs):
    """SL equals Entry → risk_per_share=0 → return 0 (no trade)."""
    qty = calculate_qty(100.00, 100.00, "US", "SWING_1d")
    assert qty == 0


def test_high_price_cap(isolated_fs):
    """Entry > 100 → max qty capped at 5,000."""
    qty = calculate_qty(1000.00, 990.00, "US", "SWING_1d")
    # risk_per_share=10, risk_amt=1000, qty=int(1000/10)=100
    # entry>100 → min(100,5000)=100
    assert qty == 100


def test_low_price_cap_under_01(isolated_fs):
    """Entry < 0.1 → max qty capped at 50,000."""
    qty = calculate_qty(0.05, 0.049, "CRYPTO", "SWING_1d")
    # risk_per_share=0.001, risk_amt=1000, qty=int(1000/0.001)=1000000
    # entry<0.1 → min(1000000,50000)=50000
    assert qty == 50000


def test_mid_price_cap_under_1(isolated_fs):
    """Entry < 1 → max qty capped at 10,000."""
    qty = calculate_qty(0.80, 0.79, "US", "SWING_1d")
    # risk_per_share=0.01, risk_amt=1000, qty=int(1000/0.01)=100000
    # entry<1 → min(100000,10000)=10000
    assert qty == 10000


def test_exact_sl_distance(isolated_fs):
    """Exact ₹1 risk-per-share → qty equals risk_amt exactly."""
    qty = calculate_qty(100.00, 99.00, "US", "SWING_1d")
    # risk_per_share=1, risk_amt=1000, qty=int(1000/1)=1000
    # entry=100 → no cap applies
    assert qty == 1000


def test_fractional_sl_distance(isolated_fs):
    """Fractional SL distance (₹0.50) → qty doubles."""
    qty = calculate_qty(50.00, 49.50, "US", "SWING_1d")
    # risk_per_share=0.50, risk_amt=1000, qty=int(1000/0.50)=2000
    # entry=50 → no cap applies
    assert qty == 2000


def test_minimum_qty_floor(isolated_fs):
    """When risk_per_share > risk_amt → int division = 0 → floor(1) kicks in."""
    qty = calculate_qty(100.00, 1200.00, "US", "SWING_1d")
    # risk_per_share=1100, risk_amt=1000, qty=int(1000/1100)=0
    # entry=100 → no cap applies
    # max(1,0) = 1
    assert qty == 1


# ======================================================================
# Parametrized tests — consolidated
# ======================================================================

@pytest.mark.parametrize("entry,sl,market,tf,expected", [
    # Group 1: Standard swing — no caps apply (entry between 1 and 100)
    (100.00, 99.00,     "US",      "SWING_1d",      1000),   # standard
    (75.00,  73.50,     "US",      "SWING_1d",      666),    # fractional to test int()
    (25.00,  24.00,     "US",      "SWING_1d",      1000),   # round number

    # Group 2: All 6 market+tf combinations
    (500.00, 490.00,    "US",      "SWING_1d",      100),    # US swing
    (2500.00,2450.00,   "INDIAN",  "SWING_1d",      20),     # India swing
    (1.50,   1.47,      "CRYPTO",  "SWING_1d",      33333),  # Crypto swing
    (200.00, 198.00,    "US",      "INTRADAY_1h",   500),    # US intraday
    (1.50,   1.53,      "CRYPTO",  "INTRADAY_1h",   33333),  # Crypto intraday
    (2500.00,2475.00,   "INDIAN",  "INTRADAY_1h",   40),     # India intraday

    # Group 3: Cap boundaries — entry<0.1 → 50000 cap (using FP-safe values)
    (0.05,   0.04,      "CRYPTO",  "SWING_1d",      50000),
])
def test_calculate_qty_parametrized(isolated_fs, entry, sl, market, tf, expected):
    """Parametrized test covering standard, market+tf combos, and cap boundaries."""
    qty = calculate_qty(entry, sl, market, tf)
    assert qty == expected, (
        f"calculate_qty({entry}, {sl}, {market}, {tf}): "
        f"expected {expected}, got {qty}"
    )

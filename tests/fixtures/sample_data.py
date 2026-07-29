"""
Frozen OHLC data fixtures for deterministic SL/TP testing.

All data is static Python dicts — no CSV files, no yfinance calls.
Each fixture represents realistic intraday OHLC data for a known ticker.

Usage:
    from tests.fixtures.sample_data import ohlc_normal_range, ohlc_sl_hit_intraday
    ohlc = ohlc_sl_hit_intraday()
"""

from copy import deepcopy


# ======================================================================
# SL/TP Test Data (for update_trades tests)
# Each returns: {"close": float, "high": float, "low": float}
# ======================================================================

def ohlc_normal_range(**overrides) -> dict:
    """Normal trading range — no SL/TP hit. All prices between SL and TP."""
    return {
        "close": 448.00,  # Between SL(441) and TP(468)
        "high": 452.00,   # Well below TP(468)
        "low": 443.00,    # Well above SL(441)
        **overrides,
    }


def ohlc_sl_hit_intraday(**overrides) -> dict:
    """LONG SL hit intraday — Low breached SL, Close recovered above SL."""
    return {
        "close": 445.00,  # Recovered above SL
        "high": 453.00,
        "low": 440.50,    # Below SL(441) — stop triggered!
        **overrides,
    }


def ohlc_sl_hit_close(**overrides) -> dict:
    """LONG SL hit on close — Close is below SL, Low above SL."""
    return {
        "close": 440.50,  # Below SL(441)
        "high": 450.00,
        "low": 442.00,    # Never hit SL intraday
        **overrides,
    }


def ohlc_tp_hit_intraday(**overrides) -> dict:
    """LONG TP hit intraday — High breached TP, Close below TP."""
    return {
        "close": 465.00,  # Below TP(468)
        "high": 469.00,   # Above TP(468) — target reached!
        "low": 446.00,
        **overrides,
    }


def ohlc_tp_hit_close(**overrides) -> dict:
    """LONG TP hit on close — Close is above TP, High below TP."""
    return {
        "close": 469.00,  # Above TP(468)
        "high": 467.50,   # Never hit TP intraday
        "low": 445.00,
        **overrides,
    }


def ohlc_short_sl_hit_intraday(**overrides) -> dict:
    """SHORT SL hit intraday — High breached SL, Close below SL."""
    return {
        "close": 505.00,   # Below SL(510)
        "high": 511.00,    # Above SL(510) — stopped out!
        "low": 502.00,
        **overrides,
    }


def ohlc_short_tp_hit_intraday(**overrides) -> dict:
    """SHORT TP hit intraday — Low breached TP, Close above TP."""
    return {
        "close": 485.00,   # Above TP(480)
        "high": 492.00,
        "low": 479.00,     # Below TP(480) — target reached!
        **overrides,
    }


# ======================================================================
# Edge Case / Corrupt Data Fixtures
# ======================================================================

def ohlc_nan_low(**overrides) -> dict:
    """Low is NaN — should be rejected by OHLC validation."""
    return {
        "close": 448.00,
        "high": 452.00,
        "low": float("nan"),
        **overrides,
    }


def ohlc_nan_high(**overrides) -> dict:
    """High is NaN."""
    return {
        "close": 448.00,
        "high": float("nan"),
        "low": 443.00,
        **overrides,
    }


def ohlc_nan_close(**overrides) -> dict:
    """Close is NaN."""
    return {
        "close": float("nan"),
        "high": 452.00,
        "low": 443.00,
        **overrides,
    }


def ohlc_zero_low(**overrides) -> dict:
    """Low is 0 — should be rejected."""
    return {
        "close": 448.00,
        "high": 452.00,
        "low": 0.0,
        **overrides,
    }


def ohlc_inf_high(**overrides) -> dict:
    """High is infinity — should be rejected."""
    return {
        "close": 448.00,
        "high": float("inf"),
        "low": 443.00,
        **overrides,
    }


def ohlc_none_close(**overrides) -> dict:
    """Close is None — should be rejected."""
    return {
        "close": None,
        "high": 452.00,
        "low": 443.00,
        **overrides,
    }


def ohlc_tolerance_boundary_inside(**overrides) -> dict:
    """
    Low is within 0.01% tolerance boundary (above 0.9999 multiplier).
    Should NOT trigger SL exit — guards against 1-cent data noise.
    SL = 441.00, tolerance = 0.9999, guard threshold = 441 * 0.9999 = 440.9559
    Low = 440.96 → inside tolerance → NO EXIT
    """
    return {
        "close": 445.00,
        "high": 452.00,
        "low": 440.96,  # Above guard threshold (440.9559)
        **overrides,
    }


def ohlc_tolerance_boundary_outside(**overrides) -> dict:
    """
    Low is beyond 0.01% tolerance boundary (below 0.9999 multiplier).
    Should trigger SL exit.
    SL = 441.00, guard threshold = 440.9559
    Low = 440.95 → outside tolerance → EXIT
    """
    return {
        "close": 445.00,
        "high": 452.00,
        "low": 440.95,  # Below guard threshold (440.9559)
        **overrides,
    }


# ======================================================================
# Full ohlc_data dict factory (for update_trades)
# ======================================================================

def build_ohlc_data(ticker: str = "SPY",
                    ohlc_func=ohlc_normal_range) -> dict:
    """
    Build the ohlc_data dict as expected by update_trades():

        {ticker: {"close": c, "high": h, "low": l}}

    Args:
        ticker: yfinance ticker symbol
        ohlc_func: One of the ohlc_* functions above

    Returns:
        dict suitable for update_trades(ohlc_data=...)
    """
    return {ticker: ohlc_func()}

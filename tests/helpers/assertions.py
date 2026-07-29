"""
Custom assertion helpers for the FREE 3-Market paper trade test suite.

Provides:
    - assert_trade_equal: Compare expected vs actual trade dicts
    - assert_portfolio_equals: Compare expected vs actual portfolio dicts
    - assert_valid_trade_columns: Verify a trade dict has all required fields
    - assert_valid_ohlc: Verify OHLC data passes validation
    - assert_invalid_ohlc: Verify OHLC data is rejected
"""

from typing import Any, Dict, List


def assert_trade_equal(expected: Dict[str, Any], actual: Dict[str, Any],
                       msg: str = "", ignore_keys: List[str] = None):
    """
    Assert that two trade dicts match for all non-ignored keys.

    Args:
        expected: Expected trade dict
        actual: Actual trade dict
        msg: Optional failure message
        ignore_keys: Keys to skip during comparison (e.g. ["Time_IST"])
    """
    ignore_keys = set(ignore_keys or [])
    
    for key in expected:
        if key in ignore_keys:
            continue
        if key not in actual:
            raise AssertionError(
                f"{msg} Key '{key}' missing in actual trade. "
                f"Expected keys: {list(expected.keys())}, "
                f"Actual keys: {list(actual.keys())}"
            )
        exp_val = expected[key]
        act_val = actual[key]
        
        # Convert numeric strings to floats for comparison where appropriate
        if isinstance(exp_val, str) and isinstance(act_val, str):
            if exp_val == act_val:
                continue
            # Try numeric comparison for strings that look like numbers
            try:
                if float(exp_val) == float(act_val):
                    continue
            except (ValueError, TypeError):
                pass
        
        if exp_val != act_val:
            raise AssertionError(
                f"{msg} Key '{key}' mismatch: "
                f"expected {exp_val!r} (type {type(exp_val).__name__}), "
                f"actual {act_val!r} (type {type(act_val).__name__})"
            )


def assert_portfolio_equals(expected: Dict[str, Any], actual: Dict[str, Any],
                             msg: str = ""):
    """
    Assert that two portfolio dicts match.

    Checks capital_by_market, open_positions count, and key stats.
    """
    # Check capital
    exp_cap = expected.get("capital_by_market", {})
    act_cap = actual.get("capital_by_market", {})
    for mkt in ["INDIAN", "US", "CRYPTO", "INTRADAY"]:
        e = exp_cap.get(mkt, 0)
        a = act_cap.get(mkt, 0)
        if abs(e - a) > 0.01:
            raise AssertionError(
                f"{msg} Capital mismatch for {mkt}: expected {e}, actual {a}"
            )
    
    # Check open positions count
    e_open = len(expected.get("open_positions", []))
    a_open = len(actual.get("open_positions", []))
    if e_open != a_open:
        raise AssertionError(
            f"{msg} Open positions count mismatch: expected {e_open}, actual {a_open}"
        )
    
    # Check stats
    for key in ["closed_count", "total_wins", "total_losses", "total_pnl"]:
        e = expected.get(key, 0)
        a = actual.get(key, 0)
        if abs(e - a) > 0.01:
            raise AssertionError(
                f"{msg} Portfolio '{key}' mismatch: expected {e}, actual {a}"
            )


_COLUMNS = [
    "Date", "Time_IST", "Mode", "Ticker", "Direction", "TimeFrame",
    "Entry_Price", "Qty", "SL", "Target", "MaxHold",
    "Exit_Price", "Exit_Time", "P&L", "P&L_%", "Status",
    "Pattern_Rank", "Expected_WinRate", "Pattern_Factors", "Reason",
]


def assert_valid_trade_columns(trade: Dict[str, Any], msg: str = ""):
    """
    Assert that a trade dict has all required columns.
    
    This does NOT check values — only key presence.
    """
    for col in _COLUMNS:
        if col not in trade:
            raise AssertionError(
                f"{msg} Missing required column '{col}' in trade dict. "
                f"Actual keys: {list(trade.keys())}"
            )


def assert_valid_ohlc(ohlc: dict, msg: str = ""):
    """
    Assert that OHLC data is valid (passes the _invalid_ohlc check).
    """
    close = ohlc.get("close")
    high = ohlc.get("high")
    low = ohlc.get("low")
    
    import math
    
    invalid = (
        low is None or high is None or close is None
        or not math.isfinite(low)
        or not math.isfinite(high)
        or not math.isfinite(close)
        or low <= 0 or high <= 0 or close <= 0
    )
    if invalid:
        raise AssertionError(
            f"{msg} OHLC data unexpectedly invalid: "
            f"close={close}, high={high}, low={low}"
        )


def assert_invalid_ohlc(ohlc: dict, msg: str = ""):
    """
    Assert that OHLC data is INVALID (should be rejected by _invalid_ohlc check).
    """
    close = ohlc.get("close")
    high = ohlc.get("high")
    low = ohlc.get("low")
    
    import math
    
    invalid = (
        low is None or high is None or close is None
        or not math.isfinite(low)
        or not math.isfinite(high)
        or not math.isfinite(close)
        or low <= 0 or high <= 0 or close <= 0
    )
    if not invalid:
        raise AssertionError(
            f"{msg} OHLC data unexpectedly VALID: "
            f"close={close}, high={high}, low={low}"
        )

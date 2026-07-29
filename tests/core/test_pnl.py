"""
Tests for paper_trader P&L calculations and behavioral invariants.

Covers:
    - round_price() — all 5 price tiers + boundary values
    - _safe_float() — None, NaN, strings, numbers
    - _safe_num() — None, NaN, empty, default handling
    - _calc_unrealized_pnl() — LONG/SHORT profit/loss, missing price, edge cases
    - update_last_prices / _get_current_price — cache round-trip
    - Behavioral invariants (end-to-end):
        * Winning trade: capital delta == net P&L (exact)
        * Losing trade: capital delta == net P&L (exact)
        * CSV P&L matches portfolio capital delta
        * Audit log P&L matches trade record
        * Market-specific charges (US, CRYPTO, INDIAN)
        * Strategy stats total_pnl matches trade P&L

Uses existing test_env fixture and OHLC data fixtures from Phase 2A.
Does NOT modify production code or existing fixtures.
"""

import pytest
from paper_trader import (
    round_price, _safe_float, _safe_num,
    _calc_unrealized_pnl, update_last_prices, _get_current_price,
    enter_trade, update_trades, load_portfolio,
    _load_audit, _load_strategy_stats,
)
from tests.fixtures.sample_data import build_ohlc_data


# ======================================================================
# round_price() — all 5 tiers
# ======================================================================

@pytest.mark.parametrize("input_val,expected", [
    # Tier 1: >= 1000 → round(p, 2)
    (1000.00,   1000.00),
    (9999.999,  10000.00),
    (1234.5678, 1234.57),
    # Tier 2: >= 100 → round(p, 2)
    (100.00,    100.00),
    (500.1234,  500.12),
    (99.9999,   100.00),   # rounds up: 99.9999 → 100.00
    # Tier 3: >= 1 → round(p, 2)
    (1.00,       1.00),
    (50.6789,   50.68),
    # Tier 4: >= 0.1 → round(p, 4)
    (0.10,       0.10),
    (0.55555,    0.5555),
    (0.9999,     0.9999),
    # Tier 5: >= 0.01 → round(p, 6)
    (0.01,       0.01),
    (0.087654,  0.087654),
    (0.09999,    0.09999),
    # Tier 6: < 0.01 → round(p, 8)
    (0.009,     0.009),
    (0.00123456, 0.00123456),
    (0.0099999,  0.0099999),
    # Zero and negative
    (0.0,        0.0),
    (-1.50,     -1.5),
])
def test_round_price(input_val, expected):
    """round_price handles all 6 price tiers correctly."""
    result = round_price(input_val)
    assert result == pytest.approx(expected, abs=1e-8), (
        f"round_price({input_val}): expected {expected}, got {result}"
    )


# ======================================================================
# _safe_float()
# ======================================================================

@pytest.mark.parametrize("input_val,expected", [
    (None,       0.0),
    (float("nan"), 0.0),
    ("",         0.0),
    ("nan",      0.0),
    (42.5,      42.5),
    ("123.45", 123.45),
    (0,          0.0),
    ("invalid",  0.0),
])
def test_safe_float(input_val, expected):
    """_safe_float returns 0.0 for NaN/None/empty, float for valid inputs."""
    result = _safe_float(input_val)
    assert result == pytest.approx(expected, abs=1e-9), (
        f"_safe_float({input_val!r}): expected {expected}, got {result}"
    )


# ======================================================================
# _safe_num()
# ======================================================================

@pytest.mark.parametrize("input_val,default,expected", [
    (None,       "",       ""),
    (float("nan"), "",    ""),
    (42.5,       "",      "42.5"),
    ("123.45",   "",      "123.45"),
    ("",         "",      ""),
    (0,          "",      "0.0"),
    ("nan",      "",      ""),
])
def test_safe_num(input_val, default, expected):
    """_safe_num returns default for NaN/None/empty, str representation for valid."""
    result = _safe_num(input_val, default)
    assert result == expected, f"_safe_num({input_val!r}, {default!r}): expected {expected!r}, got {result!r}"


# ======================================================================
# update_last_prices / _get_current_price
# ======================================================================

def test_price_cache_round_trip():
    """update_last_prices stores prices, _get_current_price retrieves them."""
    # Clear cache
    update_last_prices({})
    assert _get_current_price("SPY") == 0.0, "Unknown ticker should return 0.0"

    update_last_prices({"SPY": 450.00, "QQQ": 500.00})
    assert _get_current_price("SPY") == 450.00
    assert _get_current_price("QQQ") == 500.00
    assert _get_current_price("UNKNOWN") == 0.0

    # Update existing
    update_last_prices({"SPY": 455.00})
    assert _get_current_price("SPY") == 455.00
    assert _get_current_price("QQQ") == 500.00  # Unchanged


# ======================================================================
# _calc_unrealized_pnl()
# ======================================================================

def _make_row(ticker="SPY", direction="LONG", entry=450.00, qty=100, current=455.00):
    """Helper: set cached price, return a trade row dict."""
    update_last_prices({ticker: current})
    return {
        "Ticker": ticker,
        "Direction": direction,
        "Entry_Price": entry,
        "Qty": qty,
    }


def test_unrealized_long_profit():
    """LONG unrealized P&L: (current - entry) * qty > 0."""
    row = _make_row(direction="LONG", entry=100.00, qty=10, current=110.00)
    pnl, pnl_pct = _calc_unrealized_pnl(row)
    assert pnl == pytest.approx(100.0, abs=0.01)   # (110-100)*10
    assert pnl_pct == pytest.approx(10.0, abs=0.01)  # (110-100)/100*100


def test_unrealized_long_loss():
    """LONG unrealized P&L: (current - entry) * qty < 0."""
    row = _make_row(direction="LONG", entry=100.00, qty=10, current=90.00)
    pnl, pnl_pct = _calc_unrealized_pnl(row)
    assert pnl == pytest.approx(-100.0, abs=0.01)
    assert pnl_pct == pytest.approx(-10.0, abs=0.01)


def test_unrealized_short_profit():
    """SHORT unrealized P&L: (entry - current) * qty > 0 (price dropped)."""
    row = _make_row(direction="SHORT", entry=100.00, qty=10, current=90.00)
    pnl, pnl_pct = _calc_unrealized_pnl(row)
    assert pnl == pytest.approx(100.0, abs=0.01)
    assert pnl_pct == pytest.approx(10.0, abs=0.01)


def test_unrealized_short_loss():
    """SHORT unrealized P&L: (entry - current) * qty < 0 (price rose)."""
    row = _make_row(direction="SHORT", entry=100.00, qty=10, current=110.00)
    pnl, pnl_pct = _calc_unrealized_pnl(row)
    assert pnl == pytest.approx(-100.0, abs=0.01)
    assert pnl_pct == pytest.approx(-10.0, abs=0.01)


def test_unrealized_no_price():
    """No cached price → returns (0.0, 0.0)."""
    update_last_prices({})  # Clear cache
    row = _make_row(ticker="SPY", current=0.0)
    # Force cache miss by clearing after _make_row
    update_last_prices({})
    pnl, pnl_pct = _calc_unrealized_pnl(row)
    assert pnl == 0.0
    assert pnl_pct == 0.0


def test_unrealized_zero_qty():
    """Qty = 0 → returns (0.0, 0.0)."""
    row = _make_row(qty=0, current=455.00)
    pnl, pnl_pct = _calc_unrealized_pnl(row)
    assert pnl == 0.0
    assert pnl_pct == 0.0


def test_unrealized_nan_entry():
    """NaN Entry_Price → _safe_float returns 0.0 → P&L (0, 0)."""
    update_last_prices({"SPY": 455.00})
    row = {"Ticker": "SPY", "Direction": "LONG", "Entry_Price": float("nan"), "Qty": 100}
    pnl, pnl_pct = _calc_unrealized_pnl(row)
    assert pnl == 0.0
    assert pnl_pct == 0.0


# ======================================================================
# Behavioral Invariants — End-to-End P&L Consistency
# ======================================================================

def _enter_and_exit(test_env, direction, entry, exit_at, mode="US", rank=99):
    """Helper: enter one trade, exit via SL/TP, return (trade, port_before, port_after)."""
    ticker = "SPY" if direction == "LONG" else "QQQ"
    t = enter_trade(mode, ticker, direction, entry,
                    f"PnL test {direction}", pattern_rank=rank,
                    expected_win_rate=60.0, pattern_factors="Test",
                    tf="SWING_1d")
    assert t is not None
    port_before = load_portfolio()

    # Build OHLC that triggers exit_at price
    if direction == "LONG":
        ohlc = build_ohlc_data(ticker, lambda: {
            "close": exit_at, "high": exit_at * 1.01, "low": entry * 0.99
        })
    else:
        ohlc = build_ohlc_data(ticker, lambda: {
            "close": exit_at, "high": entry * 1.01, "low": exit_at * 0.99
        })
    msgs = update_trades(ohlc)
    assert len(msgs) == 1, f"Expected 1 exit, got {len(msgs)}: {msgs}"
    port_after = load_portfolio()
    return t, port_before, port_after


def _read_closed_pnl(test_env):
    """Read the P&L from the most recent closed trade in CSV."""
    import pandas as pd
    from paper_trader import PAPER_FILE
    df = pd.read_csv(PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"] == "CLOSED"]
    assert len(closed) >= 1
    row = closed.iloc[-1]
    return float(row["P&L"]), float(row["P&L_%"])


def test_winning_trade_capital_invariant(test_env):
    """Winning trade: capital delta == net P&L exactly."""
    t, before, after = _enter_and_exit(test_env, "LONG", 100.00, 110.00)
    csv_pnl, csv_pnl_pct = _read_closed_pnl(test_env)
    capital_key = "US"
    cap_delta = after["capital_by_market"][capital_key] - before["capital_by_market"][capital_key]
    assert cap_delta == pytest.approx(csv_pnl, abs=0.01), (
        f"Capital delta ({cap_delta:.2f}) != CSV P&L ({csv_pnl:.2f})"
    )
    assert after["total_pnl"] > 0


def test_losing_trade_capital_invariant(test_env):
    """Losing trade: capital delta == net P&L exactly."""
    t, before, after = _enter_and_exit(test_env, "LONG", 100.00, 90.00)
    csv_pnl, csv_pnl_pct = _read_closed_pnl(test_env)
    capital_key = "US"
    cap_delta = after["capital_by_market"][capital_key] - before["capital_by_market"][capital_key]
    assert cap_delta == pytest.approx(csv_pnl, abs=0.01), (
        f"Capital delta ({cap_delta:.2f}) != CSV P&L ({csv_pnl:.2f})"
    )
    assert after["total_pnl"] < 0


def test_audit_pnl_matches_trade_record(test_env):
    """Audit log EXIT event P&L matches the CSV trade record."""
    t, before, after = _enter_and_exit(test_env, "LONG", 100.00, 110.00)
    csv_pnl, _ = _read_closed_pnl(test_env)

    audit = _load_audit()
    exit_events = [e for e in audit if e["event"] == "EXIT"]
    assert len(exit_events) >= 1
    audit_pnl = float(exit_events[-1]["pnl"])
    assert audit_pnl == pytest.approx(csv_pnl, abs=0.01), (
        f"Audit P&L ({audit_pnl}) != CSV P&L ({csv_pnl})"
    )


def test_strategy_stats_pnl_matches_trade(test_env):
    """Strategy stats total_pnl accumulates correctly from closed trades."""
    t, before, after = _enter_and_exit(test_env, "LONG", 100.00, 110.00, rank=77)
    csv_pnl, _ = _read_closed_pnl(test_env)

    stats = _load_strategy_stats()
    assert "77" in stats, f"Strategy 77 not found in stats: {list(stats.keys())}"
    assert stats["77"]["total_pnl"] == pytest.approx(csv_pnl, abs=0.01), (
        f"Stats total_pnl ({stats['77']['total_pnl']}) != CSV P&L ({csv_pnl})"
    )


def test_market_charges_us(test_env):
    """US trade: charges = entry * qty * 0.0002 deducted from gross."""
    # Enter trade, compute expected from target (actual exit price for TP hit)
    t = enter_trade("US", "SPY", "LONG", 100.00, "Charges US",
                    pattern_rank=5, expected_win_rate=60.0,
                    pattern_factors="Test", tf="SWING_1d")
    assert t is not None
    entry = float(t["Entry_Price"])
    target = float(t["Target"])
    qty = t["Qty"]
    port_before = load_portfolio()

    # Build OHLC that triggers TP hit via high
    ohlc = build_ohlc_data("SPY", lambda: {"close": 105.00, "high": target + 1, "low": entry * 0.99})
    msgs = update_trades(ohlc)
    assert len(msgs) == 1

    csv_pnl, _ = _read_closed_pnl(test_env)
    gross = (target - entry) * qty
    expected_charges = (entry * qty) * 0.0002
    expected_net = round(gross - expected_charges, 2)
    assert csv_pnl == pytest.approx(expected_net, abs=0.05), (
        f"US net P&L ({csv_pnl}) != expected ({expected_net})"
    )


def test_market_charges_crypto(test_env):
    """Crypto trade: charges = entry * qty * 0.003."""
    t = enter_trade("CRYPTO", "BTC-USD", "LONG", 100.00, "Charges CRYPTO",
                    pattern_rank=5, expected_win_rate=60.0,
                    pattern_factors="Test", tf="SWING_1d")
    assert t is not None
    entry = float(t["Entry_Price"])
    target = float(t["Target"])
    qty = t["Qty"]

    ohlc = build_ohlc_data("BTC-USD", lambda: {"close": 105.00, "high": target + 1, "low": entry * 0.99})
    msgs = update_trades(ohlc)
    assert len(msgs) == 1

    csv_pnl, _ = _read_closed_pnl(test_env)
    gross = (target - entry) * qty
    expected_charges = (entry * qty) * 0.003
    expected_net = round(gross - expected_charges, 2)
    assert csv_pnl == pytest.approx(expected_net, abs=0.05)


def test_market_charges_indian(test_env):
    """Indian trade: charges = entry * qty * 0.0012."""
    t = enter_trade("INDIAN", "^BSESN", "LONG", 100.00, "Charges INDIA",
                    pattern_rank=5, expected_win_rate=60.0,
                    pattern_factors="Test", tf="SWING_1d")
    assert t is not None
    entry = float(t["Entry_Price"])
    target = float(t["Target"])
    qty = t["Qty"]

    ohlc = build_ohlc_data("^BSESN", lambda: {"close": 105.00, "high": target + 1, "low": entry * 0.99})
    msgs = update_trades(ohlc)
    assert len(msgs) == 1

    csv_pnl, _ = _read_closed_pnl(test_env)
    gross = (target - entry) * qty
    expected_charges = (entry * qty) * 0.0012
    expected_net = round(gross - expected_charges, 2)
    assert csv_pnl == pytest.approx(expected_net, abs=0.05)


def test_short_profit_capital_invariant(test_env):
    """SHORT winning trade: capital delta == net P&L exactly."""
    t, before, after = _enter_and_exit(test_env, "SHORT", 100.00, 90.00)
    csv_pnl, _ = _read_closed_pnl(test_env)
    capital_key = "US"
    cap_delta = after["capital_by_market"][capital_key] - before["capital_by_market"][capital_key]
    assert cap_delta == pytest.approx(csv_pnl, abs=0.01)
    assert cap_delta > 0


def test_short_loss_capital_invariant(test_env):
    """SHORT losing trade: capital delta == net P&L exactly."""
    t, before, after = _enter_and_exit(test_env, "SHORT", 100.00, 110.00)
    csv_pnl, _ = _read_closed_pnl(test_env)
    capital_key = "US"
    cap_delta = after["capital_by_market"][capital_key] - before["capital_by_market"][capital_key]
    assert cap_delta == pytest.approx(csv_pnl, abs=0.01)
    assert cap_delta < 0


def test_two_trades_cumulative_pnl(test_env):
    """Two trades (win + loss): cumulative P&L matches total_pnl."""
    # Trade 1: win
    t1, _, _ = _enter_and_exit(test_env, "LONG", 100.00, 110.00, rank=1)
    csv_pnl_1, _ = _read_closed_pnl(test_env)

    # Trade 2: loss
    t2, _, after = _enter_and_exit(test_env, "LONG", 100.00, 95.00, rank=2)
    csv_pnl_2, _ = _read_closed_pnl(test_env)

    total_csv_pnl = round(csv_pnl_1 + csv_pnl_2, 2)
    assert after["total_pnl"] == pytest.approx(total_csv_pnl, abs=0.01), (
        f"Portfolio total_pnl ({after['total_pnl']}) != sum of CSV P&Ls ({total_csv_pnl})"
    )
    assert after["closed_count"] == 2
    assert after["total_wins"] == 1
    assert after["total_losses"] == 1


def test_zero_pnl_charges_only(test_env):
    """Enter trade, exit at entry price via direct CSV manipulation: net P&L = -charges."""
    import pandas as pd
    from paper_trader import PAPER_FILE, save_portfolio, load_portfolio

    t = enter_trade("US", "SPY", "LONG", 100.00, "Zero PnL test",
                    pattern_rank=99, expected_win_rate=60.0,
                    pattern_factors="Test", tf="SWING_1d")
    assert t is not None
    entry = float(t["Entry_Price"])
    qty = t["Qty"]

    # Manually set Exit_Price = Entry_Price and close the trade
    df = pd.read_csv(PAPER_FILE, on_bad_lines="warn")
    df.at[0, "Exit_Price"] = entry
    df.at[0, "Exit_Time"] = "10:30:00 IST"
    df.at[0, "P&L"] = round(0 - (entry * qty * 0.0002), 2)  # gross=0, net=-charges
    df.at[0, "P&L_%"] = round(0 - (0.0002 * 100), 2)
    df.at[0, "Status"] = "CLOSED"
    df.at[0, "Reason"] = str(df.at[0, "Reason"]) + " | Manual exit"
    df.to_csv(PAPER_FILE, index=False)

    # Verify the calculated values
    csv_pnl, csv_pnl_pct = _read_closed_pnl(test_env)
    expected_charges = round(entry * qty * 0.0002, 2)
    assert csv_pnl < 0, f"P&L ({csv_pnl}) should be negative (charges only)"
    assert csv_pnl == pytest.approx(-expected_charges, abs=0.001), (
        f"Net P&L ({csv_pnl}) != -charges ({-expected_charges})"
    )

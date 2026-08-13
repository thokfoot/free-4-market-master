"""
LONG-BOUNCE (v5.19) paper trade tests.

Covers:
    1. enter_trade with tf=LONG_BOUNCE_5m — LONG SL below / TP above entry
    2. LONG_BOUNCE bucket gets its own ₹1L capital
    3. update_trades LONG SL hit / TP hit close the position and update the
       LONG_BOUNCE bucket
    4. Telegram section mapping: LONG_BOUNCE_5m -> "LONG" section, count = 1
"""

import pandas as pd
import pytest

import paper_trader as pt
from paper_trader import enter_trade, update_trades, load_portfolio
from tests.fixtures.sample_data import build_ohlc_data


def test_long_bounce_enter_direction_sl_tp(test_env):
    """LONG entry: SL below, TP above, own bucket capital."""
    t = enter_trade("INDIAN", "TESTLB.NS", "LONG", 50.00,
                    "Long Bounce test",
                    pattern_rank=935, expected_win_rate=54.3,
                    pattern_factors="Long L1: -3.5%/90m",
                    tf="LONG_BOUNCE_5m",
                    sl_override=49.25, tp_override=51.875,
                    max_hold_override=6)
    assert t is not None, "Failed to enter LONG_BOUNCE trade"
    assert float(t["SL"]) == pytest.approx(49.25, abs=0.01), "SL must be BELOW entry"
    assert float(t["Target"]) == pytest.approx(51.875, abs=0.01), "TP must be ABOVE entry"
    assert int(t["Qty"]) > 0
    # bucket capital present
    port = load_portfolio()
    cbm = port.get("capital_by_market", {})
    assert "LONG_BOUNCE" in cbm, "LONG_BOUNCE bucket missing"
    assert cbm["LONG_BOUNCE"] == pytest.approx(100000.0, abs=1.0)


def test_long_bounce_sl_hit(test_env):
    """LONG SL hit intraday -> closes, loss, LONG_BOUNCE bucket reduced."""
    enter_trade("INDIAN", "TESTLB2.NS", "LONG", 50.00,
                "Long Bounce test", pattern_rank=935, expected_win_rate=54.3,
                pattern_factors="Long L1", tf="LONG_BOUNCE_5m",
                sl_override=49.25, tp_override=51.875, max_hold_override=6)
    ohlc = build_ohlc_data("TESTLB2.NS", lambda: {"close": 49.00, "high": 50.20, "low": 48.80})
    msgs = update_trades(ohlc)
    assert len(msgs) == 1, f"expected 1 close msg, got {len(msgs)}"
    assert "SL Hit" in msgs[0]
    df = pd.read_csv(pt.PAPER_FILE, on_bad_lines="warn")
    closed = df[(df["Status"] == "CLOSED") & (df["Ticker"] == "TESTLB2.NS")]
    assert len(closed) == 1
    assert float(closed.iloc[0]["P&L"]) < 0
    port = load_portfolio()
    assert port["capital_by_market"]["LONG_BOUNCE"] < 100000.0


def test_long_bounce_tp_hit(test_env):
    """LONG TP hit -> closes, profit, LONG_BOUNCE bucket increased."""
    enter_trade("INDIAN", "TESTLB3.NS", "LONG", 50.00,
                "Long Bounce test", pattern_rank=935, expected_win_rate=54.3,
                pattern_factors="Long L1", tf="LONG_BOUNCE_5m",
                sl_override=49.25, tp_override=51.875, max_hold_override=6)
    ohlc = build_ohlc_data("TESTLB3.NS", lambda: {"close": 52.00, "high": 52.50, "low": 49.80})
    msgs = update_trades(ohlc)
    assert len(msgs) == 1, f"expected 1 close msg, got {len(msgs)}"
    assert "Target" in msgs[0]
    df = pd.read_csv(pt.PAPER_FILE, on_bad_lines="warn")
    closed = df[(df["Status"] == "CLOSED") & (df["Ticker"] == "TESTLB3.NS")]
    assert len(closed) == 1
    assert float(closed.iloc[0]["P&L"]) > 0
    port = load_portfolio()
    assert port["capital_by_market"]["LONG_BOUNCE"] > 100000.0


def test_long_bounce_tg_section_mapping(test_env):
    """Telegram summary maps LONG_BOUNCE_5m to the LONG section with count 1."""
    import bot
    assert bot._section_of("LONG_BOUNCE_5m", "INDIAN") == "LONG"
    assert bot._strategy_counts().get("LONG") == 1
    assert bot._SECTION_TREE.get("LONG") is not None

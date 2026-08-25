"""Reconfirmed-issue fixes, pinned by tests:
- gap-aware SL/TP fills (bar OPEN beyond trigger fills at the open)
- index (^) symbols refused for live entries (no synthetic fills)
- qty honesty: no forced minimum share
- tg_safe() strips Telegram Markdown breakers
"""
from datetime import datetime
import pandas as pd
import pytest
import pytz

import paper_trader
from paper_trader import _bars_sl_tp, split_suspected
from config import tg_safe

IST = pytz.timezone("Asia/Kolkata")


def ts(*a):
    return pd.Timestamp(datetime(*a), tz="UTC")


ENTRY = IST.localize(datetime(2026, 1, 5, 9, 30))  # 03:30 UTC


# ------------------------------------------------------------- gap-aware fills
def test_long_sl_gap_open_fills_at_open():
    """Bar OPENS below SL → fill at open (worse), not the optimistic SL."""
    bars = [(ts(2026, 1, 5, 4, 0), 98.0, 90.0, 91.0, 92.5)]  # hi lo close OPEN=92.5 <SL? no
    bars[0] = (ts(2026, 1, 5, 4, 0), 95.0, 89.0, 90.0, 91.0)  # open 91 <= sl 93
    out = _bars_sl_tp(bars, "FADE_1h", ENTRY, "LONG", 93.0, 110.0, 5, mode="INDIAN")
    assert out == (91.0, "SL Hit (intraday)")


def test_long_sl_touch_without_gap_fills_at_sl():
    """Open above SL, low touches → classic trigger fill."""
    bars = [(ts(2026, 1, 5, 4, 0), 96.0, 92.9, 95.0, 95.8)]  # open 95.8 > 93
    out = _bars_sl_tp(bars, "FADE_1h", ENTRY, "LONG", 93.0, 110.0, 5, mode="INDIAN")
    assert out == (93.0, "SL Hit (intraday)")


def test_long_tp_gap_open_fills_at_open():
    """Gap up through target → better fill at open."""
    bars = [(ts(2026, 1, 5, 4, 0), 115.0, 104.0, 114.0, 113.5)]  # open 113.5 >= tp 112
    out = _bars_sl_tp(bars, "FADE_1h", ENTRY, "LONG", 93.0, 112.0, 5, mode="INDIAN")
    assert out == (113.5, "Target Hit")


def test_four_tuple_bars_keep_legacy_trigger_fill():
    """Old 4-tuple format must keep working exactly as before."""
    bars = [(ts(2026, 1, 5, 4, 0), 95.0, 89.0, 90.0)]
    out = _bars_sl_tp(bars, "FADE_1h", ENTRY, "LONG", 93.0, 110.0, 5, mode="INDIAN")
    assert out == (93.0, "SL Hit (intraday)")


# ------------------------------------------------------------ index entry gate
def test_index_ticker_refused_when_enforced(test_env):
    t = paper_trader.enter_trade("US", "^SOX", "LONG", 11479.53, "synthetic?",
                                 tf="SWING_1d", enforce_market_hours=True)
    assert t is None, "index symbols must never be live-entered"


def test_index_ticker_allowed_without_enforcement():
    # replay/tests path unchanged
    reason = paper_trader.check_entry_allowed("^SOX", "LONG")
    assert reason is None or not reason.startswith("INDEX_NOT_TRADABLE")


# ------------------------------------------------------------------ qty honesty
def test_calculate_qty_bounded_overrisk_takes_one(monkeypatch):
    monkeypatch.setattr(paper_trader, "load_portfolio",
                        lambda: {"capital_by_market": {"CRYPTO": 100000.0}})
    # budget = 1000; per-share risk |60060-58800| = 1260 = 1.26x <= 2x → qty 1
    assert paper_trader.calculate_qty(60060.0, 58800.0, market="CRYPTO",
                                      tf="INTRADAY_1h") == 1


def test_calculate_qty_refuses_unbounded_overrisk(monkeypatch):
    monkeypatch.setattr(paper_trader, "load_portfolio",
                        lambda: {"capital_by_market": {"FADE": 1000.0}})
    # budget = 10; per-share risk 1000 = 100x > 2x policy → honest 0 (skip)
    assert paper_trader.calculate_qty(5000.0, 4000.0, market="INDIAN",
                                      tf="FADE_1h") == 0


# --------------------------------------------------------------------- tg_safe
def test_tg_safe_strips_markdown_breakers():
    assert tg_safe("dYZ_ SL Hit") == "dYZ- SL Hit"
    assert tg_safe("a*b`c[d]e") == "ab'c(d)e"
    assert tg_safe(None) == ""


# --------------------------------------------------- regression: split guard API
def test_split_guard_boundary_is_inclusive():
    assert split_suspected("LONG", 50.0, 100.0) is True     # exact 2:1 split
    assert split_suspected("SHORT", 200.0, 100.0) is True

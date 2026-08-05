"""
Tests for persisted entry-skip logging (2026-08-05).

When a fired signal cannot be entered, the rejection reason must be
permanently auditable:
    - paper_trader.check_entry_allowed() explains WHY (cap / duplicate)
    - enter_trade() writes a SKIP event to logs/trade_audit.json
    - bot.py persists skipped_entries (with reason) in the daily scan log
"""

import paper_trader as pt
from paper_trader import enter_trade


def test_check_entry_allowed_none_when_space(test_env):
    assert pt.check_entry_allowed("SPY", "LONG") is None
    assert pt.check_entry_allowed("SPY", "SHORT") is None


def test_check_entry_allowed_duplicate(test_env):
    t = enter_trade("US", "SPY", "LONG", 100.0, "Test", pattern_rank=1,
                    expected_win_rate=60.0, pattern_factors="P", tf="SWING_1d")
    assert t is not None
    assert pt.check_entry_allowed("SPY", "LONG") == "Duplicate SPY LONG already open"
    # Opposite direction on the same ticker is still allowed
    assert pt.check_entry_allowed("SPY", "SHORT") is None


def test_duplicate_entry_skipped_and_audited(test_env):
    t1 = enter_trade("US", "SPY", "LONG", 100.0, "Test", pattern_rank=1,
                     expected_win_rate=60.0, pattern_factors="P", tf="SWING_1d")
    assert t1 is not None

    t2 = enter_trade("US", "SPY", "LONG", 101.0, "Test", pattern_rank=1,
                     expected_win_rate=60.0, pattern_factors="P", tf="SWING_1d")
    assert t2 is None, "duplicate ticker+direction must be rejected"

    audit = pt._load_audit()
    skips = [e for e in audit if e["event"] == "SKIP"]
    assert len(skips) == 1
    assert skips[0]["ticker"] == "SPY"
    assert skips[0]["direction"] == "LONG"
    assert skips[0]["skip_reason"] == "Duplicate SPY LONG already open"
    assert skips[0]["tf"] == "SWING_1d"


def test_check_entry_allowed_cap_and_audited(test_env, monkeypatch):
    monkeypatch.setattr(pt, "MAX_CONCURRENT", 3)
    for i in range(3):
        t = enter_trade("US", f"T{i}", "LONG", 100.0, "Test", pattern_rank=1,
                        expected_win_rate=60.0, pattern_factors="P", tf="SWING_1d")
        assert t is not None

    assert pt.check_entry_allowed("AAA", "LONG").startswith("MAX_CONCURRENT")

    t4 = enter_trade("US", "AAA", "LONG", 100.0, "Test", pattern_rank=1,
                     expected_win_rate=60.0, pattern_factors="P", tf="SWING_1d")
    assert t4 is None, "cap must reject a new unique ticker"

    audit = pt._load_audit()
    skips = [e for e in audit if e["event"] == "SKIP"]
    assert any("MAX_CONCURRENT" in s["skip_reason"] for s in skips)

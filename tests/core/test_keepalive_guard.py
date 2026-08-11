"""
Tests for keepalive_guard.py — the LIVE P&L self-reschedule decision.

2026-08-11: GitHub Actions cron went silent for 75 min while 7 gap-down
scalps were open; no SL check ran and all rode through SL to expiry. The
keep-alive chain re-triggers the live P&L workflow every ~2 min while
time-sensitive (GAP_DOWN_1m / INTRADAY_1h) positions are open and their
market is active, and stops cleanly otherwise (so it never burns minutes).
"""

from datetime import datetime

import pytest
import pytz

import keepalive_guard as kg

IST = pytz.timezone("Asia/Kolkata")


def _dt(y, m, d, h, mi):
    return datetime(y, m, d, h, mi, tzinfo=IST)


def test_stop_when_no_open_positions():
    assert kg.should_reschedule([], _dt(2026, 8, 11, 12, 0))[0] is False


def test_stop_when_only_swing_positions():
    """Swing holds are covered by cron — no need for the fast self-loop."""
    opens = [{"TimeFrame": "SWING_1d", "Mode": "US"}]
    assert kg.should_reschedule(opens, _dt(2026, 8, 11, 12, 0))[0] is False


def test_reschedule_gapdown_during_india_market():
    opens = [{"TimeFrame": "GAP_DOWN_1m", "Mode": "INDIAN"}]
    # Tuesday 11:00 IST — India market open
    assert kg.should_reschedule(opens, _dt(2026, 8, 11, 11, 0))[0] is True


def test_stop_gapdown_outside_india_market():
    opens = [{"TimeFrame": "GAP_DOWN_1m", "Mode": "INDIAN"}]
    # Saturday 11:00 IST — market closed
    assert kg.should_reschedule(opens, _dt(2026, 8, 15, 11, 0))[0] is False
    # Weekday 18:00 IST — India market closed (US open time doesn't matter
    # for an INDIAN-mode position)
    assert kg.should_reschedule(opens, _dt(2026, 8, 11, 18, 0))[0] is False


def test_reschedule_us_intraday_during_us_session():
    opens = [{"TimeFrame": "INTRADAY_1h", "Mode": "US"}]
    # Monday 21:00 IST = US market open (18:30-02:30 IST)
    assert kg.should_reschedule(opens, _dt(2026, 8, 10, 21, 0))[0] is True


def test_reschedule_crypto_anytime():
    opens = [{"TimeFrame": "INTRADAY_1h", "Mode": "CRYPTO"}]
    # Crypto is 24/7 — even weekend 03:00 IST
    assert kg.should_reschedule(opens, _dt(2026, 8, 15, 3, 0))[0] is True


def test_stop_mixed_swing_and_gapdown_outside_window():
    opens = [
        {"TimeFrame": "SWING_1d", "Mode": "US"},
        {"TimeFrame": "GAP_DOWN_1m", "Mode": "INDIAN"},
    ]
    # Saturday — India closed, gap-down inactive
    assert kg.should_reschedule(opens, _dt(2026, 8, 15, 11, 0))[0] is False


def test_main_exit_codes(tmp_path, monkeypatch):
    """main() exits 0 (reschedule) vs 9 (stop) based on portfolio.json."""
    import os
    pf = tmp_path / "portfolio.json"
    pf.write_text('{"open_positions": [{"TimeFrame": "SWING_1d", "Mode": "US"}]}')
    monkeypatch.setattr(kg, "PORTFOLIO_FILE", str(pf))
    with pytest.raises(SystemExit) as e:
        kg.main()
    assert e.value.code == 9

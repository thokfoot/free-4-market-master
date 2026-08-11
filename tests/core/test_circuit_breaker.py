"""
Circuit breaker regression tests (v5.11).

Behavior under test (config.py):
    CIRCUIT_BREAKER_ENABLED            = True
    CIRCUIT_BREAKER_MAX_CONSEC_LOSSES  = 5
    CIRCUIT_BREAKER_COOLDOWN_DAYS      = 2

Design rules verified here:
  - A strategy that loses 5 trades in a row is auto-paused from NEW entries.
  - The counter is FORWARD-LOOKING: it starts at 0 on deployment and only
    losses from that point on count (historical losses are never counted).
  - A WIN resets the streak; a win on any open position lifts an active pause.
  - resume_strategy(rank) manually lifts a pause.
  - After CIRCUIT_BREAKER_COOLDOWN_DAYS days the strategy auto-resumes.
  - Breakeven (P&L == 0) is neither a win nor a loss.
  - live_pnl_updater.update_strategy_stats maintains identical state
    (engine parity with paper_trader).
"""
import pytest
from datetime import datetime, timezone, timedelta
import pytz

import paper_trader as pt
from paper_trader import (
    enter_trade,
    update_trades,
    check_entry_allowed,
    update_strategy_stats,
    resume_strategy,
    _load_strategy_stats,
    _load_audit,
)

IST = pytz.timezone("Asia/Kolkata")

# Frozen start used by the conftest frozen_time fixture (2026-01-15 10:30 IST)
FROZEN_START = datetime(2026, 1, 15)


def _date_after(days):
    """IST date string `days` days after the frozen start."""
    return (FROZEN_START + timedelta(days=days)).strftime("%Y-%m-%d")


# ======================================================================
# Helpers (mirror test_replay.py's frozen-time advancement pattern)
# ======================================================================

def _advance(monkeypatch, days=1):
    """Advance the frozen clock by N days."""
    current = pt.datetime._FROZEN_NAIVE
    new_time = current + timedelta(days=days)

    class AdvDateTime:
        _FROZEN_NAIVE = new_time

        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return IST.localize(cls._FROZEN_NAIVE)
            return cls._FROZEN_NAIVE

        @classmethod
        def utcnow(cls):
            return cls._FROZEN_NAIVE.replace(tzinfo=timezone.utc)

        @classmethod
        def strptime(cls, date_string, format):
            return datetime.strptime(date_string, format)

    monkeypatch.setattr("paper_trader.datetime", AdvDateTime)
    return new_time


def _freeze_live_datetime(monkeypatch):
    """Freeze live_pnl_updater's own datetime import (not patched by conftest)."""
    import live_pnl_updater as lp

    class FrozenDT:
        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return IST.localize(FROZEN_START.replace(hour=10, minute=30))
            return FROZEN_START.replace(hour=10, minute=30)

        @classmethod
        def strptime(cls, date_string, format):
            return datetime.strptime(date_string, format)

    monkeypatch.setattr(lp, "datetime", FrozenDT)


def _enter(monkeypatch, ticker, rank, price=100.0, direction="LONG"):
    """Enter a US swing LONG for the given strategy rank."""
    return enter_trade("US", ticker, direction, price,
                       f"#{rank} CB test", pattern_rank=rank,
                       expected_win_rate=60.0)


def _force_sl(monkeypatch, ticker):
    """Advance a day and drive the open LONG into its SL (entry 100 -> SL 98)."""
    _advance(monkeypatch)
    msgs = update_trades({ticker: {"close": 99.0, "high": 100.5, "low": 97.5}})
    assert msgs, f"expected SL exit for {ticker}"
    return msgs


def _force_tp(monkeypatch, ticker):
    """Advance a day and drive the open LONG into its TP (entry 100 -> TP 104)."""
    _advance(monkeypatch)
    msgs = update_trades({ticker: {"close": 105.0, "high": 106.0, "low": 101.0}})
    assert msgs, f"expected TP exit for {ticker}"
    return msgs


def _streak(rank, n, monkeypatch, prefix="L"):
    """Enter n LONG trades for rank and force each to SL -> n consecutive losses.

    Each forced SL advances the frozen clock by one day, so the n-th loss is
    recorded on `_date_after(n)`.
    """
    for i in range(n):
        assert _enter(monkeypatch, f"{prefix}{i}", rank) is not None, \
            f"entry {i} for rank {rank} should have been allowed"
        _force_sl(monkeypatch, f"{prefix}{i}")


# ======================================================================
# 1. Below threshold -> entries stay allowed
# ======================================================================

def test_below_threshold_entries_allowed(test_env, monkeypatch):
    """4 consecutive losses must NOT pause the strategy (threshold is 5)."""
    _streak(5, 4, monkeypatch, prefix="BL")
    stats = _load_strategy_stats()
    assert stats["5"]["consec_losses"] == 4
    assert stats["5"].get("paused_since") is None
    assert check_entry_allowed("BLX", "LONG", pattern_rank=5) is None
    assert _enter(monkeypatch, "BLX", 5) is not None


# ======================================================================
# 2. Pause at threshold (5 consecutive losses)
# ======================================================================

def test_paused_at_five_consecutive_losses(test_env, monkeypatch):
    """5 consecutive losses pause the strategy: entries blocked + audit SKIP."""
    _streak(6, 5, monkeypatch, prefix="PA")
    stats = _load_strategy_stats()
    assert stats["6"]["consec_losses"] == 5
    assert stats["6"]["paused_since"] == _date_after(5), stats["6"]["paused_since"]

    reason = check_entry_allowed("PAX", "LONG", pattern_rank=6)
    assert reason is not None
    assert "CIRCUIT_BREAKER" in reason and "Rank #6" in reason

    # enter_trade itself must reject with an audit SKIP entry
    t = _enter(monkeypatch, "PAX", 6)
    assert t is None
    skips = [e for e in _load_audit() if e.get("event") == "SKIP"]
    assert any("CIRCUIT_BREAKER" in str(e.get("skip_reason", "")) for e in skips), \
        f"no CIRCUIT_BREAKER SKIP audit entry: {skips[-1:]}"


# ======================================================================
# 3. Win resets the streak
# ======================================================================

def test_win_resets_streak(test_env, monkeypatch):
    """A win after losses resets consec_losses back to 0."""
    _streak(11, 2, monkeypatch, prefix="WR")
    assert _load_strategy_stats()["11"]["consec_losses"] == 2
    assert _enter(monkeypatch, "WRX", 11) is not None
    _force_tp(monkeypatch, "WRX")
    stats = _load_strategy_stats()
    assert stats["11"]["consec_losses"] == 0
    assert stats["11"]["paused_since"] is None
    # entries still allowed
    assert _enter(monkeypatch, "WRY", 11) is not None


# ======================================================================
# 4. Win on a pre-pause position lifts an active pause
# ======================================================================

def test_win_lifts_active_pause(test_env, monkeypatch):
    """A win on any open position (entered before the pause) resumes the strategy."""
    # Enter SIX open positions for rank 7 before any exit
    for i in range(6):
        assert _enter(monkeypatch, f"WL{i}", 7) is not None
    # Close 5 as losses -> rank 7 pauses (5th loss on day 5)
    for i in range(5):
        _force_sl(monkeypatch, f"WL{i}")
    assert _load_strategy_stats()["7"]["paused_since"] == _date_after(5)
    assert check_entry_allowed("WLX", "LONG", pattern_rank=7) is not None
    # The 6th position still open -> close it as a WIN -> pause lifts
    _force_tp(monkeypatch, "WL5")
    stats = _load_strategy_stats()
    assert stats["7"]["consec_losses"] == 0
    assert stats["7"]["paused_since"] is None
    assert check_entry_allowed("WLX", "LONG", pattern_rank=7) is None
    assert _enter(monkeypatch, "WLX", 7) is not None


# ======================================================================
# 5. Manual resume
# ======================================================================

def test_manual_resume(test_env, monkeypatch):
    """resume_strategy(rank) lifts a pause and restores a fresh budget."""
    _streak(8, 5, monkeypatch, prefix="MR")
    assert _load_strategy_stats()["8"]["paused_since"] == _date_after(5)
    assert resume_strategy(8) is True
    stats = _load_strategy_stats()
    assert stats["8"]["paused_since"] is None
    assert stats["8"]["consec_losses"] == 0
    assert check_entry_allowed("MRX", "LONG", pattern_rank=8) is None
    assert _enter(monkeypatch, "MRX", 8) is not None
    # Resuming a non-paused rank returns False
    assert resume_strategy(8) is False


# ======================================================================
# 6. Cooldown auto-resume
# ======================================================================

def test_cooldown_auto_resume(test_env, monkeypatch):
    """After CIRCUIT_BREAKER_COOLDOWN_DAYS the strategy auto-resumes."""
    _streak(9, 5, monkeypatch, prefix="CD")
    assert _load_strategy_stats()["9"]["paused_since"] == _date_after(5)
    # Within cooldown: still blocked
    assert check_entry_allowed("CDX", "LONG", pattern_rank=9) is not None
    # Advance past the 2-day cooldown (day 5 -> day 7)
    _advance(monkeypatch, days=2)
    assert check_entry_allowed("CDX", "LONG", pattern_rank=9) is None
    stats = _load_strategy_stats()
    assert stats["9"]["paused_since"] is None
    assert stats["9"]["consec_losses"] == 0
    assert _enter(monkeypatch, "CDX", 9) is not None


# ======================================================================
# 7. Disabled via config flag
# ======================================================================

def test_disabled_via_config(test_env, monkeypatch):
    """CIRCUIT_BREAKER_ENABLED=False disables pausing entirely."""
    monkeypatch.setattr(pt, "CIRCUIT_BREAKER_ENABLED", False)
    _streak(10, 6, monkeypatch, prefix="DS")
    stats = _load_strategy_stats()
    assert stats["10"]["consec_losses"] == 6
    assert stats["10"].get("paused_since") is None
    assert check_entry_allowed("DSX", "LONG", pattern_rank=10) is None
    assert _enter(monkeypatch, "DSX", 10) is not None


# ======================================================================
# 8. Breakeven does not count
# ======================================================================

def test_breakeven_does_not_count(test_env, monkeypatch):
    """P&L == 0 is neither a win nor a loss — the streak is unchanged."""
    update_strategy_stats("#12SW breakeven", 0.0)
    update_strategy_stats("#12SW breakeven", 0.0)
    stats = _load_strategy_stats()
    assert stats["12"]["consec_losses"] == 0
    assert stats["12"]["paused_since"] is None
    assert stats["12"]["losses"] == 0


# ======================================================================
# 9. Gap-down ranks tracked
# ======================================================================

def test_gap_down_rank_tracking(test_env, monkeypatch):
    """Gap-down strategy ranks (997/998) are tracked by the same guard."""
    for _ in range(3):
        update_strategy_stats("#997ID gap_down_52wk_low", -100.0)
    stats = _load_strategy_stats()
    assert stats["997"]["consec_losses"] == 3
    assert stats["997"]["paused_since"] is None  # not yet at threshold
    for _ in range(2):
        update_strategy_stats("#997ID gap_down_52wk_low", -100.0)
    stats = _load_strategy_stats()
    assert stats["997"]["consec_losses"] == 5
    assert stats["997"]["paused_since"] == "2026-01-15"
    assert check_entry_allowed("PFC.NS", "LONG", tf="GAP_DOWN_1m", pattern_rank=997) is not None


# ======================================================================
# 10. live_pnl_updater parity
# ======================================================================

def test_live_pnl_updater_parity(test_env, monkeypatch):
    """live_pnl_updater maintains identical CB state in the same file."""
    import live_pnl_updater as lp
    monkeypatch.setattr(lp, "STRATEGY_STATS_FILE", pt.STRATEGY_STATS_FILE)
    _freeze_live_datetime(monkeypatch)

    for _ in range(5):
        lp.update_strategy_stats("#998ID gap_down", -50.0)
    stats = _load_strategy_stats()
    assert stats["998"]["consec_losses"] == 5
    assert stats["998"]["paused_since"] == "2026-01-15"

    # paper_trader's entry guard sees the same pause
    reason = check_entry_allowed("GRASIM.NS", "LONG", tf="GAP_DOWN_1m", pattern_rank=998)
    assert reason is not None and "CIRCUIT_BREAKER" in reason

    # a win via the live updater lifts the pause in the shared file
    lp.update_strategy_stats("#998ID gap_down", +150.0)
    stats = _load_strategy_stats()
    assert stats["998"]["consec_losses"] == 0
    assert stats["998"]["paused_since"] is None


# ======================================================================
# 11. Legacy stats entries get defaults
# ======================================================================

def test_legacy_entry_gets_defaults(test_env, monkeypatch):
    """Old strategy_stats entries without CB fields get them via setdefault."""
    stats = _load_strategy_stats()
    stats["77"] = {"rank": 77, "factors": "legacy", "wins": 1, "losses": 1,
                   "total_pnl": -10.0}  # no consec_losses / paused_since
    pt._save_strategy_stats(stats)

    update_strategy_stats("#77SW legacy loss", -5.0)
    stats = _load_strategy_stats()
    assert stats["77"]["consec_losses"] == 1
    assert stats["77"]["paused_since"] is None
    assert check_entry_allowed("LGX", "LONG", pattern_rank=77) is None


# ======================================================================
# 12. pattern_rank=None bypasses the guard
# ======================================================================

def test_none_rank_passes_through(test_env, monkeypatch):
    """Callers that do not know the rank never trigger the guard."""
    stats = _load_strategy_stats()
    stats["13"] = {"rank": 13, "factors": "x", "wins": 0, "losses": 5,
                   "total_pnl": -100.0, "consec_losses": 5,
                   "paused_since": "2026-01-15"}
    pt._save_strategy_stats(stats)

    # without pattern_rank: no circuit-breaker check (dedupe/MAX_CONCURRENT only)
    assert check_entry_allowed("NKX", "LONG") is None
    # with pattern_rank: blocked
    assert check_entry_allowed("NKX", "LONG", pattern_rank=13) is not None


# ======================================================================
# 13. Existing stats fields remain intact (no data loss)
# ======================================================================

def test_existing_fields_intact(test_env, monkeypatch):
    """Wins/losses/total_pnl continue to accumulate exactly as before."""
    _streak(14, 3, monkeypatch, prefix="EF")
    update_strategy_stats("#14SW win", +120.0)
    stats = _load_strategy_stats()
    assert stats["14"]["wins"] == 1
    assert stats["14"]["losses"] == 3
    assert stats["14"]["consec_losses"] == 0

    # total_pnl must equal the CSV ground truth (3 real SL losses) + synthetic win
    import pandas as pd
    df = pd.read_csv(pt.PAPER_FILE)
    rank_rows = df[df["Pattern_Rank"].astype(str) == "14"]
    csv_total = float(pd.to_numeric(rank_rows["P&L"], errors="coerce").sum())
    assert stats["14"]["total_pnl"] == pytest.approx(csv_total + 120.0, abs=0.02)

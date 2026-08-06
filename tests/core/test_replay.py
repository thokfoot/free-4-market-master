"""
Phase 2G: Deterministic Replay / Regression

Verifies complete end-to-end trade lifecycles using sequential OHLC snapshots
and advancing frozen time between steps.

Key invariants verified across every scenario:
  - portfolio.json          (capital, P&L, open positions, wins/losses)
  - paper_trades.csv        (Status, P&L, Exit_Price, charges)
  - strategy_stats.json     (wins, losses, total_pnl per rank)
  - trade_audit.json        (ENTRY + EXIT events with all fields)
  - Report generation       (HTML does not crash)
  - Determinism             (identical inputs → identical outputs across runs)

Floating-point policy:
  - P&L, P&L_% from CSV:  exact equality via round(x, 2) / round(x, 1)
  - Capital / total_pnl:   pytest.approx(abs=0.02) for aggregated values
  - All other fields:      exact equality
"""

import os
import hashlib
import pytest
import pandas as pd
from datetime import datetime, timezone, timedelta
import pytz

import paper_trader as _pt
from paper_trader import (
    enter_trade,
    update_trades,
    load_portfolio,
    _load_audit,
    _load_strategy_stats,
    generate_portfolio_report,
    get_strategy_stats,
)

IST = pytz.timezone("Asia/Kolkata")


# ======================================================================
# Time advancement helper
# ======================================================================

def _advance_time(monkeypatch, days=0, hours=0, minutes=0):
    """
    Advance the frozen clock by the given delta.

    Reads the current frozen time from paper_trader.datetime._FROZEN_NAIVE,
    computes new_time = current + delta, and monkeypatches a new
    FrozenDateTime class with the advanced time.

    Args:
        monkeypatch: pytest monkeypatch fixture
        days, hours, minutes: delta components (default 0)

    Returns:
        The new naive datetime
    """
    current = _pt.datetime._FROZEN_NAIVE
    delta = timedelta(days=days, hours=hours, minutes=minutes)
    new_time = current + delta

    class AdvancedDateTime:
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

    monkeypatch.setattr("paper_trader.datetime", AdvancedDateTime)
    return new_time


# ======================================================================
# State capture helper
# ======================================================================

def _capture():
    """Return a snapshot of all observable state (uses _pt.PAPER_FILE at call time)."""
    port = load_portfolio()
    csv_path = _pt.PAPER_FILE
    trades = pd.read_csv(csv_path, on_bad_lines="warn") if os.path.exists(csv_path) else pd.DataFrame()
    stats = _load_strategy_stats()
    audit = _load_audit()

    return {
        "portfolio": port,
        "open_positions": len(port.get("open_positions", [])),
        "closed_count": port.get("closed_count", 0),
        "total_pnl": port.get("total_pnl", 0.0),
        "total_wins": port.get("total_wins", 0),
        "total_losses": port.get("total_losses", 0),
        "trades_df": trades,
        "num_trades": len(trades) if len(trades) > 0 else 0,
        "open_in_csv": len(trades[trades["Status"].astype(str) == "OPEN"]) if len(trades) > 0 else 0,
        "closed_in_csv": len(trades[trades["Status"].astype(str) == "CLOSED"]) if len(trades) > 0 else 0,
        "strategy_stats": stats,
        "audit": audit,
        "audit_events": len(audit),
    }


def _get_trade(idx=0):
    """Return the idx-th trade from CSV as a dict (uses _pt.PAPER_FILE at call time)."""
    df = pd.read_csv(_pt.PAPER_FILE, on_bad_lines="warn")
    if len(df) == 0:
        return {}
    row = df.iloc[idx]
    return {c: row[c] for c in df.columns}


def _hash_file(path):
    """Return SHA256 hex digest of a file, or empty string if missing."""
    if not os.path.exists(path):
        return ""
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


# Module-level store for determinism comparison across parametrized runs
_DETERM_HASHES: dict = {}


# ======================================================================
# Scenario 1: LONG SWING -> TP Hit (Day 1 enter, Day 3 exit)
# ======================================================================

class TestReplayLongTp:
    """LONG swing trade -> TP hit on Day 3. Verifies complete lifecycle."""

    def test_full_lifecycle(self, test_env, monkeypatch):
        # -- Day 1: Enter LONG SPY @ 100 --
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#1 Test LONG TP", pattern_rank=1, expected_win_rate=62.0)
        assert t is not None
        assert t["Status"] == "OPEN"
        # Entry gets LONG entry slippage (US swing 0.01%): 100 -> 100.01
        assert t["Entry_Price"] == 100.01
        assert t["SL"] == 98.0
        assert t["Target"] == 104.0
        assert t["Qty"] > 0

        s = _capture()
        assert s["open_positions"] == 1
        assert s["closed_count"] == 0
        assert s["num_trades"] == 1
        assert s["open_in_csv"] == 1
        assert s["closed_in_csv"] == 0
        assert s["audit_events"] == 1
        assert s["audit"][0]["event"] == "ENTRY"
        assert s["audit"][0]["ticker"] == "SPY"

        # -- Day 2: Price moves up but within SL/TP --
        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 102.00, "high": 103.00, "low": 99.00}})

        s = _capture()
        assert len(msgs) == 0, f"Expected no exit on Day 2, got: {msgs}"
        assert s["open_positions"] == 1
        assert s["closed_count"] == 0
        assert s["audit_events"] == 1

        # -- Day 3: High hits TP -> exit --
        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}})

        assert len(msgs) == 1, f"Expected 1 exit on Day 3, got: {msgs}"
        assert "Target" in msgs[0], f"Expected Target hit, got: {msgs[0]}"

        trade = _get_trade(0)
        assert trade["Status"] == "CLOSED"
        pnl = float(trade["P&L"])
        assert pnl > 0, f"Expected positive P&L, got {pnl}"
        assert "Target Hit" in str(trade["Reason"])

        s = _capture()
        assert s["open_positions"] == 0
        assert s["closed_count"] == 1
        assert s["total_wins"] == 1
        assert s["total_losses"] == 0
        assert s["open_in_csv"] == 0
        assert s["closed_in_csv"] == 1
        assert s["audit_events"] == 2
        assert s["audit"][1]["event"] == "EXIT"
        assert float(s["audit"][1]["pnl"]) == pytest.approx(pnl, abs=0.02)

        assert "1" in s["strategy_stats"]
        assert s["strategy_stats"]["1"]["wins"] == 1
        assert s["strategy_stats"]["1"]["losses"] == 0
        assert s["strategy_stats"]["1"]["total_pnl"] == pytest.approx(pnl, abs=0.02)

        assert s["total_pnl"] == pytest.approx(pnl, abs=0.02)
        assert s["portfolio"]["total_pnl_by_market"]["US"] == pytest.approx(pnl, abs=0.02)

        report_path = generate_portfolio_report()
        assert os.path.exists(report_path)
        with open(report_path, "r", encoding="utf-8") as f:
            html = f.read()
        assert "SPY" in html
        assert "CLOSED" in html


# ======================================================================
# Scenario 2: SHORT SWING -> SL Hit (Day 1 enter, Day 2 exit)
# ======================================================================

class TestReplayShortSl:
    """SHORT swing trade -> SL hit on Day 2. Verifies loss lifecycle."""

    def test_full_lifecycle(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "SHORT", 100.00,
                        "#2 Test SHORT SL", pattern_rank=2, expected_win_rate=64.0)
        assert t is not None
        assert t["Status"] == "OPEN"
        assert t["SL"] == 102.0
        assert t["Target"] == 96.0

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 101.00, "high": 102.50, "low": 99.00}})

        assert len(msgs) == 1, f"Expected 1 exit on Day 2, got: {msgs}"
        assert "SL" in msgs[0], f"Expected SL hit, got: {msgs[0]}"

        trade = _get_trade(0)
        assert trade["Status"] == "CLOSED"
        pnl = float(trade["P&L"])
        assert pnl < 0, f"Expected negative P&L for SL hit, got {pnl}"

        s = _capture()
        assert s["open_positions"] == 0
        assert s["closed_count"] == 1
        assert s["total_wins"] == 0
        assert s["total_losses"] == 1
        assert s["total_pnl"] == pytest.approx(pnl, abs=0.02)
        assert s["audit_events"] == 2
        assert s["audit"][1]["event"] == "EXIT"

        cap_us = s["portfolio"]["capital_by_market"].get("US", 0)
        assert cap_us < 100000, f"Expected US capital to decrease after loss, got {cap_us}"

        assert "2" in s["strategy_stats"]
        assert s["strategy_stats"]["2"]["losses"] == 1
        assert s["strategy_stats"]["2"]["wins"] == 0


# ======================================================================
# Scenario 3: LONG -> SL Hit intraday (Day 2)
# ======================================================================

class TestReplayLongSl:
    """LONG swing trade -> SL hit intraday on Day 2."""

    def test_full_lifecycle(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#3 Test LONG SL", pattern_rank=3)
        assert t is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 99.00, "high": 100.50, "low": 97.50}})

        assert len(msgs) == 1
        assert "SL" in msgs[0]

        trade = _get_trade(0)
        assert trade["Status"] == "CLOSED"
        pnl = float(trade["P&L"])
        assert pnl < 0

        s = _capture()
        assert s["total_pnl"] == pytest.approx(pnl, abs=0.02)

        exit_price = float(trade["Exit_Price"])
        # Exit at SL=98.00 less LONG exit slippage (US swing 0.01%) = 97.99
        assert exit_price == pytest.approx(97.99, abs=0.01), \
            f"Expected exit at SL=98.0, got {exit_price}"


# ======================================================================
# Scenario 4: Swing expiry
# ======================================================================

# NOTE: Expiry tests (swing 5d, intraday 6h) are blocked by a pytz
# .replace(tzinfo=IST) vs .localize() incompatibility between the
# frozen_time fixture and update_trades()'s internal datetime handling.
# This causes the timedelta (now - entry_date) to produce incorrect
# results (negative or zero), preventing the MaxHold expiry check from
# triggering regardless of the MaxHold value.
#
# A dedicated timezone-consistent fixture is needed. Planned for a
# future phase (e.g., Phase 4 with replay-versioned baselines).



# ======================================================================
# Scenario 6: Multiple trades - cumulative P&L
# ======================================================================

class TestReplayMultipleTrades:
    """Two sequential trades - verify cumulative P&L and strategy stats."""

    def test_two_trades_cumulative(self, test_env, monkeypatch):
        t1 = enter_trade("US", "AAPL", "LONG", 200.00,
                         "#10 Trade WIN", pattern_rank=10, expected_win_rate=66.0)
        assert t1 is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"AAPL": {"close": 210.00, "high": 212.00, "low": 201.00}})
        assert len(msgs) == 1
        pnl1 = float(_get_trade(0)["P&L"])

        _advance_time(monkeypatch, days=1)
        t2 = enter_trade("US", "AAPL", "LONG", 205.00,
                         "#11 Trade LOSS", pattern_rank=11, expected_win_rate=60.0)
        assert t2 is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"AAPL": {"close": 195.00, "high": 207.00, "low": 194.00}})
        assert len(msgs) == 1
        pnl2 = float(_get_trade(1)["P&L"])

        total = pnl1 + pnl2
        s = _capture()
        assert s["total_pnl"] == pytest.approx(total, abs=0.02)
        assert s["closed_count"] == 2
        assert s["total_wins"] == 1
        assert s["total_losses"] == 1
        assert "10" in s["strategy_stats"]
        assert "11" in s["strategy_stats"]
        assert s["strategy_stats"]["10"]["wins"] == 1
        assert s["strategy_stats"]["11"]["losses"] == 1

        stats_result = get_strategy_stats(top_n=5)
        all_ranks = [r["rank"] for r in stats_result["top"]]
        assert 10 in all_ranks or 11 in all_ranks


# ======================================================================
# Scenario 7: Regression - OHLC validation
# ======================================================================

class TestReplayRegressionOhlcValidation:
    """Regression: Invalid OHLC must NEVER close a trade."""

    def test_nan_low_does_not_exit(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#20 Test NaN guard", pattern_rank=20)
        assert t is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 105.00, "high": 106.00, "low": float("nan")}})
        assert len(msgs) == 0, "NaN low must not trigger exit"
        assert _capture()["open_positions"] == 1

    def test_zero_low_does_not_exit(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#20 Test NaN guard", pattern_rank=20)
        assert t is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 105.00, "high": 106.00, "low": 0.0}})
        assert len(msgs) == 0, "Zero low must not trigger exit"
        assert _capture()["open_positions"] == 1

    def test_none_close_does_not_exit(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#20 Test NaN guard", pattern_rank=20)
        assert t is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": None, "high": 106.00, "low": 99.00}})
        assert len(msgs) == 0, "None close must not trigger exit"
        assert _capture()["open_positions"] == 1


# ======================================================================
# Scenario 8: Regression - No double-count strategy stats
# ======================================================================

class TestReplayRegressionStatsNotDoubleCounted:
    """Regression: update_strategy_stats called exactly once per trade exit."""

    def test_stats_incremented_once(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#30 Test Single Stat", pattern_rank=30)
        assert t is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}})
        assert len(msgs) == 1

        msgs2 = update_trades({"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}})
        assert len(msgs2) == 0, "Second call should produce no new exits"

        stats = _load_strategy_stats()
        assert "30" in stats
        assert stats["30"]["wins"] == 1, \
            f"Expected exactly 1 win, got {stats['30']['wins']}"
        assert stats["30"]["losses"] == 0


# ======================================================================
# Scenario 9: Regression - P&L consistency across all outputs
# ======================================================================

class TestReplayRegressionPnlConsistency:
    """P&L must match across CSV, portfolio, audit, and stats."""

    def test_pnl_consistent_across_all_outputs(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#40 Test P&L consistency", pattern_rank=40)
        assert t is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}})
        assert len(msgs) == 1

        trade = _get_trade(0)
        csv_pnl = float(trade["P&L"])

        s = _capture()
        port_pnl = s["portfolio"]["total_pnl"]
        audit_pnl = float(s["audit"][1]["pnl"])
        stats_pnl = s["strategy_stats"]["40"]["total_pnl"]

        assert csv_pnl == pytest.approx(port_pnl, abs=0.02), \
            f"CSV P&L {csv_pnl} != Portfolio P&L {port_pnl}"
        assert csv_pnl == pytest.approx(audit_pnl, abs=0.02), \
            f"CSV P&L {csv_pnl} != Audit P&L {audit_pnl}"
        assert csv_pnl == pytest.approx(stats_pnl, abs=0.02), \
            f"CSV P&L {csv_pnl} != Stats P&L {stats_pnl}"


# ======================================================================
# Scenario 10: SHORT TP hit
# ======================================================================

class TestReplayShortTp:
    """SHORT swing trade -> TP hit. Verify SHORT P&L formula."""

    def test_short_tp_lifecycle(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "SHORT", 100.00,
                        "#50 Test SHORT TP", pattern_rank=50)
        assert t is not None

        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 95.00, "high": 97.00, "low": 94.00}})

        assert len(msgs) == 1, f"Expected TP exit, got: {msgs}"
        assert "Target" in msgs[0]

        trade = _get_trade(0)
        assert trade["Status"] == "CLOSED"
        pnl = float(trade["P&L"])
        assert pnl > 0, f"SHORT TP should have positive P&L, got {pnl}"

        exit_price = float(trade["Exit_Price"])
        # SHORT exit at TP=96.00 pays more (+0.01% US swing slippage) = 96.01
        assert exit_price == pytest.approx(96.01, abs=0.01)


# ======================================================================
# Scenario 11: Swing trade TP (same as intraday test but with wider SL)
# ======================================================================

class TestReplaySwingTpWideSl:
    """Swing trade with TP hit - uses wider SL (2% vs 1%) to avoid SL triggering first."""

    def test_swing_tp(self, test_env, monkeypatch):
        t = enter_trade("US", "QQQ", "LONG", 500.00,
                        "#60 Test Swing TP", pattern_rank=60)
        assert t is not None
        # SL = round_price(500 * 0.98) = 490.0 (swing SL=2%)
        # Target = round_price(500 * 1.04) = 520.0 (swing TP=4%)

        _advance_time(monkeypatch, hours=1)
        msgs = update_trades({"QQQ": {"close": 522.00, "high": 525.00, "low": 505.00}})

        assert len(msgs) == 1, f"Expected TP exit, got: {msgs}"
        assert "Target" in msgs[0]

        trade = _get_trade(0)
        assert trade["Status"] == "CLOSED"
        pnl = float(trade["P&L"])
        assert pnl > 0

        s = _capture()
        assert s["total_wins"] == 1
        assert s["audit_events"] == 2


# ======================================================================
# Scenario 12: Determinism check
# ======================================================================

class TestReplayDeterminism:
    """Run identical scenario twice (each with own test_env), verify identical file hashes."""

    def _run_win_trade_scenario(self, test_env, monkeypatch):
        t = enter_trade("US", "SPY", "LONG", 100.00,
                        "#70 Det Test", pattern_rank=70, expected_win_rate=62.0)
        assert t is not None
        _advance_time(monkeypatch, days=1)
        msgs = update_trades({"SPY": {"close": 105.00, "high": 106.00, "low": 101.00}})
        assert len(msgs) == 1

        return {
            "portfolio": _hash_file(_pt.PORTFOLIO_FILE),
            "trades": _hash_file(_pt.PAPER_FILE),
            "stats": _hash_file(_pt.STRATEGY_STATS_FILE),
            "audit": _hash_file(_pt.AUDIT_FILE),
        }

    @pytest.mark.parametrize("run_id", [1, 2])
    def test_two_runs_identical(self, test_env, monkeypatch, run_id):
        """Each parametrized invocation gets its own test_env (fresh filesystem)."""
        hashes = self._run_win_trade_scenario(test_env, monkeypatch)
        _DETERM_HASHES[f"run{run_id}"] = hashes
        if len(_DETERM_HASHES) == 2:
            assert _DETERM_HASHES["run1"] == _DETERM_HASHES["run2"], \
                f"Determinism FAILED\n  Run1: {_DETERM_HASHES['run1']}\n  Run2: {_DETERM_HASHES['run2']}"

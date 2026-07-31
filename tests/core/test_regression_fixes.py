"""
Regression tests for bugs fixed on 2026-07-31.

Covers:
    Bug 1: live_pnl_updater.save_portfolio() did NOT recalculate total_capital
           after live exits updated capital_by_market (stale by exactly the
           last 4 live exits' losses ~Rs 3,623).
           Fix: save_portfolio now recalculates total_capital = sum(capital_by_market).

    Bug 2: paper_trader.update_trades() computed MaxHold expiry from entry DATE
           (midnight) instead of actual entry time. Evening entries were
           prematurely force-closed (e.g., IWM entered 23:22 IST was closed as
           'Expiry 25h' when actual hold was only ~1.9h).
           Fix: use actual entry datetime (Date + Time_IST).

    Bug 3 (same class): live_pnl_updater never enforced MaxHold expiry, so
           positions could stay open well past their hold limit between
           scheduled bot scans (which have an ~8h gap).
           Fix: MaxHold expiry check added before SL/TP.

Uses the existing test_env fixture (frozen time, isolated fs, mocked TG/yf).
Does NOT modify production code or existing fixtures.
"""

import json
import pandas as pd
import pytest
import pytz
from datetime import datetime, timezone

import paper_trader
from paper_trader import (
    enter_trade, update_trades, load_portfolio,
)

IST = pytz.timezone("Asia/Kolkata")


# ======================================================================
# Time helper (mirrors the _advance_time pattern from test_replay.py)
# ======================================================================

def _set_time(monkeypatch, naive_dt):
    """Freeze paper_trader.datetime to a specific IST naive datetime."""
    class FrozenDT:
        _FROZEN_NAIVE = naive_dt

        @classmethod
        def now(cls, tz=None):
            if tz is not None:
                return IST.localize(cls._FROZEN_NAIVE)
            return cls._FROZEN_NAIVE

        @classmethod
        def utcnow(cls):
            return cls._FROZEN_NAIVE.replace(tzinfo=timezone.utc)

        @classmethod
        def strptime(cls, date_string, fmt):
            return datetime.strptime(date_string, fmt)

    monkeypatch.setattr("paper_trader.datetime", FrozenDT)
    return naive_dt


# ======================================================================
# Bug 1: live_pnl_updater.save_portfolio -> total_capital recalc
# ======================================================================

def test_save_portfolio_recalculates_total_capital(test_env, monkeypatch):
    """Regression: save_portfolio must recompute total_capital from capital_by_market."""
    import live_pnl_updater as lp

    tmp = test_env["tmp_path"]
    log_dir = tmp / "logs"
    port_file = log_dir / "portfolio.json"
    monkeypatch.setattr(lp, "PORTFOLIO_FILE", str(port_file))

    port = {
        "capital_by_market": {
            "INDIAN": 100000.0, "US": 100699.27, "CRYPTO": 100000.0, "INTRADAY": 88504.35,
        },
        "total_capital": 392826.57,  # STALE - must be overwritten
        "open_positions": [],
        "closed_count": 15, "total_wins": 1, "total_losses": 14,
        "total_pnl": -10796.38,
        "total_pnl_by_market": {"INDIAN": 0.0, "US": 699.27, "CRYPTO": 0.0, "INTRADAY": -11495.65},
    }
    lp.save_portfolio(port)

    saved = json.loads(port_file.read_text())
    expected = round(sum(port["capital_by_market"].values()), 2)
    assert saved["total_capital"] == pytest.approx(expected, abs=0.01), \
        f"total_capital stale: got {saved['total_capital']}, expected {expected}"
    assert saved["total_capital"] != pytest.approx(392826.57, abs=0.01), \
        "total_capital was NOT recalculated (stale value preserved)"


def test_save_portfolio_preserves_capital_by_market(test_env, monkeypatch):
    """Regression: capital_by_market values must be unchanged after save."""
    import live_pnl_updater as lp

    tmp = test_env["tmp_path"]
    log_dir = tmp / "logs"
    port_file = log_dir / "portfolio.json"
    monkeypatch.setattr(lp, "PORTFOLIO_FILE", str(port_file))

    cap = {"INDIAN": 100000.0, "US": 100699.27, "CRYPTO": 100000.0, "INTRADAY": 88504.35}
    lp.save_portfolio({
        "capital_by_market": cap,
        "total_capital": 0.0,  # wrong on purpose
        "open_positions": [], "closed_count": 15,
        "total_wins": 1, "total_losses": 14, "total_pnl": -10796.38,
    })
    saved = json.loads(port_file.read_text())
    for k, v in cap.items():
        assert saved["capital_by_market"][k] == pytest.approx(v, abs=0.01), f"cap {k} changed"


# ======================================================================
# Bug 2: paper_trader MaxHold expiry uses ACTUAL entry time (not midnight)
# ======================================================================

def test_maxhold_uses_actual_entry_time_not_midnight(test_env, monkeypatch):
    """Regression: evening intraday entry must NOT expire after ~1.9h hold.

    Real case: IWM entered 23:22:38 IST on 28-Jul was wrongly closed with
    'Expiry 25h' at 01:14 IST next day (midnight-based calc). With actual
    entry time the hold is only ~1.9h < 6h MaxHold, so it must stay OPEN.
    """
    _set_time(monkeypatch, datetime(2026, 1, 15, 23, 22, 38))

    t = enter_trade("US", "IWM", "SHORT", 293.10, "Test IWM SHORT",
                    pattern_rank=14, expected_win_rate=63.41,
                    pattern_factors="Price<SMA50+EMA9>EMA20+EMA20<EMA50+Close>Open",
                    tf="INTRADAY_1h")
    assert t is not None, "Failed to enter test trade"

    # Advance to ~01:14 IST next day (1.87h actual hold)
    _set_time(monkeypatch, datetime(2026, 1, 16, 1, 14, 16))

    # OHLC inside SL/TP band - no SL/TP trigger
    ohlc = {"IWM": {"close": 293.50, "high": 294.00, "low": 292.00}}
    msgs = update_trades(ohlc)

    assert len(msgs) == 0, f"Trade should NOT expire after 1.87h hold, got: {msgs}"

    port = load_portfolio()
    assert len(port.get("open_positions", [])) == 1, "Trade should still be OPEN"


def test_maxhold_expires_after_actual_6h(test_env, monkeypatch):
    """Control: intraday entry DOES expire once actual hold >= 6h."""
    _set_time(monkeypatch, datetime(2026, 1, 15, 23, 22, 38))

    t = enter_trade("US", "SPY", "LONG", 741.73, "Test SPY LONG",
                    pattern_rank=31, expected_win_rate=63.16,
                    pattern_factors="Price>SMA50+EMA20<EMA50+Close<Open",
                    tf="INTRADAY_1h")
    assert t is not None

    # Advance to ~06:00 IST next day (6.6h actual hold) - past MaxHold
    _set_time(monkeypatch, datetime(2026, 1, 16, 6, 0, 0))

    ohlc = {"SPY": {"close": 741.50, "high": 742.00, "low": 740.50}}  # inside SL/TP band
    msgs = update_trades(ohlc)

    assert len(msgs) == 1, f"Expected 1 expiry msg, got: {msgs}"
    assert "Expiry" in msgs[0], f"Expected Expiry reason, got: {msgs[0]}"

    # CRITICAL: expiry must exit at CURRENT price (cmp=741.50), NOT the SL
    # price (734.31). Guards the is_expired-overwrite bug where the SHORT
    # else-branch clobbered expiry exits with a bogus "SL Hit (intraday)".
    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"] == "CLOSED"]
    assert len(closed) == 1
    assert float(closed.iloc[0]["Exit_Price"]) == pytest.approx(741.50, abs=0.05), \
        f"Exit should be current price 741.50, got {closed.iloc[0]['Exit_Price']}"


def test_maxhold_swing_still_expires_after_5_days(test_env, monkeypatch):
    """Control: SWING_1d trades still expire on the 5-day MaxHold."""
    _set_time(monkeypatch, datetime(2026, 1, 10, 10, 0, 0))

    t = enter_trade("US", "QQQ", "LONG", 500.00, "Test QQQ SWING",
                    pattern_rank=46, expected_win_rate=62.5,
                    pattern_factors="Close>Open+2Red", tf="SWING_1d")
    assert t is not None

    # Advance 5 full days
    _set_time(monkeypatch, datetime(2026, 1, 15, 10, 0, 0))

    ohlc = {"QQQ": {"close": 505.00, "high": 510.00, "low": 498.00}}
    msgs = update_trades(ohlc)

    assert len(msgs) == 1, f"Expected swing expiry, got: {msgs}"
    assert "Expiry" in msgs[0]

    # CRITICAL: expiry must exit at CURRENT price (cmp=505.00), NOT the SL
    # price (490.00). Guards the is_expired-overwrite bug (else-branch
    # clobbered expiry with a bogus SL Hit at the SL price).
    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"] == "CLOSED"]
    assert len(closed) == 1
    assert float(closed.iloc[0]["Exit_Price"]) == pytest.approx(505.00, abs=0.05), \
        f"Exit should be current price 505.00, got {closed.iloc[0]['Exit_Price']}"


# ======================================================================
# Bug 3: live_pnl_updater enforces MaxHold expiry
# ======================================================================

def test_live_pnl_enforces_maxhold_expiry(test_env, monkeypatch):
    """Regression: live_pnl_updater force-closes positions past MaxHold.

    Real case: ^GSPC entered 01:14 IST was 0.6h over its 6h MaxHold and still
    open because only scheduled bot scans enforced expiry. Now live_pnl closes
    it at the next run.
    """
    import live_pnl_updater as lp

    tmp = test_env["tmp_path"]
    log_dir = tmp / "logs"
    monkeypatch.setattr(lp, "PAPER_FILE", str(log_dir / "paper_trades.csv"))
    monkeypatch.setattr(lp, "PORTFOLIO_FILE", str(log_dir / "portfolio.json"))
    monkeypatch.setattr(lp, "AUDIT_FILE", str(log_dir / "trade_audit.json"))
    monkeypatch.setattr(lp, "STRATEGY_STATS_FILE", str(log_dir / "strategy_stats.json"))
    monkeypatch.setattr(lp, "LIVE_STATE_FILE", str(log_dir / "live_pnl_state.json"))
    monkeypatch.setattr(lp, "LIVE_PNL_LOG", str(log_dir / "live_pnl_snapshots.csv"))

    # Freeze live_pnl clock at check time: 2026-01-16 01:14 IST
    class FrozenLpDT:
        _F = datetime(2026, 1, 16, 1, 14, 16)

        @classmethod
        def now(cls, tz=None):
            return IST.localize(cls._F) if tz is not None else cls._F

        @classmethod
        def strptime(cls, s, fmt):
            return datetime.strptime(s, fmt)

    monkeypatch.setattr(lp, "datetime", FrozenLpDT)

    # Portfolio with an OPEN position entered 18:00 IST the previous day
    # (7.2h before check time - past 6h MaxHold, but SL/TP NOT hit)
    open_pos = {
        "Date": "2026-01-15", "Time_IST": "18:00:00 IST", "Mode": "US",
        "Ticker": "SPY", "Direction": "LONG", "TimeFrame": "INTRADAY_1h",
        "Entry_Price": 741.73, "Qty": 100, "SL": 734.31, "Target": 756.56,
        "MaxHold": 6, "Exit_Price": "", "Exit_Time": "", "P&L": "", "P&L_%": "",
        "Status": "OPEN", "Pattern_Rank": 31, "Expected_WinRate": 63.16,
        "Pattern_Factors": "Price>SMA50+EMA20<EMA50+Close<Open",
        "Reason": "#31ID Price>SMA50+EMA20<EMA50+Close<Open", "Signal_Indicators": "",
    }
    portfolio = {
        "capital_by_market": {"INDIAN": 100000.0, "US": 100699.27, "CRYPTO": 100000.0, "INTRADAY": 88504.35},
        "open_positions": [open_pos],
        "closed_count": 15, "total_wins": 1, "total_losses": 14,
        "total_pnl": -10796.38,
        "total_pnl_by_market": {"INDIAN": 0.0, "US": 699.27, "CRYPTO": 0.0, "INTRADAY": -11495.65},
    }
    lp.save_portfolio(portfolio)

    # CSV with the same OPEN row
    df = pd.DataFrame([open_pos])
    df.to_csv(lp.PAPER_FILE, index=False)

    # Mock live OHLC fetch (valid data - no SL/TP trigger)
    def fake_fetch(ticker):
        return {"close": 741.50, "high": 742.00, "low": 740.50, "date": "2026-01-16"}

    monkeypatch.setattr(lp, "fetch_live_ohlc", fake_fetch)

    closed_msgs, _ = lp.process_open_trades()

    assert len(closed_msgs) == 1, f"Expected 1 expiry close, got: {closed_msgs}"
    assert "Expiry" in closed_msgs[0], f"Expected Expiry reason, got: {closed_msgs[0]}"

    # Verify persisted state: trade CLOSED, open_positions empty
    saved_csv = pd.read_csv(lp.PAPER_FILE, on_bad_lines="warn")
    assert len(saved_csv[saved_csv["Status"].astype(str) == "OPEN"]) == 0
    saved_port = json.loads(open(lp.PORTFOLIO_FILE).read())
    assert len(saved_port.get("open_positions", [])) == 0


# ======================================================================
# Bug 2b: SHORT MaxHold expiry also exits at current price (not overwritten)
# ======================================================================

def test_maxhold_short_expires_at_cmp(test_env, monkeypatch):
    """Regression: SHORT MaxHold expiry exits at current price (cmp).

    Before the is_expired fix, an expired SHORT trade's else (SHORT) branch
    could overwrite the expiry exit with a bogus "SL Hit (intraday)" when the
    daily high breached the SHORT SL — closing at the SL price instead of the
    current market price. This test pins the exit at cmp.
    """
    _set_time(monkeypatch, datetime(2026, 1, 15, 23, 22, 38))

    t = enter_trade("US", "IWM", "SHORT", 293.10, "Test IWM SHORT",
                    pattern_rank=14, expected_win_rate=63.41,
                    pattern_factors="Price<SMA50+EMA9>EMA20+EMA20<EMA50+Close>Open",
                    tf="INTRADAY_1h")
    assert t is not None, "Failed to enter test trade"

    # Advance to 06:00 IST next day (7h actual hold > 6h MaxHold). High is set
    # ABOVE the SHORT SL so the SHORT SL/TP branch would have matched and
    # overwritten the expiry exit before the fix.
    _set_time(monkeypatch, datetime(2026, 1, 16, 6, 0, 0))

    ohlc = {"IWM": {"close": 292.00, "high": 297.00, "low": 291.00}}
    msgs = update_trades(ohlc)

    assert len(msgs) == 1, f"Expected 1 expiry msg, got: {msgs}"
    assert "Expiry" in msgs[0], f"Expected Expiry reason, got: {msgs[0]}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"] == "CLOSED"]
    assert len(closed) == 1
    # Exit must be the current price (close=292.00), NOT the SHORT SL (~296).
    assert float(closed.iloc[0]["Exit_Price"]) == pytest.approx(292.00, abs=0.05), \
        f"SHORT expiry should exit at current price 292.00, got {closed.iloc[0]['Exit_Price']}"


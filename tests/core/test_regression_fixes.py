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

    # Advance to 23:00 IST next day. Session-time MaxHold: the 6h budget is
    # consumed only during US session hours (13:30-20:00 UTC weekdays). Entry
    # 23:22:38 IST = 17:52:38 UTC leaves 127.4 session-min on Jan-15; the
    # remaining 232.6 min run out at 17:22:38 UTC Jan-16 = 22:52:38 IST.
    # So 23:00 IST is past MaxHold (a wall-clock 6.6h check at 06:00 IST would
    # NOT have expired it — that was the old bug).
    _set_time(monkeypatch, datetime(2026, 1, 16, 23, 0, 0))

    ohlc = {"SPY": {"close": 741.50, "high": 742.00, "low": 740.50}}  # inside SL/TP band
    msgs = update_trades(ohlc)

    assert len(msgs) == 1, f"Expected 1 expiry msg, got: {msgs}"
    assert "Expiry" in msgs[0], f"Expected Expiry reason, got: {msgs[0]}"

    # CRITICAL: expiry must exit at CURRENT price (cmp=741.50), NOT the SL
    # price (734.31). Guards the is_expired-overwrite bug where the SHORT
    # else-branch clobbered expiry exits with a bogus "SL Hit (intraday)".
    # Exit gets LONG exit slippage (US intraday 0.02%) applied on top.
    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"] == "CLOSED"]
    assert len(closed) == 1
    assert float(closed.iloc[0]["Exit_Price"]) == pytest.approx(741.35, abs=0.05), \
        f"Exit should be current price 741.35, got {closed.iloc[0]['Exit_Price']}"


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
    # Exit gets LONG exit slippage (US swing 0.01%) applied on top.
    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"] == "CLOSED"]
    assert len(closed) == 1
    assert float(closed.iloc[0]["Exit_Price"]) == pytest.approx(504.95, abs=0.05), \
        f"Exit should be current price 504.95, got {closed.iloc[0]['Exit_Price']}"


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
    def fake_fetch(ticker, entry_dt=None):
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

    # Advance to 23:00 IST next day (past session-time MaxHold expiry at
    # 22:52:38 IST: entry 23:22 IST left 127.4 session-min on Jan-15 and the
    # remaining budget runs out mid Jan-16 session). High is set ABOVE the
    # SHORT SL so the SHORT SL/TP branch would have matched and overwritten
    # the expiry exit before the fix.
    _set_time(monkeypatch, datetime(2026, 1, 16, 23, 0, 0))

    ohlc = {"IWM": {"close": 292.00, "high": 297.00, "low": 291.00}}
    msgs = update_trades(ohlc)

    assert len(msgs) == 1, f"Expected 1 expiry msg, got: {msgs}"
    assert "Expiry" in msgs[0], f"Expected Expiry reason, got: {msgs[0]}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"] == "CLOSED"]
    assert len(closed) == 1
    # Exit must be the current price (close=292.00), NOT the SHORT SL (~296).
    # SHORT exit pays more: +0.02% intraday slippage on cmp.
    assert float(closed.iloc[0]["Exit_Price"]) == pytest.approx(292.06, abs=0.05), \
        f"SHORT expiry should exit at current price 292.06, got {closed.iloc[0]['Exit_Price']}"



# ======================================================================
# Bug 4: False SL/TP exits from pre-entry (stale) OHLC data
# ======================================================================
# Real case (2026-07-31): bot entered US intraday positions (SPY/^NDX/IWM) at
# 09:48 IST while the US market was CLOSED — entries at the prior (July-30)
# session close. The LIVE P&L updater then checked them against the July-30
# session's intraday lows (which happened BEFORE entry) and falsely stopped
# them out: ^NDX low=27691.48 < SL 27818.85, IWM low=288.96 < SL 289.59.
# A position cannot be stopped out by a price that occurred before it existed.

def _stale_open_pos():
    return {
        "Date": "2026-07-31", "Time_IST": "09:48:19 IST", "Mode": "US",
        "Ticker": "^NDX", "Direction": "LONG", "TimeFrame": "INTRADAY_1h",
        "Entry_Price": 28099.85, "Qty": 3, "SL": 27818.85, "Target": 28661.85,
        "MaxHold": 6, "Exit_Price": "", "Exit_Time": "", "P&L": "", "P&L_%": "",
        "Status": "OPEN", "Pattern_Rank": 6, "Expected_WinRate": 62.86,
        "Pattern_Factors": "Price>SMA20+Price<SMA50+EMA9>EMA20+EMA20<EMA50",
        "Reason": "#6ID Price>SMA20+Price<SMA50+EMA9>EMA20+EMA20<EMA50",
        "Signal_Indicators": "",
    }


def test_live_pnl_no_false_sl_on_pre_entry_low(test_env, monkeypatch):
    """Regression: live updater must NOT exit when the only bars are from
    BEFORE the entry (stale pre-entry low below SL)."""
    import live_pnl_updater as lp

    tmp = test_env["tmp_path"]
    log_dir = tmp / "logs"
    monkeypatch.setattr(lp, "PAPER_FILE", str(log_dir / "paper_trades.csv"))
    monkeypatch.setattr(lp, "PORTFOLIO_FILE", str(log_dir / "portfolio.json"))
    monkeypatch.setattr(lp, "AUDIT_FILE", str(log_dir / "trade_audit.json"))
    monkeypatch.setattr(lp, "STRATEGY_STATS_FILE", str(log_dir / "strategy_stats.json"))
    monkeypatch.setattr(lp, "LIVE_STATE_FILE", str(log_dir / "live_pnl_state.json"))
    monkeypatch.setattr(lp, "LIVE_PNL_LOG", str(log_dir / "live_pnl_snapshots.csv"))

    # Check time INSIDE the US processing window: 2026-07-31 20:37 IST =
    # 11:07 ET Fri. The per-market gate defers off-session US positions to
    # their next session (v5.26), so live-P&L behaviour must be tested at a
    # time the updater would actually process US positions.
    class FrozenLpDT:
        _F = datetime(2026, 7, 31, 20, 37, 0)

        @classmethod
        def now(cls, tz=None):
            return IST.localize(cls._F) if tz is not None else cls._F

        @classmethod
        def strptime(cls, s, fmt):
            return datetime.strptime(s, fmt)

    monkeypatch.setattr(lp, "datetime", FrozenLpDT)

    open_pos = _stale_open_pos()
    portfolio = {
        "capital_by_market": {"INDIAN": 100000.0, "US": 100699.27, "CRYPTO": 100000.0, "INTRADAY": 88654.05},
        "open_positions": [open_pos],
        "closed_count": 18, "total_wins": 1, "total_losses": 17,
        "total_pnl": -11786.53,
        "total_pnl_by_market": {"INDIAN": 0.0, "US": 699.27, "CRYPTO": 0.0, "INTRADAY": -12485.80},
    }
    lp.save_portfolio(portfolio)
    df = pd.DataFrame([open_pos])
    df.to_csv(lp.PAPER_FILE, index=False)

    # Stale July-30 data: close==entry, low 27691.48 is BELOW SL 27818.85 but
    # it happened BEFORE the July-31 entry → must NOT trigger a false SL exit.
    def fake_fetch(ticker, entry_dt=None):
        return {"close": 28099.85, "high": 28168.04, "low": 27691.48,
                "date": "2026-07-30", "has_post_entry": False}

    monkeypatch.setattr(lp, "fetch_live_ohlc", fake_fetch)

    closed_msgs, _ = lp.process_open_trades()

    assert len(closed_msgs) == 0, f"No exit expected (stale pre-entry low), got: {closed_msgs}"

    # Position must still be OPEN
    saved_csv = pd.read_csv(lp.PAPER_FILE, on_bad_lines="warn")
    assert len(saved_csv[saved_csv["Status"].astype(str) == "OPEN"]) == 1
    saved_port = json.loads(open(lp.PORTFOLIO_FILE).read())
    assert len(saved_port.get("open_positions", [])) == 1


def test_live_pnl_still_exits_on_post_entry_low(test_env, monkeypatch):
    """Regression: live updater STILL exits when the bar data is from AFTER
    the entry (has_post_entry=True) — the guard must not block real SL/TP."""
    import live_pnl_updater as lp

    tmp = test_env["tmp_path"]
    log_dir = tmp / "logs"
    monkeypatch.setattr(lp, "PAPER_FILE", str(log_dir / "paper_trades.csv"))
    monkeypatch.setattr(lp, "PORTFOLIO_FILE", str(log_dir / "portfolio.json"))
    monkeypatch.setattr(lp, "AUDIT_FILE", str(log_dir / "trade_audit.json"))
    monkeypatch.setattr(lp, "STRATEGY_STATS_FILE", str(log_dir / "strategy_stats.json"))
    monkeypatch.setattr(lp, "LIVE_STATE_FILE", str(log_dir / "live_pnl_state.json"))
    monkeypatch.setattr(lp, "LIVE_PNL_LOG", str(log_dir / "live_pnl_snapshots.csv"))

    # Check time INSIDE the US processing window: 2026-07-31 20:37 IST =
    # 11:07 ET Fri. The per-market gate defers off-session US positions to
    # their next session (v5.26), so live-P&L behaviour must be tested at a
    # time the updater would actually process US positions.
    class FrozenLpDT:
        _F = datetime(2026, 7, 31, 20, 37, 0)

        @classmethod
        def now(cls, tz=None):
            return IST.localize(cls._F) if tz is not None else cls._F

        @classmethod
        def strptime(cls, s, fmt):
            return datetime.strptime(s, fmt)

    monkeypatch.setattr(lp, "datetime", FrozenLpDT)

    open_pos = _stale_open_pos()
    portfolio = {
        "capital_by_market": {"INDIAN": 100000.0, "US": 100699.27, "CRYPTO": 100000.0, "INTRADAY": 88654.05},
        "open_positions": [open_pos],
        "closed_count": 18, "total_wins": 1, "total_losses": 17,
        "total_pnl": -11786.53,
        "total_pnl_by_market": {"INDIAN": 0.0, "US": 699.27, "CRYPTO": 0.0, "INTRADAY": -12485.80},
    }
    lp.save_portfolio(portfolio)
    df = pd.DataFrame([open_pos])
    df.to_csv(lp.PAPER_FILE, index=False)

    # Fresh same-day data (has_post_entry=True) where low breaches SL → real exit
    def fake_fetch(ticker, entry_dt=None):
        return {"close": 28099.85, "high": 28120.00, "low": 27800.00,
                "date": "2026-07-31", "has_post_entry": True}

    monkeypatch.setattr(lp, "fetch_live_ohlc", fake_fetch)

    closed_msgs, _ = lp.process_open_trades()

    assert len(closed_msgs) == 1, f"Expected 1 SL exit, got: {closed_msgs}"
    assert "SL Hit" in closed_msgs[0], f"Expected SL Hit, got: {closed_msgs[0]}"


def test_fetch_live_ohlc_filters_pre_entry_bars(test_env, monkeypatch):
    """Regression: fetch_live_ohlc must drop bars before entry_dt so a stale
    pre-entry low can never be returned as the daily low."""
    import live_pnl_updater as lp
    import pandas as pd
    from datetime import timedelta

    # Bars 03:30–04:20 UTC (pre-entry, low 27691.48) and 04:30–05:00 UTC
    # (post-entry, low 28000). Entry_dt 10:00 IST == 04:30 UTC, so the filter
    # must drop the pre-entry bars and keep only post-entry lows.
    idx = pd.date_range("2026-07-31 03:30", periods=10, freq="10min", tz="UTC")
    df = pd.DataFrame({
        "Open": [28100.0] * 10,
        "High": [28150.0] * 10,
        "Low": [27691.48] * 6 + [28000.0] * 4,
        "Close": [28099.85] * 10,
        "Volume": [1000] * 10,
    }, index=idx)

    monkeypatch.setattr(lp.market_data, "download", lambda *a, **k: df)

    entry_dt = IST.localize(datetime(2026, 7, 31, 10, 0, 0))
    ohlc = lp.fetch_live_ohlc("^NDX", entry_dt)

    assert ohlc is not None
    assert ohlc["has_post_entry"] is True
    # Low must be the post-entry low (28000), NOT the stale pre-entry 27691.48
    assert ohlc["low"] == pytest.approx(28000.00, abs=0.01),         f"Pre-entry low leaked into daily low: {ohlc['low']}"


def test_fetch_live_ohlc_no_post_entry_flag(test_env, monkeypatch):
    """Regression: when ALL bars precede entry (market closed since entry),
    fetch_live_ohlc must return has_post_entry=False so SL/TP is skipped."""
    import live_pnl_updater as lp
    import pandas as pd

    idx = pd.date_range("2026-07-30 13:30", periods=5, freq="1h", tz="UTC")
    df = pd.DataFrame({
        "Open": [28100.0] * 5,
        "High": [28150.0] * 5,
        "Low": [27691.48] * 5,
        "Close": [28099.85] * 5,
        "Volume": [1000] * 5,
    }, index=idx)

    monkeypatch.setattr(lp.market_data, "download", lambda *a, **k: df)

    entry_dt = IST.localize(datetime(2026, 7, 31, 9, 48, 19))
    ohlc = lp.fetch_live_ohlc("^NDX", entry_dt)

    assert ohlc is not None
    assert ohlc["has_post_entry"] is False, "Expected no post-entry bars"


def test_paper_trader_skips_sltp_on_stale_ohlc_date(test_env, monkeypatch):
    """Regression: paper_trader.update_trades must skip SL/TP when the OHLC
    data's date is BEFORE the position's entry date (stale pre-entry data)."""
    import paper_trader
    from paper_trader import enter_trade, update_trades

    _set_time(monkeypatch, datetime(2026, 7, 31, 9, 48, 19))

    t = enter_trade("US", "^NDX", "LONG", 28099.85, "Stale test",
                    pattern_rank=6, expected_win_rate=62.86,
                    pattern_factors="Price>SMA20+Price<SMA50+EMA9>EMA20+EMA20<EMA50",
                    tf="INTRADAY_1h")
    assert t is not None, "Failed to enter test trade"

    # ohlc data date (2026-07-30) < entry date (2026-07-31) → stale → no SL/TP exit
    ohlc = {"^NDX": {"close": 28099.85, "high": 28168.04, "low": 27691.48,
                     "date": "2026-07-30"}}
    msgs = update_trades(ohlc)
    assert len(msgs) == 0, f"No exit expected on stale data, got: {msgs}"

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    assert len(df[df["Status"] == "OPEN"]) == 1, "Trade must remain OPEN"


def test_paper_trader_still_exits_on_current_ohlc_date(test_env, monkeypatch):
    """Regression: paper_trader.update_trades STILL exits when the OHLC date
    matches the entry date (fresh same-day data) — guard must not block real SL/TP."""
    import paper_trader
    from paper_trader import enter_trade, update_trades

    _set_time(monkeypatch, datetime(2026, 7, 31, 9, 48, 19))

    t = enter_trade("US", "^NDX", "LONG", 28099.85, "Fresh test",
                    pattern_rank=6, expected_win_rate=62.86,
                    pattern_factors="Price>SMA20+Price<SMA50+EMA9>EMA20+EMA20<EMA50",
                    tf="INTRADAY_1h")
    assert t is not None, "Failed to enter test trade"

    # Same-day data (2026-07-31) where low breaches SL → real exit
    ohlc = {"^NDX": {"close": 28099.85, "high": 28120.00, "low": 27800.00,
                     "date": "2026-07-31"}}
    msgs = update_trades(ohlc)
    assert len(msgs) == 1, f"Expected 1 SL exit, got: {msgs}"
    assert "SL Hit" in msgs[0], f"Expected SL Hit, got: {msgs[0]}"



# ======================================================================
# Bug 5: MaxHold expired + stale pre-entry low -> Expiry close, NOT false SL
# ======================================================================
# Real case (2026-07-31): SPY entered 09:48 IST (US market closed, entry at
# July-30 close). LIVE updater's 1m window only has July-30 bars (pre-entry,
# stale). At 15:50 IST the position is past its 6h MaxHold (expiry 15:48).
# Even though the stale July-30 low (734.10) is BELOW SL (734.31), the exit
# must be MaxHold Expiry at current price (~741.73) — NOT a false "SL Hit".

def test_live_pnl_expired_stale_data_closes_at_cmp_not_sl(test_env, monkeypatch):
    """Regression (expired + stale combination): when a position is past MaxHold
    AND the only available data is stale/pre-entry (has_post_entry=False), it
    must close via Expiry at the current price — never at a stale pre-entry low
    below SL. Guards against SL/TP logic overwriting an expiry exit.

    NOTE: the pure stale-data guard (non-expired position) is pinned by
    test_live_pnl_no_false_sl_on_pre_entry_low; this test pins the combined
    ordering invariant (expiry exit must win even with stale data present)."""
    import live_pnl_updater as lp

    tmp = test_env["tmp_path"]
    log_dir = tmp / "logs"
    monkeypatch.setattr(lp, "PAPER_FILE", str(log_dir / "paper_trades.csv"))
    monkeypatch.setattr(lp, "PORTFOLIO_FILE", str(log_dir / "portfolio.json"))
    monkeypatch.setattr(lp, "AUDIT_FILE", str(log_dir / "trade_audit.json"))
    monkeypatch.setattr(lp, "STRATEGY_STATS_FILE", str(log_dir / "strategy_stats.json"))
    monkeypatch.setattr(lp, "LIVE_STATE_FILE", str(log_dir / "live_pnl_state.json"))
    monkeypatch.setattr(lp, "LIVE_PNL_LOG", str(log_dir / "live_pnl_snapshots.csv"))

    # Check time: 01:10 IST on Aug-01. Session-time MaxHold: entry 09:48:19 IST
    # = 04:18 UTC is PRE-MARKET (US market closed), so the 6h budget only
    # starts at the 13:30 UTC open and expires 13:30+6h = 19:30 UTC =
    # 01:00 IST Aug-01. 01:10 IST is past MaxHold. (The old wall-clock expiry
    # at 15:48 IST — while the market was still closed — was the bug.)
    class FrozenLpDT:
        _F = datetime(2026, 8, 1, 1, 10, 0)

        @classmethod
        def now(cls, tz=None):
            return IST.localize(cls._F) if tz is not None else cls._F

        @classmethod
        def strptime(cls, s, fmt):
            return datetime.strptime(s, fmt)

    monkeypatch.setattr(lp, "datetime", FrozenLpDT)

    open_pos = {
        "Date": "2026-07-31", "Time_IST": "09:48:19 IST", "Mode": "US",
        "Ticker": "SPY", "Direction": "LONG", "TimeFrame": "INTRADAY_1h",
        "Entry_Price": 741.73, "Qty": 119, "SL": 734.31, "Target": 756.56,
        "MaxHold": 6, "Exit_Price": "", "Exit_Time": "", "P&L": "", "P&L_%": "",
        "Status": "OPEN", "Pattern_Rank": 31, "Expected_WinRate": 63.16,
        "Pattern_Factors": "Price>SMA50+EMA20<EMA50+Close<Open",
        "Reason": "#31ID Price>SMA50+EMA20<EMA50+Close<Open", "Signal_Indicators": "",
    }
    portfolio = {
        "capital_by_market": {"INDIAN": 100000.0, "US": 100699.27, "CRYPTO": 100000.0, "INTRADAY": 88654.05},
        "open_positions": [open_pos],
        "closed_count": 19, "total_wins": 1, "total_losses": 18,
        "total_pnl": -12646.67,
        "total_pnl_by_market": {"INDIAN": 0.0, "US": 699.27, "CRYPTO": 0.0, "INTRADAY": -13345.94},
    }
    lp.save_portfolio(portfolio)
    df = pd.DataFrame([open_pos])
    df.to_csv(lp.PAPER_FILE, index=False)

    # Stale July-30 data: low 734.10 is BELOW SL 734.31 but pre-entry → has_post_entry=False
    def fake_fetch(ticker, entry_dt=None):
        return {"close": 741.73, "high": 742.45, "low": 734.10,
                "date": "2026-07-30", "has_post_entry": False}

    monkeypatch.setattr(lp, "fetch_live_ohlc", fake_fetch)

    closed_msgs, _ = lp.process_open_trades()

    assert len(closed_msgs) == 1, f"Expected 1 Expiry close, got: {closed_msgs}"
    assert "Expiry" in closed_msgs[0], f"Expected Expiry reason, got: {closed_msgs[0]}"
    assert "SL Hit" not in closed_msgs[0], f"False SL exit! got: {closed_msgs[0]}"

    # Exit must be at current price (~741.73), NOT the SL (734.31)
    saved_csv = pd.read_csv(lp.PAPER_FILE, on_bad_lines="warn")
    closed = saved_csv[saved_csv["Status"].astype(str) == "CLOSED"]
    assert len(closed) == 1
    exit_px = float(closed.iloc[0]["Exit_Price"])
    assert exit_px == pytest.approx(741.58, abs=0.05), \
        f"Expiry should exit at current price 741.58, got {exit_px}"
    assert "Expiry" in str(closed.iloc[0]["Reason"])

    # Position removed from portfolio
    # NOTE: portfolio.json is rebuilt entirely from paper_trades.csv (single
    # source of truth) so its counters reflect the CSV, not the old incremental
    # counts. The test CSV contains exactly this one row, so closed_count==1.
    saved_port = json.loads(open(lp.PORTFOLIO_FILE).read())
    assert len(saved_port.get("open_positions", [])) == 0
    assert saved_port.get("closed_count") == 1


# ======================================================================
# Bug 6: paper_trader bar-level SL/TP (post-entry filter)
# ======================================================================
# paper_trader.update_trades() previously received only aggregate
# {close, high, low, date}, so a SAME-DAY bar before the entry candle could
# falsely stop out a mid-session position (documented "KNOWN LIMITATION").
# bot.py now supplies full `bars`; update_trades evaluates SL/TP first-touch
# only on bars at/after the entry time within the session-time live window.

def test_paper_trader_bars_ignore_pre_entry_low(test_env, monkeypatch):
    """Regression: a bar BEFORE the entry candle (low below SL) must be ignored."""
    _set_time(monkeypatch, datetime(2026, 1, 15, 23, 22, 38))

    t = enter_trade("US", "SPY", "LONG", 741.73, "Test SPY LONG",
                    pattern_rank=31, expected_win_rate=63.16,
                    pattern_factors="Price>SMA50+EMA20<EMA50+Close<Open",
                    tf="INTRADAY_1h")
    assert t is not None

    # Advance into the next morning (market still closed, US session not open).
    _set_time(monkeypatch, datetime(2026, 1, 16, 1, 14, 16))

    # Entry 23:22:38 IST Jan-15 = 17:52:38 UTC. The 14:00 UTC bar (low 734.00,
    # below SL 734.31) happened BEFORE entry → must be ignored. The 18:00 UTC
    # post-entry bar is inside the live window and does not hit SL/TP.
    bars = [
        (pd.Timestamp("2026-01-15 14:00:00", tz="UTC"), 742.00, 734.00, 741.00),
        (pd.Timestamp("2026-01-15 18:00:00", tz="UTC"), 743.00, 738.00, 741.50),
    ]
    ohlc = {"SPY": {"close": 741.50, "high": 743.00, "low": 736.00,
                    "date": "2026-01-16", "bars": bars}}
    msgs = update_trades(ohlc)
    assert len(msgs) == 0, f"No exit expected (pre-entry low ignored), got: {msgs}"

    port = load_portfolio()
    assert len(port.get("open_positions", [])) == 1, "Trade should still be OPEN"


def test_paper_trader_bars_exit_on_post_entry_sl(test_env, monkeypatch):
    """Control: a POST-entry bar hitting SL does trigger the exit at SL."""
    _set_time(monkeypatch, datetime(2026, 1, 15, 23, 22, 38))

    t = enter_trade("US", "SPY", "LONG", 741.73, "Test SPY LONG",
                    pattern_rank=31, expected_win_rate=63.16,
                    pattern_factors="Price>SMA50+EMA20<EMA50+Close<Open",
                    tf="INTRADAY_1h")
    assert t is not None

    _set_time(monkeypatch, datetime(2026, 1, 16, 1, 14, 16))

    bars = [
        (pd.Timestamp("2026-01-15 14:00:00", tz="UTC"), 742.00, 734.00, 741.00),   # pre-entry: ignored
        (pd.Timestamp("2026-01-15 18:00:00", tz="UTC"), 743.00, 733.50, 741.00),   # post-entry: SL hit
    ]
    ohlc = {"SPY": {"close": 741.00, "high": 743.00, "low": 733.50,
                    "date": "2026-01-16", "bars": bars}}
    msgs = update_trades(ohlc)
    assert len(msgs) == 1, f"Expected SL exit, got: {msgs}"
    assert "SL" in msgs[0]

    df = pd.read_csv(paper_trader.PAPER_FILE, on_bad_lines="warn")
    closed = df[df["Status"] == "CLOSED"]
    assert len(closed) == 1
    assert float(closed.iloc[0]["Exit_Price"]) == pytest.approx(734.16, abs=0.05), \
        f"Exit should be at SL 734.16, got {closed.iloc[0]['Exit_Price']}"

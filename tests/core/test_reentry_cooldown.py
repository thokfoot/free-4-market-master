"""
Regression tests for the re-entry cooldown guard (2026-08-11).

Confirmed production loss: the gap-down scanner re-entered the SAME 7
tickers 4 minutes after they were stopped out at expiry — all 7 hit SL
again on the next scan (+Rs 7,400 needless loss on top of the expiry
losses). Fix: check_entry_allowed() now blocks same-ticker re-entry for
GAP_DOWN_1m / INTRADAY_1h within a cooldown window (config:
GAP_DOWN_REENTRY_COOLDOWN_MINUTES / INTRADAY_REENTRY_COOLDOWN_MINUTES).
Swing re-entries remain allowed — a fresh next-day signal is legitimate.
"""

import os

import pandas as pd

import paper_trader as pt
from paper_trader import COLUMNS, check_entry_allowed, enter_trade


def _seed_row(paper_file, ticker, direction, tf, exit_time, status="CLOSED"):
    """Append a trade row directly to the paper CSV (bypasses enter/exit)."""
    row = {c: "" for c in COLUMNS}
    row.update({
        "Date": "2026-01-15",
        "Time_IST": "10:00:00 IST",
        "Mode": "INDIAN" if tf == "GAP_DOWN_1m" else "US",
        "Ticker": ticker,
        "Direction": direction,
        "TimeFrame": tf,
        "Entry_Price": 100.0,
        "Qty": 10,
        "SL": 99.5,
        "Target": 101.0,
        "MaxHold": 5,
        "Exit_Price": 99.6 if status == "CLOSED" else "",
        "Exit_Time": exit_time if status == "CLOSED" else "",
        "P&L": -4.0 if status == "CLOSED" else "",
        "P&L_%": -0.4 if status == "CLOSED" else "",
        "Status": status,
        "Pattern_Rank": 997,
        "Expected_WinRate": 75.0,
        "Pattern_Factors": "f_gap_down + f_52wk_low",
        "Reason": "test",
        "Signal_Indicators": "",
    })
    df_new = pd.DataFrame([row])[COLUMNS]
    if os.path.exists(paper_file):
        df_old = pd.read_csv(paper_file, on_bad_lines="warn")
        for col in COLUMNS:
            if col in df_old.columns:
                df_old[col] = df_old[col].astype(object)
        df_combined = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_combined = df_new
    df_combined.to_csv(paper_file, index=False)


def test_gap_down_reentry_blocked_within_cooldown(test_env):
    """Closed 5 min ago (frozen now = 10:30 IST) → same-ticker re-entry blocked."""
    _seed_row(pt.PAPER_FILE, "RECLTD.NS", "LONG", "GAP_DOWN_1m",
              "2026-01-15 10:25:00 IST")
    reason = check_entry_allowed("RECLTD.NS", "LONG", tf="GAP_DOWN_1m")
    assert reason is not None and "Cooldown" in reason


def test_gap_down_reentry_allowed_after_cooldown_window(test_env):
    """Closed 3h ago → outside the 120-min window → allowed."""
    _seed_row(pt.PAPER_FILE, "RECLTD.NS", "LONG", "GAP_DOWN_1m",
              "2026-01-15 07:30:00 IST")
    assert check_entry_allowed("RECLTD.NS", "LONG", tf="GAP_DOWN_1m") is None


def test_gap_down_different_ticker_not_blocked(test_env):
    """Cooldown is per-ticker; other tickers are unaffected."""
    _seed_row(pt.PAPER_FILE, "ABFRL.NS", "LONG", "GAP_DOWN_1m",
              "2026-01-15 10:25:00 IST")
    assert check_entry_allowed("RECLTD.NS", "LONG", tf="GAP_DOWN_1m") is None


def test_gap_down_opposite_direction_not_blocked(test_env):
    """Cooldown is per-direction; opposite side on same ticker is allowed."""
    _seed_row(pt.PAPER_FILE, "ABFRL.NS", "LONG", "GAP_DOWN_1m",
              "2026-01-15 10:25:00 IST")
    assert check_entry_allowed("ABFRL.NS", "SHORT", tf="GAP_DOWN_1m") is None


def test_swing_reentry_never_blocked_by_cooldown(test_env):
    """Swing re-entry after a stop is a legitimate fresh signal — never blocked."""
    _seed_row(pt.PAPER_FILE, "SPY", "LONG", "SWING_1d",
              "2026-01-15 10:25:00 IST")
    assert check_entry_allowed("SPY", "LONG", tf="SWING_1d") is None
    # Legacy callers that don't pass tf must also remain unblocked
    assert check_entry_allowed("SPY", "LONG") is None


def test_intraday_reentry_blocked_within_cooldown(test_env):
    """INTRADAY_1h same-ticker re-entry within the window is blocked too."""
    _seed_row(pt.PAPER_FILE, "QQQ", "LONG", "INTRADAY_1h",
              "2026-01-15 10:25:00 IST")
    reason = check_entry_allowed("QQQ", "LONG", tf="INTRADAY_1h")
    assert reason is not None and "Cooldown" in reason


def test_enter_trade_enforces_cooldown_for_gap_down(test_env):
    """The gatekeeper itself (enter_trade) must reject a cooldown re-entry."""
    _seed_row(pt.PAPER_FILE, "RECLTD.NS", "LONG", "GAP_DOWN_1m",
              "2026-01-15 10:25:00 IST")
    t = enter_trade("INDIAN", "RECLTD.NS", "LONG", 342.92, "Test gapdown",
                    pattern_rank=997, expected_win_rate=75.0,
                    pattern_factors="f_gap_down + f_52wk_low",
                    tf="GAP_DOWN_1m", sl_override=341.72, tp_override=346.18,
                    max_hold_override=5)
    assert t is None, "gap-down re-entry within cooldown must be rejected"


def test_enter_trade_allows_gap_down_after_cooldown(test_env):
    """After the window, the same ticker can be traded again."""
    _seed_row(pt.PAPER_FILE, "RECLTD.NS", "LONG", "GAP_DOWN_1m",
              "2026-01-15 07:30:00 IST")
    t = enter_trade("INDIAN", "RECLTD.NS", "LONG", 342.92, "Test gapdown",
                    pattern_rank=997, expected_win_rate=75.0,
                    pattern_factors="f_gap_down + f_52wk_low",
                    tf="GAP_DOWN_1m", sl_override=341.72, tp_override=346.18,
                    max_hold_override=5)
    assert t is not None, "gap-down re-entry after cooldown must be allowed"

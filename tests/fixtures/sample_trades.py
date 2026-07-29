"""
Reusable trade dict factories.

Each factory returns a dict matching the COLUMNS schema from paper_trader.py.
All values are deterministic — no randomness, no datetime.now() dependency.

Covers all 9 combinations: LONG/SHORT × SWING/INTRADAY × India/US/Crypto

Usage:
    from tests.fixtures.sample_trades import long_swing_us_trade
    trade = long_swing_us_trade()
"""

from copy import deepcopy
from paper_trader import COLUMNS

_FROZEN_DATE = "2026-01-15"
_FROZEN_TIME = "10:30:00 IST"

# ── Base Trade Template ────────────────────────────────────────

_BASE_TRADE = {
    "Date": _FROZEN_DATE,
    "Time_IST": _FROZEN_TIME,
    "Mode": "US",
    "Ticker": "SPY",
    "Direction": "LONG",
    "TimeFrame": "SWING_1d",
    "Entry_Price": 450.00,
    "Qty": 100,
    "SL": 441.00,
    "Target": 468.00,
    "MaxHold": 5,
    "Exit_Price": "",
    "Exit_Time": "",
    "P&L": "",
    "P&L_%": "",
    "Status": "OPEN",
    "Pattern_Rank": "46",
    "Expected_WinRate": "62.5",
    "Pattern_Factors": "Price>SMA50+2Red",
    "Reason": "#46SW Price>SMA50+2Red",
}


# ======================================================================
# SWING TRADES (1d timeframe)
# ======================================================================

def long_swing_us_trade(**overrides):
    """Long swing US (e.g. SPY @ 450, SL 441, TP 468, Qty 100)."""
    t = deepcopy(_BASE_TRADE)
    t.update(overrides)
    return t


def short_swing_us_trade(**overrides):
    """Short swing US (e.g. QQQ @ 500, SL 510, TP 480, Qty 100)."""
    t = deepcopy(_BASE_TRADE)
    t.update({
        "Ticker": "QQQ",
        "Direction": "SHORT",
        "Entry_Price": 500.00,
        "SL": 510.00,
        "Target": 480.00,
        "MaxHold": 5,
        "Pattern_Rank": "30",
        "Reason": "#30SW Price<SMA50+EMA9>EMA20",
    })
    t.update(overrides)
    return t


def long_swing_india_trade(**overrides):
    """Long swing India (e.g. ^BSESN @ 75,000, SL 73,500, TP 78,000, Qty 13)."""
    t = deepcopy(_BASE_TRADE)
    t.update({
        "Mode": "INDIAN",
        "Ticker": "^BSESN",
        "Direction": "LONG",
        "Entry_Price": 75000.00,
        "SL": 73500.00,
        "Target": 78000.00,
        "Qty": 13,
        "MaxHold": 5,
        "Pattern_Rank": "12",
        "Reason": "#12SW EMA20>EMA50+Close>Open",
    })
    t.update(overrides)
    return t


def short_swing_crypto_trade(**overrides):
    """Short swing crypto (e.g. ADA-USD @ 0.35, SL 0.357, TP 0.336, Qty 10,000)."""
    t = deepcopy(_BASE_TRADE)
    t.update({
        "Mode": "CRYPTO",
        "Ticker": "ADA-USD",
        "Direction": "SHORT",
        "Entry_Price": 0.35,
        "SL": 0.357,
        "Target": 0.336,
        "Qty": 10000,
        "MaxHold": 5,
        "Pattern_Rank": "52",
        "Expected_WinRate": "61.33",
        "Reason": "#52SW EMA9>EMA20+EMA20<EMA50+Range>1.5%",
    })
    t.update(overrides)
    return t


# ======================================================================
# INTRADAY TRADES (1h timeframe)
# ======================================================================

def long_intraday_us_trade(**overrides):
    """Long intraday US (e.g. SPY @ 741, SL 733.59, TP 755.82, Qty 133)."""
    t = deepcopy(_BASE_TRADE)
    t.update({
        "TimeFrame": "INTRADAY_1h",
        "Ticker": "SPY",
        "Direction": "LONG",
        "Entry_Price": 741.00,
        "SL": 733.59,
        "Target": 755.82,
        "Qty": 133,
        "MaxHold": 6,
        "Pattern_Rank": "32",
        "Expected_WinRate": "67.05",
        "Pattern_Factors": "Price>SMA20+Price<SMA50+EMA9>EMA20+EMA20<EMA50",
        "Reason": "#32ID Price>SMA20+Price<SMA50+EMA9>EMA20+EMA20<EMA50",
    })
    t.update(overrides)
    return t


def short_intraday_us_trade(**overrides):
    """Short intraday US (e.g. IWM @ 225, SL 229.5, TP 220.5, Qty 444)."""
    t = deepcopy(_BASE_TRADE)
    t.update({
        "TimeFrame": "INTRADAY_1h",
        "Ticker": "IWM",
        "Direction": "SHORT",
        "Entry_Price": 225.00,
        "SL": 229.50,
        "Target": 220.50,
        "Qty": 444,
        "MaxHold": 6,
        "Pattern_Rank": "8",
        "Expected_WinRate": "64.44",
        "Pattern_Factors": "Price>SMA20+Range>1.5%",
        "Reason": "#8ID Price>SMA20+Range>1.5%",
    })
    t.update(overrides)
    return t


def long_intraday_crypto_trade(**overrides):
    """Long intraday crypto (e.g. ETH-USD @ 3,200, SL 3,152, TP 3,296, Qty 31)."""
    t = deepcopy(_BASE_TRADE)
    t.update({
        "Mode": "CRYPTO",
        "TimeFrame": "INTRADAY_1h",
        "Ticker": "ETH-USD",
        "Direction": "LONG",
        "Entry_Price": 3200.00,
        "SL": 3152.00,
        "Target": 3296.00,
        "Qty": 31,
        "MaxHold": 12,
        "Pattern_Rank": "15",
        "Expected_WinRate": "60.38",
        "Pattern_Factors": "Price<SMA20+Price>SMA50+EMA9<EMA20+Range>1.5%",
        "Reason": "#15ID Price<SMA20+Price>SMA50+EMA9<EMA20+Range>1.5%",
    })
    t.update(overrides)
    return t


# ======================================================================
# CLOSED TRADE HELPERS (used for regression tests)
# ======================================================================

def closed_us_trade(pnl: float = 50.0, **overrides):
    """Return a CLOSED trade dict with given P&L."""
    t = long_swing_us_trade()
    t.update({
        "Status": "CLOSED",
        "Exit_Price": str(t["Entry_Price"] + (pnl / t["Qty"])),
        "Exit_Time": _FROZEN_TIME,
        "P&L": str(pnl),
        "P&L_%": str(round(pnl / (t["Entry_Price"] * t["Qty"]) * 100, 2)),
        "Reason": t["Reason"] + " | SL Hit (intraday)",
    })
    t.update(overrides)
    return t


def closed_us_loss_trade(**overrides):
    """Return a CLOSED trade with a loss (P&L = -50)."""
    return closed_us_trade(pnl=-50.0, **overrides)


# ======================================================================
# CSV ROW FACTORY (for update_trades data from pd.read_csv)
# ======================================================================

def row_from_trade(trade: dict) -> dict:
    """Convert a trade dict to a pandas row-like dict (all strings/floats)."""
    return {k: trade[k] for k in COLUMNS}

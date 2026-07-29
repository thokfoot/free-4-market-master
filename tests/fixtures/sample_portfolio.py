"""
Reusable portfolio state factories.

Each factory returns a dict matching the portfolio.json schema from paper_trader.py.
All values are deterministic with known capital allocations.

Usage:
    from tests.fixtures.sample_portfolio import empty_portfolio, portfolio_with_open_trade
    port = empty_portfolio()
"""

from copy import deepcopy
from tests.fixtures.sample_trades import (
    long_swing_us_trade,
    short_swing_us_trade,
    long_intraday_us_trade,
)

_INITIAL_CAPITAL = 100000.0


def empty_portfolio(**overrides) -> dict:
    """Portfolio with no open positions, zero P&L, full capital intact."""
    port = {
        "capital_by_market": {
            "INDIAN": _INITIAL_CAPITAL,
            "US": _INITIAL_CAPITAL,
            "CRYPTO": _INITIAL_CAPITAL,
            "INTRADAY": _INITIAL_CAPITAL,
        },
        "open_positions": [],
        "closed_count": 0,
        "total_wins": 0,
        "total_losses": 0,
        "total_pnl": 0,
        "total_pnl_by_market": {
            "INDIAN": 0.0,
            "US": 0.0,
            "CRYPTO": 0.0,
            "INTRADAY": 0.0,
        },
    }
    port.update(overrides)
    return port


def portfolio_with_one_swing_long(**overrides) -> dict:
    """Portfolio with 1 open US swing LONG position. Capital intact (not debited until exit)."""
    trade = long_swing_us_trade()
    port = empty_portfolio()
    port["open_positions"] = [trade]
    port.update(overrides)
    return port


def portfolio_with_one_intraday_long(**overrides) -> dict:
    """Portfolio with 1 open intraday US LONG position."""
    trade = long_intraday_us_trade()
    port = empty_portfolio()
    port["open_positions"] = [trade]
    port.update(overrides)
    return port


def portfolio_with_mixed_positions(**overrides) -> dict:
    """Portfolio with 2 swing LONG + 1 intraday SHORT open."""
    swing1 = long_swing_us_trade()
    swing2 = long_swing_us_trade(
        Ticker="QQQ", Entry_Price=500.00, SL=490.00, Target=520.00, Qty=200,
        Pattern_Rank="30", Reason="#30SW Price<SMA50+EMA9>EMA20",
    )
    intra = long_intraday_us_trade(
        Ticker="IWM", Direction="SHORT", Entry_Price=225.00, SL=229.50, Target=220.50, Qty=444,
        Pattern_Rank="8", Reason="#8ID Price>SMA20+Range>1.5%",
    )
    port = empty_portfolio()
    port["open_positions"] = [swing1, swing2, intra]
    port.update(overrides)
    return port


def portfolio_with_full_swing_concurrent(**overrides) -> dict:
    """Portfolio with 5 open swing positions (at max concurrent limit)."""
    trades = [
        long_swing_us_trade(Ticker="SPY"),
        long_swing_us_trade(Ticker="QQQ", Entry_Price=500.00, SL=490.00, Target=520.00, Qty=200),
        long_swing_us_trade(Ticker="IWM", Entry_Price=225.00, SL=220.50, Target=234.00, Qty=444),
        long_swing_us_trade(Ticker="DIA", Entry_Price=350.00, SL=343.00, Target=364.00, Qty=285),
        long_swing_us_trade(Ticker="^GSPC", Entry_Price=5000.00, SL=4900.00, Target=5200.00, Qty=20),
    ]
    # Apply overrides to ALL trades after construction (avoid duplicate kwarg collision)
    if overrides:
        for t in trades:
            t.update(overrides)
    port = empty_portfolio()
    port["open_positions"] = trades
    port.update(overrides)
    return port


def portfolio_with_history(wins: int = 3, losses: int = 2, total_pnl: float = 250.0, **overrides) -> dict:
    """Portfolio with closed trade history (wins, losses, closed_count reflected)."""
    port = empty_portfolio()
    port["closed_count"] = wins + losses
    port["total_wins"] = wins
    port["total_losses"] = losses
    port["total_pnl"] = total_pnl
    # Distribute PnL proportionally across markets
    port["total_pnl_by_market"]["US"] = total_pnl
    port.update(overrides)
    return port


def old_format_portfolio(**overrides) -> dict:
    """Old format portfolio.json with single 'capital' key — for migration test."""
    port = {
        "capital": 300000.0,
        "open_positions": [],
        "closed_count": 5,
        "total_wins": 3,
        "total_losses": 2,
        "total_pnl": 150.0,
    }
    port.update(overrides)
    return port

"""
FREE 3-Market v5.10 — PROFESSIONAL PAPER TRADING SYSTEM
======================================================
Author: Finance Manager
Config: Centralized constants — change here, all modules pick up.

Strategy Source: CSV-V3-FINAL.csv (50 verified patterns, 5-year backtest)
Indicators: adjust=False for EMA & Wilder's RSI (TradingView-matched)
"""

import os

# ===== CAPITAL & RISK =====
# Per-market capital allocation: ₹1,00,000 each → ₹3,00,000 total
CAPITAL_BY_MARKET = {
    "INDIAN": 100000.0,
    "US": 100000.0,
    "CRYPTO": 100000.0,
}

# Intraday gets separate ₹1L (defined BEFORE TOTAL_CAPITAL to avoid NameError)
INTRADAY_CAPITAL = 100000.0
# FADE (NSE 1h Big-Player-Exit) gets its OWN ₹1L bucket (v5.13)
FADE_CAPITAL = 100000.0

TOTAL_CAPITAL = sum(CAPITAL_BY_MARKET.values()) + INTRADAY_CAPITAL + FADE_CAPITAL  # ₹5,00,000
CAPITAL = TOTAL_CAPITAL  # For backward compatibility
RISK_PER_TRADE = 0.01           # 1% risk per trade
SL_PCT = 0.02                   # 2% stop loss
TP_PCT = 0.04                   # 4% take profit
MAX_HOLD_DAYS = 5               # Max hold days per trade (matches backtest)
MAX_CONCURRENT = 100            # Total active positions allowed (all TFs/markets).
                                # High cap so every fired signal is paper-entered
                                # for long-run strategy evaluation, with a sane
                                # ceiling to keep the portfolio manageable.
# NOTE: No consecutive-loss cooldown / re-entry-gap limits. Paper trade only —
# every fired signal is entered so each strategy's real long-run performance can
# be measured without loss-based entry caps distorting results.

# ===== STRATEGY FILE =====
STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "data", "strategies.csv")

# ===== QUANTITY CAPS =====
# Maximum position size per trade based on entry price tier.
# Prevents excessively large positions in low-priced assets.
# These were previously hardcoded in paper_trader.py calculate_qty().
# Moved here in v5.9 for easy configuration.
CAP_MAX_QTY_ULTRA_LOW = 50000   # entry < 0.1  (penny stocks, low-cap crypto)
CAP_MAX_QTY_LOW       = 10000   # entry < 1    (sub-dollar crypto, cheap stocks)
CAP_MAX_QTY_HIGH      = 5000    # entry > 100  (expensive stocks like GOOGL, MRF)

# ===== SLIPPAGE MODEL =====
# Realistic fill slippage as a fraction of price per trade.
# Applied to both entry and exit prices — makes paper P&L more realistic.
# Set to 0.0 for ideal fills (no slippage).
# Recommended values (based on market liquidity):
#   US ETFs: 0.0001-0.0005  (0.01-0.05% — very liquid)
#   US Indices: 0.0002-0.001 (0.02-0.1%)
#   Indian stocks: 0.0005-0.002 (0.05-0.2%)
#   Crypto: 0.001-0.003 (0.1-0.3% — less liquid)
# NOTE: Values match strategy_miner backtest cost assumptions (US 0.01%, Crypto 0.1%)
SLIPPAGE_PCT = {
    "INDIAN": 0.0005,   # 0.05% — low end of recommended 0.05-0.2%
    "US": 0.0001,       # 0.01% — matches miner backtest slippage assumption
    "CRYPTO": 0.001,    # 0.10% — matches miner backtest slippage assumption
}

# Intraday slippage (typically wider due to 1h candle execution)
INTRADAY_SLIPPAGE_PCT = {
    "US": 0.0002,       # 0.02% — slightly wider for 1h candle execution
    "CRYPTO": 0.001,    # 0.10% — matches miner backtest slippage assumption
    "INDIAN": 0.0005,   # 0.05%
}

# ===== GAP-DOWN STRATEGY (Indian 1m Intraday, v5.9+) =====
# Two mean-reversion strategies for Indian stocks:
#   A: f_gap_down + f_52wk_low → SL 0.3%, TP 1.0%, 5min hold
#   B: f_gap_down (single)     → SL 0.5%, TP 1.0%, 5min hold
# Data: 1m OHLCV via yf.Ticker().history() (NOT yf.download())

# Data download settings
GAP_DOWN_PERIOD_DAYS = 7        # Days of 1m data to download
GAP_DOWN_MIN_DATA = 60           # Minimum 1m candles needed for factor calc
GAP_DOWN_MAX_HOLD_MINUTES = 5    # Max hold time per trade

# Strategy A: f_gap_down + f_52wk_low (higher confidence)
GAP_DOWN_A_SL_PCT = 0.003        # 0.3% stop loss
GAP_DOWN_A_TP_PCT = 0.010         # 1.0% take profit

# Strategy B: f_gap_down single factor (more frequent)
GAP_DOWN_B_SL_PCT = 0.005        # 0.5% stop loss
GAP_DOWN_B_TP_PCT = 0.010         # 1.0% take profit

# Gap-down crash protection: if N+ stocks gap down simultaneously,
# it's a market-wide event (budget, election, global crash).
# Skip ALL entries in that run to prevent repeated SL hits.
GAP_DOWN_MAX_SIGNALS_PER_RUN = 8

# Strategy rank IDs for stats tracking and loss guard
# These go in Pattern_Rank column so strategy_stats and consecutive_loss_guard work.
GAP_DOWN_RANK_A = 997   # f_gap_down + f_52wk_low
GAP_DOWN_RANK_B = 998   # f_gap_down (single)

# Re-entry cooldowns (2026-08-11): block ANY same-ticker GAP_DOWN_1m /
# INTRADAY_1h re-entry within this window after a stop-out/expiry — not
# only 'after a stop'. Prevents re-entering a falling knife minutes after
# a close (the same 7 gap-down tickers re-entered 4 min after expiry all
# SL'd again, +Rs 7,400). A legit same-day re-gap hours later is also
# delayed — that trade-off is intentional and tunable here.
GAP_DOWN_REENTRY_COOLDOWN_MINUTES = 120
INTRADAY_REENTRY_COOLDOWN_MINUTES = 120

# ===== NSE FADE STRATEGY FAMILY (v5.15, 7.5-year clean-OOS verified, 35 variants) =====
# "Big Player Exit Fade": stock shoots up fast with volume + RSI -> SHORT it
# (big-player exit). 35 verified variants (2015-2022, 828 NSE stocks, strict
# train/test split):
#   S1-S10  = other-AI TOP 10 verified on 7.5yr OOS (all 8/8 yrs positive)
#   G1-G25  = grid-search combos (3.5-4% shoot + day-high), test net +16..+29/mo
# Signal (no lookahead): rolling-low shoot over dur_min, volume mult, RSI, gap,
# optional day-high filter, IST time window. Entry at signal candle close.
# Each variant has its own rank (900-934) for stats + circuit breaker.
FADE_UNIVERSE_FILE = os.path.join(os.path.dirname(__file__), "data", "nse_fade_universe.csv")

FADE_VARIANTS = [
    {
        "key": "S1", "rank": 900, "interval": "15m", "period": "60d",
        "shoot_pct": 2.0, "dur_min": 45, "vol_mult": 2.2, "rsi_min": 75.0,
        "gap_max": 0.8, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 39.38, "trades_count": 3182,
        "factors": "Fade S1: 15m +2.0%/45m vol2.2x RSI75 gap0.8",
        "name": "Fade S1 15m 2.0%/45m",
    },
    {
        "key": "S2", "rank": 901, "interval": "15m", "period": "60d",
        "shoot_pct": 2.0, "dur_min": 45, "vol_mult": 3.0, "rsi_min": 75.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 40.54, "trades_count": 4714,
        "factors": "Fade S2: 15m +2.0%/45m vol3.0x RSI75",
        "name": "Fade S2 15m 2.0%/45m",
    },
    {
        "key": "S3", "rank": 902, "interval": "15m", "period": "60d",
        "shoot_pct": 3.0, "dur_min": 90, "vol_mult": 3.0, "rsi_min": 70.0,
        "gap_max": 0.8, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.036, "dh": False,
        "max_per_day": 5, "win_rate": 42.84, "trades_count": 2801,
        "factors": "Fade S3: 15m +3.0%/90m vol3.0x RSI70 gap0.8",
        "name": "Fade S3 15m 3.0%/90m",
    },
    {
        "key": "S4", "rank": 903, "interval": "15m", "period": "60d",
        "shoot_pct": 3.0, "dur_min": 90, "vol_mult": 2.0, "rsi_min": 65.0,
        "gap_max": 1.0, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 45.41, "trades_count": 3299,
        "factors": "Fade S4: 15m +3.0%/90m vol2.0x RSI65 gap1.0",
        "name": "Fade S4 15m 3.0%/90m",
    },
    {
        "key": "S5", "rank": 904, "interval": "5m", "period": "60d",
        "shoot_pct": 1.5, "dur_min": 90, "vol_mult": 1.5, "rsi_min": 65.0,
        "gap_max": 0.5, "win": "1030_1300", "sl_pct": 0.007, "tp_pct": 0.021, "dh": True,
        "max_per_day": 5, "win_rate": 41.68, "trades_count": 2373,
        "factors": "Fade S5: 5m +1.5%/90m vol1.5x RSI65 gap0.5 dayhigh",
        "name": "Fade S5 5m 1.5%/90m",
    },
    {
        "key": "S6", "rank": 905, "interval": "15m", "period": "60d",
        "shoot_pct": 2.5, "dur_min": 60, "vol_mult": 2.5, "rsi_min": 70.0,
        "gap_max": 0.8, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 41.53, "trades_count": 3207,
        "factors": "Fade S6: 15m +2.5%/60m vol2.5x RSI70 gap0.8",
        "name": "Fade S6 15m 2.5%/60m",
    },
    {
        "key": "S7", "rank": 906, "interval": "15m", "period": "60d",
        "shoot_pct": 2.0, "dur_min": 60, "vol_mult": 2.0, "rsi_min": 70.0,
        "gap_max": 1.0, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 42.12, "trades_count": 3369,
        "factors": "Fade S7: 15m +2.0%/60m vol2.0x RSI70 gap1.0",
        "name": "Fade S7 15m 2.0%/60m",
    },
    {
        "key": "S8", "rank": 907, "interval": "15m", "period": "60d",
        "shoot_pct": 3.0, "dur_min": 60, "vol_mult": 2.5, "rsi_min": 70.0,
        "gap_max": 0.8, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 43.76, "trades_count": 3117,
        "factors": "Fade S8: 15m +3.0%/60m vol2.5x RSI70 gap0.8",
        "name": "Fade S8 15m 3.0%/60m",
    },
    {
        "key": "S9", "rank": 908, "interval": "5m", "period": "60d",
        "shoot_pct": 2.0, "dur_min": 90, "vol_mult": 2.0, "rsi_min": 70.0,
        "gap_max": 0.5, "win": "1030_1300", "sl_pct": 0.008, "tp_pct": 0.024, "dh": False,
        "max_per_day": 5, "win_rate": 42.48, "trades_count": 2253,
        "factors": "Fade S9: 5m +2.0%/90m vol2.0x RSI70 gap0.5",
        "name": "Fade S9 5m 2.0%/90m",
    },
    {
        "key": "S10", "rank": 909, "interval": "15m", "period": "60d",
        "shoot_pct": 2.0, "dur_min": 30, "vol_mult": 2.2, "rsi_min": 75.0,
        "gap_max": 0.8, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.025, "dh": False,
        "max_per_day": 5, "win_rate": 41.29, "trades_count": 3652,
        "factors": "Fade S10: 15m +2.0%/30m vol2.2x RSI75 gap0.8",
        "name": "Fade S10 15m 2.0%/30m",
    },
    {
        "key": "G1", "rank": 910, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 30, "vol_mult": 1.5, "rsi_min": 65.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.036000000000000004, "dh": True,
        "max_per_day": 5, "win_rate": 43.46, "trades_count": 1553,
        "factors": "Fade G1: 15m +4.0%/30m vol1.5x RSI65 dayhigh",
        "name": "Fade G1 15m 4.0%/30m",
    },
    {
        "key": "G2", "rank": 911, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 30, "vol_mult": 2.5, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.006999999999999999, "tp_pct": 0.0175, "dh": True,
        "max_per_day": 5, "win_rate": 44.39, "trades_count": 2300,
        "factors": "Fade G2: 15m +4.0%/30m vol2.5x RSI60 dayhigh",
        "name": "Fade G2 15m 4.0%/30m",
    },
    {
        "key": "G3", "rank": 912, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 30, "vol_mult": 2.0, "rsi_min": 70.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 39.77, "trades_count": 2122,
        "factors": "Fade G3: 15m +4.0%/30m vol2.0x RSI70",
        "name": "Fade G3 15m 4.0%/30m",
    },
    {
        "key": "G4", "rank": 913, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 30, "vol_mult": 1.5, "rsi_min": 70.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.015, "tp_pct": 0.0375, "dh": True,
        "max_per_day": 5, "win_rate": 43.73, "trades_count": 1443,
        "factors": "Fade G4: 15m +4.0%/30m vol1.5x RSI70 dayhigh",
        "name": "Fade G4 15m 4.0%/30m",
    },
    {
        "key": "G5", "rank": 914, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 30, "vol_mult": 1.5, "rsi_min": 70.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": True,
        "max_per_day": 5, "win_rate": 38.93, "trades_count": 1716,
        "factors": "Fade G5: 15m +4.0%/30m vol1.5x RSI70 dayhigh",
        "name": "Fade G5 15m 4.0%/30m",
    },
    {
        "key": "G6", "rank": 915, "interval": "15m", "period": "60d",
        "shoot_pct": 2.5, "dur_min": 30, "vol_mult": 2.5, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.006999999999999999, "tp_pct": 0.021, "dh": True,
        "max_per_day": 5, "win_rate": 40.31, "trades_count": 2168,
        "factors": "Fade G6: 15m +2.5%/30m vol2.5x RSI60 dayhigh",
        "name": "Fade G6 15m 2.5%/30m",
    },
    {
        "key": "G7", "rank": 916, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 45, "vol_mult": 2.0, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.015, "tp_pct": 0.03, "dh": True,
        "max_per_day": 5, "win_rate": 43.98, "trades_count": 1537,
        "factors": "Fade G7: 15m +4.0%/45m vol2.0x RSI60 dayhigh",
        "name": "Fade G7 15m 4.0%/45m",
    },
    {
        "key": "G8", "rank": 917, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 30, "vol_mult": 3.0, "rsi_min": 70.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.03, "dh": True,
        "max_per_day": 5, "win_rate": 42.81, "trades_count": 1628,
        "factors": "Fade G8: 15m +3.5%/30m vol3.0x RSI70 dayhigh",
        "name": "Fade G8 15m 3.5%/30m",
    },
    {
        "key": "G9", "rank": 918, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 30, "vol_mult": 2.0, "rsi_min": 70.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 42.86, "trades_count": 1939,
        "factors": "Fade G9: 15m +3.5%/30m vol2.0x RSI70",
        "name": "Fade G9 15m 3.5%/30m",
    },
    {
        "key": "G10", "rank": 919, "interval": "15m", "period": "60d",
        "shoot_pct": 3.0, "dur_min": 30, "vol_mult": 2.0, "rsi_min": 65.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.025, "dh": True,
        "max_per_day": 5, "win_rate": 42.09, "trades_count": 1834,
        "factors": "Fade G10: 15m +3.0%/30m vol2.0x RSI65 dayhigh",
        "name": "Fade G10 15m 3.0%/30m",
    },
    {
        "key": "G11", "rank": 920, "interval": "15m", "period": "60d",
        "shoot_pct": 3.0, "dur_min": 30, "vol_mult": 2.5, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.015, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 47.68, "trades_count": 1831,
        "factors": "Fade G11: 15m +3.0%/30m vol2.5x RSI60",
        "name": "Fade G11 15m 3.0%/30m",
    },
    {
        "key": "G12", "rank": 921, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 30, "vol_mult": 2.0, "rsi_min": 65.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.02, "dh": False,
        "max_per_day": 5, "win_rate": 46.09, "trades_count": 2415,
        "factors": "Fade G12: 15m +3.5%/30m vol2.0x RSI65",
        "name": "Fade G12 15m 3.5%/30m",
    },
    {
        "key": "G13", "rank": 922, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 30, "vol_mult": 1.5, "rsi_min": 60.0,
        "gap_max": 1.0, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 42.24, "trades_count": 1553,
        "factors": "Fade G13: 15m +3.5%/30m vol1.5x RSI60 gap1.0",
        "name": "Fade G13 15m 3.5%/30m",
    },
    {
        "key": "G14", "rank": 923, "interval": "15m", "period": "60d",
        "shoot_pct": 3.0, "dur_min": 30, "vol_mult": 3.0, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.006999999999999999, "tp_pct": 0.021, "dh": False,
        "max_per_day": 5, "win_rate": 38.58, "trades_count": 2553,
        "factors": "Fade G14: 15m +3.0%/30m vol3.0x RSI60",
        "name": "Fade G14 15m 3.0%/30m",
    },
    {
        "key": "G15", "rank": 924, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 45, "vol_mult": 1.5, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.025, "dh": False,
        "max_per_day": 5, "win_rate": 41.18, "trades_count": 2183,
        "factors": "Fade G15: 15m +4.0%/45m vol1.5x RSI60",
        "name": "Fade G15 15m 4.0%/45m",
    },
    {
        "key": "G16", "rank": 925, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 30, "vol_mult": 2.5, "rsi_min": 70.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.024, "dh": True,
        "max_per_day": 5, "win_rate": 43.93, "trades_count": 1819,
        "factors": "Fade G16: 15m +3.5%/30m vol2.5x RSI70 dayhigh",
        "name": "Fade G16 15m 3.5%/30m",
    },
    {
        "key": "G17", "rank": 926, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 30, "vol_mult": 3.0, "rsi_min": 75.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 39.35, "trades_count": 1977,
        "factors": "Fade G17: 15m +3.5%/30m vol3.0x RSI75",
        "name": "Fade G17 15m 3.5%/30m",
    },
    {
        "key": "G18", "rank": 927, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 45, "vol_mult": 1.5, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.036000000000000004, "dh": False,
        "max_per_day": 5, "win_rate": 41.48, "trades_count": 1707,
        "factors": "Fade G18: 15m +3.5%/45m vol1.5x RSI60",
        "name": "Fade G18 15m 3.5%/45m",
    },
    {
        "key": "G19", "rank": 928, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 90, "vol_mult": 2.0, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.03, "dh": True,
        "max_per_day": 5, "win_rate": 42.59, "trades_count": 1512,
        "factors": "Fade G19: 15m +3.5%/90m vol2.0x RSI60 dayhigh",
        "name": "Fade G19 15m 3.5%/90m",
    },
    {
        "key": "G20", "rank": 929, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 60, "vol_mult": 2.5, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.01, "tp_pct": 0.03, "dh": False,
        "max_per_day": 5, "win_rate": 38.84, "trades_count": 2011,
        "factors": "Fade G20: 15m +4.0%/60m vol2.5x RSI60",
        "name": "Fade G20 15m 4.0%/60m",
    },
    {
        "key": "G21", "rank": 930, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 45, "vol_mult": 2.5, "rsi_min": 60.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.006999999999999999, "tp_pct": 0.021, "dh": False,
        "max_per_day": 5, "win_rate": 37.55, "trades_count": 2474,
        "factors": "Fade G21: 15m +4.0%/45m vol2.5x RSI60",
        "name": "Fade G21 15m 4.0%/45m",
    },
    {
        "key": "G22", "rank": 931, "interval": "15m", "period": "60d",
        "shoot_pct": 3.0, "dur_min": 30, "vol_mult": 2.5, "rsi_min": 65.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.006999999999999999, "tp_pct": 0.0175, "dh": True,
        "max_per_day": 5, "win_rate": 41.98, "trades_count": 2244,
        "factors": "Fade G22: 15m +3.0%/30m vol2.5x RSI65 dayhigh",
        "name": "Fade G22 15m 3.0%/30m",
    },
    {
        "key": "G23", "rank": 932, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 60, "vol_mult": 2.0, "rsi_min": 65.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.012, "tp_pct": 0.03, "dh": True,
        "max_per_day": 5, "win_rate": 39.67, "trades_count": 1525,
        "factors": "Fade G23: 15m +4.0%/60m vol2.0x RSI65 dayhigh",
        "name": "Fade G23 15m 4.0%/60m",
    },
    {
        "key": "G24", "rank": 933, "interval": "15m", "period": "60d",
        "shoot_pct": 4.0, "dur_min": 60, "vol_mult": 1.5, "rsi_min": 65.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.006999999999999999, "tp_pct": 0.021, "dh": True,
        "max_per_day": 5, "win_rate": 36.55, "trades_count": 1899,
        "factors": "Fade G24: 15m +4.0%/60m vol1.5x RSI65 dayhigh",
        "name": "Fade G24 15m 4.0%/60m",
    },
    {
        "key": "G25", "rank": 934, "interval": "15m", "period": "60d",
        "shoot_pct": 3.5, "dur_min": 60, "vol_mult": 3.0, "rsi_min": 65.0,
        "gap_max": None, "win": "0930_1500", "sl_pct": 0.006999999999999999, "tp_pct": 0.021, "dh": False,
        "max_per_day": 5, "win_rate": 37.59, "trades_count": 2405,
        "factors": "Fade G25: 15m +3.5%/60m vol3.0x RSI65",
        "name": "Fade G25 15m 3.5%/60m",
    },
]

# Backward-compat aliases (S1 = live bot defaults)
FADE_PERIOD = FADE_VARIANTS[0]["period"]
FADE_INTERVAL = FADE_VARIANTS[0]["interval"]
FADE_SHOOT_PCT = FADE_VARIANTS[0]["shoot_pct"]
FADE_VOL_MULT = FADE_VARIANTS[0]["vol_mult"]
FADE_RSI_MIN = FADE_VARIANTS[0]["rsi_min"]
FADE_GAP_MAX = FADE_VARIANTS[0]["gap_max"]
FADE_SL_PCT = FADE_VARIANTS[0]["sl_pct"]
FADE_TP_PCT = FADE_VARIANTS[0]["tp_pct"]
FADE_MAX_TRADES_PER_DAY = FADE_VARIANTS[0]["max_per_day"]
FADE_MAX_HOLD_HOURS = 5      # intraday: exit by 15:00 IST if no SL/TP
FADE_RANK = FADE_VARIANTS[0]["rank"]
FADE_ALLOW_SHORT = True      # fade is inherently SHORT; paper trade simulates it
FADE_MIN_PRICE = 8.0         # skip penny stocks (< Rs 5) — matches backtest
# NOTE: India cash market has no intraday shorting — this is a PAPER-TRADE
# simulation of the validated edge; real execution needs F&O/hedged access.
# ===== CIRCUIT BREAKER (per-strategy loss guard, v5.11) =====
# A strategy that loses CIRCUIT_BREAKER_MAX_CONSEC_LOSSES trades in a row is
# auto-paused from NEW entries. It resumes when ANY of these happen:
#   - it records its next WIN (an open position exits profitable)
#   - CIRCUIT_BREAKER_COOLDOWN_DAYS days have passed since the pause
#   - resume_strategy(rank) is called manually
# The consecutive-loss counter is FORWARD-LOOKING: it starts at 0 when this
# ships and counts only losses from that point on (historical losses -- e.g.
# the infra-broken gap-down day -- are deliberately NOT counted, so those
# strategies get a clean trial protected by this safety net).
CIRCUIT_BREAKER_ENABLED = True
CIRCUIT_BREAKER_MAX_CONSEC_LOSSES = 5
CIRCUIT_BREAKER_COOLDOWN_DAYS = 2

# NOTE: pause state is keyed by strategy rank only (the same bucket
# strategy_stats.json uses). If one rank number exists in both SWING and
# INTRADAY files, a pause applies to both. This is the documented trade-off
# of the existing rank-only strategy identity model.


# Indian stock universe for gap-down scanning
# NIFTY 50 + NIFTY NEXT 50 + BANKNIFTY = 97 tickers
INDIAN_TICKERS = [
    # NIFTY 50
    "RELIANCE.NS", "TCS.NS", "INFY.NS", "HDFCBANK.NS", "ICICIBANK.NS",
    "SBIN.NS", "BHARTIARTL.NS", "ITC.NS", "LT.NS", "KOTAKBANK.NS",
    "BAJFINANCE.NS", "HINDUNILVR.NS", "ASIANPAINT.NS", "AXISBANK.NS",
    "MARUTI.NS", "TITAN.NS", "SUNPHARMA.NS", "ULTRACEMCO.NS", "WIPRO.NS",
    "NTPC.NS", "HCLTECH.NS", "POWERGRID.NS", "ONGC.NS", "M&M.NS",
    "BAJAJFINSV.NS", "ADANIENT.NS", "ADANIPORTS.NS", "COALINDIA.NS",
    "HDFCLIFE.NS", "SBILIFE.NS", "TATASTEEL.NS", "JSWSTEEL.NS",
    "HINDALCO.NS", "GRASIM.NS", "CIPLA.NS", "DRREDDY.NS", "DIVISLAB.NS",
    "EICHERMOT.NS", "BAJAJ-AUTO.NS", "HEROMOTOCO.NS", "BRITANNIA.NS",
    "NESTLEIND.NS", "TATACONSUM.NS", "APOLLOHOSP.NS", "BPCL.NS", "UPL.NS",
    "TECHM.NS", "INDUSINDBK.NS", "TATAMOTORS.NS",
    # NIFTY NEXT 50
    "VEDL.NS", "SAIL.NS", "NMDC.NS", "BANKBARODA.NS", "PNB.NS", "CANBK.NS",
    "IDFCFIRSTB.NS", "FEDERALBNK.NS", "AUBANK.NS", "BANDHANBNK.NS",
    "INDIGO.NS", "ZOMATO.NS", "DMART.NS", "TRENT.NS", "ABFRL.NS",
    "PIDILITIND.NS", "BERGEPAINT.NS", "CUMMINSIND.NS", "ASHOKLEY.NS",
    "MOTHERSON.NS", "TVSMOTOR.NS", "BAJAJHLDNG.NS", "CHOLAFIN.NS",
    "MUTHOOTFIN.NS", "PFC.NS", "RECLTD.NS", "IRCTC.NS", "IRFC.NS", "HAL.NS",
    "BEL.NS", "BDL.NS", "MAZDOCK.NS", "COCHINSHIP.NS", "RVNL.NS", "IDEA.NS",
    "TATAPOWER.NS", "ADANIGREEN.NS", "ADANIENSOL.NS", "SUZLON.NS",
    "KPITTECH.NS", "PERSISTENT.NS", "COFORGE.NS", "MPHASIS.NS", "LTTS.NS",
    "TATAELXSI.NS", "OFSS.NS", "LTIM.NS", "POLYCAB.NS", "KEI.NS",
    # Indices
    "^NSEI", "^NSEBANK",
]

# ===== INTRADAY SETTINGS (v5.7+) =====
INTRADAY_STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "data", "intraday_strategies.csv")


# Intraday SL/TP per market (tighter than swing)
INTRADAY_SL_PCT = {
    "US": 0.01,      # 1% stop loss
    "CRYPTO": 0.015,  # 1.5% stop loss
    "INDIAN": 0.01,   # 1% stop loss (if India intraday added)
}
INTRADAY_TP_PCT = {
    "US": 0.02,      # 2% take profit
    "CRYPTO": 0.03,    # 3% take profit
    "INDIAN": 0.02,   # 2% take profit
}
INTRADAY_MAX_HOLD_HOURS = {
    "US": 6,         # Max 6 hours hold for US intraday
    "CRYPTO": 12,     # Max 12 hours for crypto (24/7 market)
    "INDIAN": 5,      # Max 5 hours for India intraday
}
INTRADAY_PERIOD = "3mo"      # 3 months of 1h data from yfinance
INTRADAY_INTERVAL = "1h"     # 1-hour candles

# ===== SCAN SETTINGS =====
# Bot runs scans for all markets. SHORT configurable per region.
ALLOW_SHORT = {
    "US": True,
    "CRYPTO": True,
    "INDIAN": False,  # India cash market NO SHORT
}

# ===== TRADING COSTS (Round Turn — entry + exit) =====
# From V3 verified analysis
CHARGES_PER_MARKET = {
    "INDIAN": 0.0012,     # 0.12% RT — Zerodha: Brokerage 0.03% + STT 0.01% + Exchange 0.003% + GST 18% + Stamp 0.003% + slippage
    "US": 0.0002,          # 0.02% RT — $0 commission + SEC 0.0008% + FINRA 0.000145% + Exchange 0.003% + slippage 0.01%
    "CRYPTO": 0.003,       # 0.30% RT — Binance 0.1% per side (0.2%) + slippage 0.05% per side (0.1%)
}

# ===== YAHOO FINANCE =====
YF_PERIOD = "2y"                # Download 2 years for indicator computation
YF_INTERVAL = "1d"              # Daily data

# ===== TELEGRAM =====
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN") or os.getenv("BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID") or os.getenv("CHAT_ID")

# ===== TICKER MAPPING =====
# CSV Market Name -> Yahoo Finance Ticker
# Used to download live data for each unique underlying
TICKER_MAP = {
    # Crypto
    "ADA": "ADA-USD",
    "AVAX": "AVAX-USD",
    "BTC": "BTC-USD",
    "BNB": "BNB-USD",
    "DOGE": "DOGE-USD",
    "ETH": "ETH-USD",
    "LINK": "LINK-USD",
    "SOL": "SOL-USD",
    "TRX": "TRX-USD",
    "XRP": "XRP-USD",
    # US ETFs
    "QQQ": "QQQ",
    "SPY": "SPY",
    "DIA": "DIA",
    "IWM": "IWM",
    # US Indices
    "Nasdaq100": "^NDX",
    "SP500": "^GSPC",
    "PHLX_Semi": "^SOX",
    "NYSE_Comp": "^NYA",
    "SP400": "^SP400",
    "Russell2000": "IWM",
    "Russell1000": "IWB",
    "SP100": "OEF",
    # US Sectors
    "XLI": "XLI",
    "XLC": "XLC",
    "XLRE": "XLRE",
    "XLF": "XLF",
    "XLK": "XLK",
    "XLE": "XLE",
    "XLB": "XLB",
    "XLV": "XLV",
    "XLY": "XLY",
    "XLP": "XLP",
    "XLU": "XLU",
    "IBB": "IBB",
    "KBW": "^BKX",
    # US Dow
    "Dow_Jones": "DIA",
    "Dow_Trans": "^DJT",
    "Dow_Util": "^DJU",
    # India
    "Bank Nifty": "^NSEBANK",
    "Sensex": "^BSESN",
    "Nifty 50": "^NSEI",
    "GIFT Nifty": "^NSEI",  # GIFT = SGX Nifty, use NSE as proxy
}

# Identity mappings: allow daily-swing strategies to reference each Indian
# stock / index / crypto coin directly by its yfinance ticker in the CSV
# Market column (added with the INDIAN + CRYPTO new-strategy deployment).
for _tk in INDIAN_TICKERS + [
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
    "ADA-USD", "DOGE-USD", "AVAX-USD", "LINK-USD", "TRX-USD",
]:
    TICKER_MAP.setdefault(_tk, _tk)

# ===== REGION INFERENCE =====
# If ticker ends with -USD, it's crypto
# If ticker starts with ^, it's India or US index
# If ticker is in US ETF list, it's US
CRYPTO_SUFFIX = "-USD"
INDIA_PREFIXES = ("^NSE", "^BSESN")

def get_region(yf_ticker: str, csv_region: str = None) -> str:
    """
    Infer region from ticker or CSV region column.
    
    Returns normalized region name: "CRYPTO", "US", or "INDIAN"
    (all uppercase, matching CHARGES_PER_MARKET, INTRADAY_SL_PCT, etc.)
    """
    if csv_region:
        region_upper = csv_region.upper()
        if region_upper == "INDIA":
            return "INDIAN"
        if region_upper in ("CRYPTO", "US"):
            return region_upper
    if yf_ticker.endswith(CRYPTO_SUFFIX):
        return "CRYPTO"
    if yf_ticker.startswith(INDIA_PREFIXES):
        return "INDIAN"
    return "US"


# ===== HOLIDAY CALENDAR =====
# Known market holidays for India (NSE) and US (NYSE)
# Format: "MM-DD" — year-independent
INDIAN_HOLIDAYS = {
    "01-26",  # Republic Day
    "03-25",  # Holi (approx)
    "03-29",  # Good Friday (approx)
    "04-14",  # Dr Ambedkar Jayanti
    "04-17",  # Ram Navami (approx)
    "05-01",  # Maharashtra Day / Labour Day
    "06-17",  # Bakri Eid (approx)
    "07-17",  # Muharram (approx)
    "08-15",  # Independence Day
    "08-27",  # Ganesh Chaturthi (approx)
    "10-02",  # Gandhi Jayanti
    "10-12",  # Dussehra (approx)
    "10-31",  # Diwali (approx)
    "11-01",  # Diwali Balipratipada (approx)
    "11-15",  # Guru Nanak Jayanti
    "12-25",  # Christmas
}
US_HOLIDAYS = {
    "01-01",  # New Year's Day
    "01-15",  # Martin Luther King Jr. Day
    "02-19",  # Presidents' Day
    "03-29",  # Good Friday
    "05-27",  # Memorial Day
    "06-19",  # Juneteenth
    "07-04",  # Independence Day
    "09-02",  # Labor Day
    "11-28",  # Thanksgiving
    "12-25",  # Christmas
}

# ===== MARKET HOURS =====
import pytz
from datetime import datetime, time as dtime, timedelta

IST_TZ = pytz.timezone("Asia/Kolkata")
ET_TZ = pytz.timezone("US/Eastern")


def get_market_status(now_ist: datetime = None) -> dict:
    """
    Return market status for all three markets at the given IST time.
    
    Returns:
        {
            "INDIAN": "OPEN" | "CLOSED" | "HOLIDAY" | "WEEKEND",
            "US": "OPEN" | "CLOSED" | "HOLIDAY" | "WEEKEND",
            "CRYPTO": "24/7",
            "message": "🇮🇳 India OPEN | 🇺🇸 US CLOSED | ₿ Crypto 24/7"
        }
    """
    if now_ist is None:
        now_ist = datetime.now(IST_TZ)
    
    date_str = now_ist.strftime("%m-%d")
    weekday = now_ist.weekday()  # 0=Mon, 6=Sun
    hour = now_ist.hour
    minute = now_ist.minute
    current_ist_minutes = hour * 60 + minute
    
    result = {}
    
    # Crypto — always 24/7
    result["CRYPTO"] = "24/7"
    
    # India market
    if weekday >= 5:  # Sat/Sun
        result["INDIAN"] = "WEEKEND"
    elif date_str in INDIAN_HOLIDAYS:
        result["INDIAN"] = "HOLIDAY"
    elif 555 <= current_ist_minutes <= 930:  # 9:15 AM to 3:30 PM IST
        result["INDIAN"] = "OPEN"
    elif current_ist_minutes < 555:  # Before 9:15 AM
        result["INDIAN"] = "PRE-OPEN"
    else:  # After 3:30 PM
        result["INDIAN"] = "CLOSED"
    
    # US market (ET)
    # Convert IST to ET: IST = ET + 9:30 (during EST) or + 10:30 (during EDT)
    # Rough: US market 9:30 AM - 4:00 PM ET
    if weekday >= 5:  # Sat/Sun
        result["US"] = "WEEKEND"
    elif date_str in US_HOLIDAYS:
        result["US"] = "HOLIDAY"
    else:
        # Convert IST to ET approximation
        # During EDT (Mar-Nov): ET = IST - 9:30
        # During EST (Nov-Mar): ET = IST - 10:30
        # Rough check: 7:00 PM IST = 9:30 AM ET (EDT), 1:30 AM IST = 4:00 PM ET (EDT)
        us_open_minutes = 19 * 60 + 0  # 7:00 PM IST = 9:30 AM ET
        us_close_minutes = 1 * 60 + 30  # 1:30 AM IST = 4:00 PM ET
        
        if us_open_minutes <= current_ist_minutes or current_ist_minutes < us_close_minutes:
            # Handles overnight session (after 7PM IST → before 1:30AM IST next day)
            result["US"] = "OPEN"
        else:
            result["US"] = "CLOSED"
    
    # Build human-readable message
    status_icons = {
        "OPEN": "🟢 OPEN",
        "CLOSED": "🔴 CLOSED",
        "HOLIDAY": "🎉 HOLIDAY",
        "WEEKEND": "⛔ WEEKEND",
        "PRE-OPEN": "🟡 PRE-OPEN",
        "24/7": "🟢 24/7",
    }
    parts = []
    for mkt in ["INDIAN", "US", "CRYPTO"]:
        icon = status_icons.get(result[mkt], result[mkt])
        parts.append(f"{mkt[:4]} {icon}")
    result["message"] = " | ".join(parts)
    
    return result


# ===== INDICATOR FORMULAS (Documentation) =====
# SMA20  = Close.rolling(20).mean()
# SMA50  = Close.rolling(50).mean()
# EMA9   = Close.ewm(span=9, adjust=False).mean()
# EMA20  = Close.ewm(span=20, adjust=False).mean()
# EMA50  = Close.ewm(span=50, adjust=False).mean()
# RSI14  = Wilder's RSI: gain.ewm(alpha=1/14, adjust=False).mean()
# Range  = (High - Low) / Close
# Ret    = Close.pct_change()
# 2Red   = Ret < 0 & Ret.shift(1) < 0
# Next_Ret = Close.shift(-1) / Close - 1

"""
FREE 4-Market v4.4 — MARKET-WISE STRATEGY CONFIG
=================================================
Centralized configuration file with MARKET-SPECIFIC parameters.
Change parameters here and all modules pick them up automatically.

Backtest Results (2-year, market-wise):
  INDIAN: SL=3% TP=9% Hold=7d Score>=2 MaxCon=2 -> PnL +1,006 (breakeven)
  US:     SL=5% TP=15% Hold=14d Score>=2 MaxCon=5 -> PnL +6,713
  CRYPTO: SL=7% TP=14% Hold=21d Score>=3 MaxCon=2 -> PnL +10,558

Author: Finance Manager — DO NOT MODIFY WITHOUT AUTHORIZATION
"""

# ===== CAPITAL & RISK =====
INITIAL_CAPITAL = 100000.0       # Starting capital for paper trading
RISK_PER_TRADE = 0.01            # 1% risk per trade
BROKERAGE_PCT = 0.001            # 0.1% per trade (STT + brokerage)
SLIPPAGE_PCT = 0.001             # 0.1% slippage on entry/exit

# ===== GLOBAL STRATEGY =====
STRATEGY_MODE = "LONG"           # "LONG" or "BOTH" (SHORT unreliable per backtest)
SCAN_INDIAN = True
SCAN_US = True
SCAN_CRYPTO = True

# ===== DATA =====
YF_PERIOD = "1y"                 # yfinance data period (need >50 days for indicators)
YF_INTERVAL = "1d"               # Daily data

# ===== MARKET-SPECIFIC PARAMETERS =====
# Each market has its own SL, TP, Hold, Score, Concurrent optimized separately.

MARKET_PARAMS = {
    "INDIAN": {
        "SL_PCT": 0.03,          # 3% stop loss
        "TP_PCT": 0.09,          # 9% take profit
        "MAX_HOLD_DAYS": 7,       # 7 day max hold
        "MIN_SCORE": 2,           # Minimum signal score
        "MAX_CONCURRENT": 2,      # Max simultaneous trades
        "MONITOR_SHORT": False,   # No SHORT for Indian
    },
    "US": {
        "SL_PCT": 0.05,          # 5% stop loss (wider for US volatility)
        "TP_PCT": 0.15,          # 15% take profit
        "MAX_HOLD_DAYS": 14,      # 14 day max hold
        "MIN_SCORE": 2,           # Minimum signal score
        "MAX_CONCURRENT": 5,      # 5 concurrent (more opportunities)
        "MONITOR_SHORT": False,
    },
    "CRYPTO": {
        "SL_PCT": 0.07,          # 7% stop loss (crypto very volatile)
        "TP_PCT": 0.14,          # 14% take profit
        "MAX_HOLD_DAYS": 21,      # 21 day max hold (trends last longer)
        "MIN_SCORE": 3,           # Higher score for crypto
        "MAX_CONCURRENT": 2,      # Max concurrent
        "MONITOR_SHORT": False,
    },
}

# ===== FALLBACK (used if market not found) =====
DEFAULT_SL_PCT = 0.04
DEFAULT_TP_PCT = 0.08
DEFAULT_HOLD_DAYS = 14
DEFAULT_MIN_SCORE = 3
DEFAULT_MAX_CONCURRENT = 2

# ===== EXPIRY STRATEGY =====
# Indian weekly expiry: Thursday (weekday=3)
# Backtest showed expiry strategy NOT profitable (PnL -1,835 vs -11,480)
ENABLE_EXPIRY_SCAN = True        # Still scan for awareness, but don't auto-enter

# ===== SIGNAL WEIGHTS (Total possible: 12 with ADX) =====
# LONG signals:
#   RVOL (rvol>=2 + close>prev)          = +2
#   RVOL_EXTRA (rvol>=3)                  = +1
#   20D_BREAKOUT (close>20D_high + vol)   = +2
#   VWAP_RECLAIM (close reclaims VWAP)    = +1
#   LIQ_SWEEP (low sweep + close up)      = +2
#   TREND_PB (close>20MA>50MA + bounce)   = +1
#   RSI_OK (55<=rsi<=75)                  = +1
#   EMA_BULL (ema9>ema21 + close>ema9)    = +1
#   ADX_STRONG (adx>=25)                   = +1
#   TOTAL                                 = 12

# SHORT signals (monitoring only):
#   RVOL_SHORT (rvol>=2 + close<prev)     = +2
#   RVOL_S_EXTRA (rvol>=3)                = +1
#   20D_BREAKDOWN (close<20D_low + vol)   = +2
#   VWAP_REJECT (close rejects VWAP)      = +1
#   LIQ_SWEEP_S (high sweep + close dn)   = +2
#   TREND_PB_S (close<20MA<50MA + bounce) = +1
#   RSI_BEAR (25<=rsi<=45)                = +1
#   EMA_BEAR (ema9<ema21 + close<ema9)    = +1
#   TOTAL                                 = 11

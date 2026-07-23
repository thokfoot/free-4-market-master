"""
FREE 4-Market v4.1 — STRATEGY CONFIG
=====================================
Centralized configuration file. 
Change parameters here and all modules pick them up automatically.

Author: Finance Manager — DO NOT MODIFY WITHOUT AUTHORIZATION
"""

# ===== CAPITAL & RISK =====
INITIAL_CAPITAL = 100000.0       # Starting capital for paper trading
RISK_PER_TRADE = 0.01            # 1% risk per trade
BROKERAGE_PCT = 0.001            # 0.1% per trade (STT + brokerage)
SLIPPAGE_PCT = 0.001             # 0.1% slippage on entry/exit

# ===== STRATEGY PARAMETERS (Backtest Optimized) =====
# Best params from 2-year backtest on 262 tickers:
#   SL=4%, TP=8%, Hold=14d, Score>=3, MaxConcurrent=1 -> +23.1% | PF 1.85
#   SL=4%, TP=8%, Hold=14d, Score>=3, MaxConcurrent=2 -> +12.9% | PF 1.47

STRATEGY_MODE = "LONG"           # "LONG" or "BOTH" (SHORT unreliable per backtest)
SL_PCT = 0.04                    # Stop loss: 4% below entry
TP_PCT = 0.08                    # Take profit: 8% above entry
MAX_HOLD_DAYS = 14               # Max holding period (calendar days)
MIN_SCORE = 3                    # Minimum signal score to enter (max 11)
MAX_CONCURRENT = 2               # Max simultaneous open positions

# ===== TICKERS =====
SCAN_INDIAN = True
SCAN_US = True
SCAN_CRYPTO = True

# ===== DATA =====
YF_PERIOD = "1y"                 # yfinance data period (need >50 days for indicators)
YF_INTERVAL = "1d"               # Daily data

# ===== SIGNAL WEIGHTS (Total possible: 11) =====
# LONG signals:
#   RVOL (rvol>=2 + close>prev)          = +2
#   RVOL_EXTRA (rvol>=3)                  = +1
#   20D_BREAKOUT (close>20D_high + vol)   = +2
#   VWAP_RECLAIM (close reclaims VWAP)    = +1
#   LIQ_SWEEP (low sweep + close up)      = +2
#   TREND_PB (close>20MA>50MA + bounce)   = +1
#   RSI_OK (55<=rsi<=75)                  = +1
#   EMA_BULL (ema9>ema21 + close>ema9)    = +1
#   TOTAL                                 = 11

# SHORT signals (monitoring only — not activated by default):
#   RVOL_SHORT (rvol>=2 + close<prev)     = +2
#   RVOL_S_EXTRA (rvol>=3)                = +1
#   20D_BREAKDOWN (close<20D_low + vol)   = +2
#   VWAP_REJECT (close rejects VWAP)      = +1
#   LIQ_SWEEP_S (high sweep + close dn)   = +2
#   TREND_PB_S (close<20MA<50MA + bounce) = +1
#   RSI_BEAR (25<=rsi<=45)                = +1
#   EMA_BEAR (ema9<ema21 + close<ema9)    = +1
#   TOTAL                                 = 11

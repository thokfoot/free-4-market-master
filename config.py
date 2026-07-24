"""
FREE 4-Market v5.0 — PROFESSIONAL PAPER TRADING SYSTEM
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
TOTAL_CAPITAL = sum(CAPITAL_BY_MARKET.values())  # ₹3,00,000
CAPITAL = TOTAL_CAPITAL  # For backward compatibility
RISK_PER_TRADE = 0.01           # 1% risk per trade
SL_PCT = 0.02                   # 2% stop loss
TP_PCT = 0.04                   # 4% take profit
MAX_HOLD_DAYS = 5               # Max hold days per trade (matches backtest)
MAX_CONCURRENT = 5              # Max simultaneous open positions

# ===== STRATEGY FILE =====
STRATEGY_FILE = os.path.join(os.path.dirname(__file__), "data", "strategies.csv")

# ===== SCAN SETTINGS =====
# Bot runs scans for all markets. SHORT configurable per region.
ALLOW_SHORT = {
    "US": True,
    "CRYPTO": True,
    "INDIAN": True,   # Paper only; set False for real ₹ trading
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

# ===== REGION INFERENCE =====
# If ticker ends with -USD, it's crypto
# If ticker starts with ^, it's India or US index
# If ticker is in US ETF list, it's US
CRYPTO_SUFFIX = "-USD"
INDIA_PREFIXES = ("^NSE", "^BSESN")

def get_region(yf_ticker: str, csv_region: str = None) -> str:
    """Infer region from ticker or CSV region column."""
    if csv_region and csv_region in ("Crypto", "US", "India"):
        return csv_region
    if yf_ticker.endswith(CRYPTO_SUFFIX):
        return "CRYPTO"
    if yf_ticker.startswith(INDIA_PREFIXES):
        return "INDIAN"
    return "US"


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

"""
FREE 3-Market v5.0 — PROFESSIONAL PAPER TRADING SYSTEM
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

TOTAL_CAPITAL = sum(CAPITAL_BY_MARKET.values()) + INTRADAY_CAPITAL  # ₹4,00,000
CAPITAL = TOTAL_CAPITAL  # For backward compatibility
RISK_PER_TRADE = 0.01           # 1% risk per trade
SL_PCT = 0.02                   # 2% stop loss
TP_PCT = 0.04                   # 4% take profit
MAX_HOLD_DAYS = 5               # Max hold days per trade (matches backtest)
MAX_CONCURRENT = 5              # Max simultaneous open positions

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
INTRADAY_MAX_CONCURRENT = 3  # Max 3 concurrent intraday positions
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

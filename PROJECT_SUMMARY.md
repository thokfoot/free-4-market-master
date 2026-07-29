# FREE 3-Market Paper Trade Bot — Complete Project Summary

**Generated:** 29 Jul 2026  
**Version:** v5.8  
**Repository:** [thokfoot/free-4-market-master](https://github.com/thokfoot/free-4-market-master)  
**Author/Owner:** Mind

---

## 1. WHAT — What Is This System?

A fully automated **paper trading bot** that scans 3 markets (India, US, Crypto) using **121 verified strategies** (81 swing + 40 intraday), enters paper trades with position sizing based on risk, monitors them with live SL/TP, logs everything for audit, and sends Telegram updates.

**It does NOT trade real money.** It simulates real trading conditions exactly — including brokerage charges, taxes, SL/TP, and position sizing — so the user can validate strategy performance before going live.

---

## 2. WHY — Why Was It Built?

- **To validate 121 trading strategies** that were backtested and verified by another AI
- **To simulate real market conditions** with charges, slippage, taxes, and position sizing
- **To get Telegram alerts** for every trade entry/exit with full OHLC telemetry
- **To maintain a complete audit trail** so every decision can be traced and validated
- **To detect bugs early** before real money is involved
- **To operate 24/7 via GitHub Actions** at zero infrastructure cost

---

## 3. WHEN — Schedule & Triggers

### GitHub Actions Workflows

#### 1. `bot.yml` — Trade Entry (3 schedules)
| Time (IST) | Time (UTC) | What It Does |
|:-----------|:-----------|:-------------|
| **06:30** | 01:00 | Morning swing scan (India pre-market) |
| **15:15** | 09:45 | India close scan + US pre-open |
| **18:30** | 13:00 | US market open scan |
| **19:00-02:00** | 13:30-20:30 | Every 1 hour — US intraday scans |

#### 2. `live_pnl.yml` — Live P&L Monitoring
| Market | Frequency | Hours (IST) |
|:-------|:---------:|:------------|
| **India** | Every 5 min | 09:15-15:30 (market hours) |
| **US** | Every 5 min | 19:00-02:30 (market hours) |
| **Crypto** | Every 30 min | 24/7 |

**Total:** ~220 GitHub Actions runs/day — all zero-cost via GitHub's free tier.

---

## 4. HOW — Architecture (File-by-File)

### Core Files

| File | Purpose | Lines |
|:-----|:--------|:-----:|
| `bot.py` | **Main orchestrator** — reads strategies CSV, fetches yfinance data, runs scanners, calls paper_trader for entries/exits, builds Telegram message, calls logger | 960 |
| `config.py` | **All configuration** — capital allocation (₹1L/market), SL/TP %, charges per market, strategy files, allow_short settings | 200 |
| `scanner.py` | **Swing scanner** — loads 81 strategies from CSV, computes indicators (SMA, EMA, RSI, Range), checks each pattern against ticker data, returns fired signals | 350 |
| `scanner_intraday.py` | **Intraday scanner** — same as scanner.py but uses 1h interval with 40 intraday strategies | 300 |
| `paper_trader.py` | **Portfolio & trade management** — enter_trade(), update_trades() (SL/TP check), calculate_qty(), generate_portfolio_report(), _send_sl_tp_alert() | 1150 |
| `live_pnl_updater.py` | **Live P&L checker** — runs every 5 min during market hours, fetches 1m data, checks SL/TP intraday, sends unrealized P&L updates | 700 |
| `logger.py` | **Logging** — log_trade() to CSV/Excel, log_scan() to daily JSON, log_error() | 200 |
| `config.py` helper | `get_region()` — maps ticker to market region (INDIAN/US/CRYPTO) | — |

### Data Files

| File | Content |
|:-----|:--------|
| `data/strategies.csv` | **81 swing strategies** (India=15, US=20, Crypto=15 from NEXT-50 + 31 CLEANED-100 verified) |
| `data/intraday_strategies.csv` | **40 intraday strategies** (1h timeframe, all US/Crypto, verified correct formula) |

### Log Files (in `logs/`)

| File | Content |
|:-----|:--------|
| `paper_trades.csv` | **Master trade log** — all trades with entry/exit/P&L/status |
| `portfolio.json` | **Portfolio state** — capital_by_market, open_positions, P&L |
| `strategy_stats.json` | **Per-strategy win rate** — tracks each strategy's W/L/P&L |
| `trade_audit.json` | **Full audit trail** — every entry and exit with all metadata |
| `daily_scan_YYYY-MM-DD.json` | **Daily scan log** — every ticker scanned, every pattern checked |
| `portfolio_snapshots.csv` | **Capital history** — timestamped capital snapshots |
| `live_pnl_snapshots.csv` | **Live P&L history** — 5-min snapshots during market |
| `trade_log.csv` / `.xlsx` | **Summary log** — daily trade summaries |

---

## 5. THE 121 STRATEGIES

### 81 Swing Strategies (1d timeframe, hold up to 5 days)

| Region | Count | Sources | Max SL | Max TP |
|:-------|:-----:|:--------|:------:|:------:|
| **India** | 15 | Sensex, Bank Nifty, Nifty 50, GIFT Nifty | 2% | 4% |
| **US** | 40 | QQQ, Nasdaq100, SPY, DIA, SP500, PHLX_Semi, XLI, XLRE, XLC, XLK, XLE, XLV, IWM, XLF | 2% | 4% |
| **Crypto** | 26 | ADA, AVAX, ETH, BNB, XRP, DOGE | 2% | 4% |

### 40 Intraday Strategies (1h timeframe, hold up to 6 hours)

| Region | Count | Markets |
|:-------|:-----:|:--------|
| **US** | 36 | XLK_Tech, QQQ, Nasdaq100, IWM, SP500, DIA, SPY, XLF_Fin |
| **Crypto** | 4 | ADA |
| **India** | 0 | (no verified 1h patterns above 60% win rate) |

### Strategy Verification

Every strategy was **independently verified by another AI** with:
- Correct formula check: `SMA20=Close.rolling(20).mean()`, `EMA9=ewm(span=9, adjust=False)`, `RSI14=Wilder: gain.ewm(alpha=1/14, adjust=False)`
- Charges: India 0.12% RT, US 0.02% RT, Crypto 0.30% RT
- Tax: India 30%, US 25%, Crypto 30%
- Verified files: `*_5Y_REAL.csv` for swing, `*_1h_2y_REAL.csv` for intraday

---

## 6. KEY DESIGN DECISIONS

### 6a. Capital Allocation
```
India:     ₹1,00,000 (separate from intraday)
US:        ₹1,00,000
Crypto:    ₹1,00,000
Intraday:  ₹1,00,000 (separate pool for intraday trades)
```
**Risk per trade:** 1% of market capital (₹1,000 per trade)

### 6b. Position Sizing
```python
risk_amt = market_capital * RISK_PER_TRADE    # 1% per trade
risk_per_share = abs(entry - sl)              # Stop-loss distance
qty = int(risk_amt / risk_per_share)          # Integer shares
```
With hard caps: entry < 0.1 → max 50,000, entry < 1 → max 10,000, entry > 100 → max 5,000

### 6c. SL/TP Exit Logic (HIGH/LOW priority)
```python
# For LONG trades (priority order):
# 1st: Intraday LOW hit SL → stopped out during the day
if daily_low <= sl * 0.9999:
    exit_price = sl, reason = "SL Hit (intraday)"
# 2nd: Intraday HIGH hit TP
elif daily_high >= target / 0.9999:
    exit_price = target, reason = "Target Hit"
# 3rd: Close <= SL
elif cmp <= sl * 0.9999:
    exit_price = sl, reason = "SL Hit (close)"
# 4th: Close >= TP
elif cmp >= target / 0.9999:
    exit_price = target, reason = "Target Hit (close)"
```
- **0.01% tolerance guard** prevents exits from 1-cent data noise
- **OHLC data validation** rejects NaN/0/inf values before any exit decision

### 6d. Charges Deduction
```python
# Deducted ONCE on exit (not on entry)
charge_rate = CHARGES_PER_MARKET[mode]  # India=0.12%, US=0.02%, Crypto=0.30%
charges = round(notional * charge_rate, 2)
pnl -= charges
pnl_pct -= charge_rate * 100
```

### 6e. Short Selling
```python
ALLOW_SHORT = {
    "INDIAN": False,  # India equity = no short (F&O only)
    "US": True,       # US ETFs = short allowed
    "CRYPTO": True,   # Crypto = short allowed
}
```

---

## 7. TELEGRAM MESSAGING

### Message Types
| Type | When | Content |
|:-----|:-----|:--------|
| **Portfolio summary** | Every scan run | Market capitals, open/closed, P&L, win rate, strategy rankings |
| **Entry alert** | When trade enters | Ticker, direction, entry, SL, TP, qty, rank |
| **SL/TP alert** | When SL/TP hits | **Real-time** with full OHLC telemetry (Close, High, Low that triggered it) |
| **Live P&L update** | Every 25 min if P&L changes >0.5% | Unrealized P&L per open position |
| **Portfolio report** | On demand via button | Full HTML report (viewable in browser) |

### Telegram Button
- "📊 Download Full Portfolio" → links to portfolio_report.html on GitHub

---

## 8. IMPORTANT BUG FIXES (Git History)

| Commit | Fix |
|:-------|:----|
| `b6fe89b` | **False SL exits** — Added OHLC validation + tolerance guard after 3 trades were falsely exited at 09:30 IST due to yfinance data glitch |
| `0b92bfd` | **Strategy stats double-counting** — `update_strategy_stats()` was being called at bottom of `update_trades()`, re-counting previously closed trades every run |
| `04dd20c` | **Live P&L telemetry** — Added `_send_sl_tp_alert()` to send immediate Telegram alert when SL/TP/Expiry hits, with full OHLC values |
| `079f307` | **OHLC validation in live_pnl_updater** — Same NaN/0/inf guard missing in live P&L checker (it could still false-exit) |
| `817f398` | **Missing audit fields** — `expected_win_rate` and `pattern_factors` were being passed to audit log function but silently dropped |
| `4579a81` | **Strategy stats mismatch** — `update_strategy_stats()` in live_pnl_updater was missing factors update check, print log, using unrounded PnL and original reason (no exit context) |

---

## 9. CURRENT STATUS (29 Jul 2026, 12:00 IST)

### Portfolio
| Market | Capital | Return |
|:-------|:-------:|:------:|
| 🇮🇳 India | ₹1,00,000 | 0.0% |
| 🇺🇸 US | ₹1,00,000 | 0.0% |
| ₿ Crypto | ₹1,00,000 | 0.0% |
| ⚡ Intraday | ₹98,969 | -1.0% |
| **Total** | **₹3,98,969** | **-0.26%** |

### Open Positions (4)
| Ticker | Dir | Entry | SL | Target | Timeframe | Reason |
|:-------|:---:|:-----:|:--:|:------:|:---------:|:-------|
| QQQ | LONG | 676.32 | 662.79 | 703.37 | Swing #46SW | Close>Open+2Red |
| IWM | LONG | 293.09 | 290.16 | 298.95 | Intraday #30ID | Price>SMA50+EMA9>EMA20+EMA20<EMA50+Close>Open |
| ^GSPC | LONG | 7435.34 | 7360.99 | 7584.05 | Intraday #16ID | Price>SMA20+EMA9>EMA20+EMA20<EMA50+Close>Open |
| SPY | LONG | 741.20 | 733.79 | 756.02 | Intraday #32ID | Price>SMA20+EMA9>EMA20+EMA20<EMA50+Close>Open |

### Closed Trades (2)
| Ticker | Dir | P&L | Reason |
|:-------|:---:|:---:|:-------|
| DIA | LONG | -₹1,015 | SL Hit (from earlier false exit issue, now corrected) |
| IWM | SHORT | -₹16 | Expiry |

---

## 10. WHAT ANOTHER AI SHOULD CHECK NEXT

### Priority Areas for Review

1. **Quantity cap impact on crypto trades** — Current caps: `entry < 0.1 → max 50,000`. For ADA at ~0.16, this doesn't apply. But risk utilization may still be low. Verify whether intended risk (1% = ₹1,000) matches actual risk (qty × |entry − SL|) for all open trades.

2. **Dynamic capital erosion** — Every loss reduces market capital → reduces future position size → reduces recovery speed. This is mathematically correct for money management but may distort strategy win rates. The user should confirm this is intentional.

3. **Intraday vs swing capital separation** — Intraday has a separate ₹1,00,000 pool. This means swing losses don't reduce intraday position sizes. But intraday losses don't reduce swing sizes either. Confirm this separation is intended.

4. **No India intraday strategies** — India has only 15 swing strategies. If the user wants India intraday trades, new 1h strategies need to be backtested and verified separately.

5. **No automated regression tests** — There are no unit tests. All validation is done via GitHub Actions logs and manual inspection. This is the #1 reliability gap.

6. **No broker integration** — The system is paper trading only. Real money would require broker API integration (Zerodha/AliceBlue for India, Alpaca/IBKR for US, Binance/Coinbase for crypto).

### File List for Quick Reference
```
free-4-market-master/
├── bot.py               # Main orchestrator (960 lines)
├── config.py            # Configuration (200 lines)
├── scanner.py           # Swing strategy scanner (350 lines)
├── scanner_intraday.py  # Intraday strategy scanner (300 lines)
├── paper_trader.py      # Portfolio + trade management (1150 lines)
├── live_pnl_updater.py  # Live P&L + SL/TP monitor (700 lines)
├── logger.py            # Logging (200 lines)
├── data/
│   ├── strategies.csv            # 81 swing strategies
│   └── intraday_strategies.csv   # 40 intraday strategies
├── logs/                # All runtime log files
└── .github/workflows/
    ├── bot.yml          # Trade entry schedules
    └── live_pnl.yml     # Live P&L monitoring schedules
```

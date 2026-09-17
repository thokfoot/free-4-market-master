# Free 4-Market Paper Trade Bot — Comprehensive Project Audit & Architecture Summary

> **Document Version:** v5.32+  
> **Updated Date:** September 17, 2026 (08:35 IST)  
> **Repository:** [thokfoot/free-4-market-master](https://github.com/thokfoot/free-4-market-master)  
> **Main Branch:** [`main`](https://github.com/thokfoot/free-4-market-master/tree/main)  
> **Target Audience:** External AI Reviewer / Quant Auditor / Senior Quantitative Systems Engineer  

---

## 1. Executive Summary & Objective

The **Free 4-Market Paper Trade Bot** is an autonomous, institutional-grade algorithmic trading simulation and validation engine operating continuously (24/7) across **4 major asset classes**:
1. **Indian Equities (NSE)** (Cash Dip-Buy / Long-Bounce & Big-Player Exit Fade)
2. **US Equities & Sector ETFs** (Daily Swing 1d & Intraday 1h)
3. **Crypto Assets** (Daily Swing 1d & Intraday 1h, 24/7 continuous session)
4. **Gap-Down / Special Situations** (1m automated bounce & momentum recovery)

### Core Mandates
* **Execution Realism:** Enforce real-world brokerage charges, transaction taxes (STT, Stamp Duty, GST, Exchange charges), strict percentage-based position sizing, session timeouts (5-day swing / 6-hour intraday), and continuous tick-level SL/TP evaluation.
* **Pure Out-of-Sample (OOS) Strategy Validation:** Objectively evaluate whether backtested multi-factor patterns retain positive expectancy in live forward testing before committing real capital.
* **Deterministic Single Source of Truth:** Portfolio metrics, open positions, capital buckets, and win/loss statistics are 100% deterministically derived from an immutable trade ledger (`logs/paper_trades.csv`).
* **Zero Infrastructure Cost:** Runs entirely on GitHub Actions workflows (~220 scheduled runs/day) within GitHub free-tier compute limits, with self-healing watchdog supervision.

---

## 2. Credentials, Secrets & Access Architecture

To satisfy strict institutional security standards and prevent account compromise, the bot enforces a **Zero-Hardcoded-Secrets Policy**:

```
┌────────────────────────────────────────────────────────────────────────┐
│                     CREDENTIALS & ACCESS ARCHITECTURE                  │
├──────────────────────────────┬─────────────────────────┬───────────────┤
│ Secret / Environment Key     │ Storage Location        │ Scope & Usage │
├──────────────────────────────┼─────────────────────────┼───────────────┤
│ TELEGRAM_TOKEN /             │ GitHub Secrets          │ Bot alert API │
│ TELEGRAM_BOT_TOKEN           │ (Encrypted Repository)  │ HTTPS POST    │
├──────────────────────────────┼─────────────────────────┼───────────────┤
│ TELEGRAM_CHAT_ID             │ GitHub Secrets / Config │ Broadcast     │
│                              │                         │ Target Group  │
├──────────────────────────────┼─────────────────────────┼───────────────┤
│ GITHUB_TOKEN / GH_TOKEN      │ GitHub Actions Runtime  │ Self-heal API │
│                              │ (Auto-injected Secret)  │ triggers & git│
├──────────────────────────────┼─────────────────────────┼───────────────┤
│ Market Data Access           │ Decentralized Fallback  │ Zero API keys │
│ (yfinance/yahoo/nasdaq/cache)│ (Public Resilient Chain)│ required      │
└──────────────────────────────┴─────────────────────────┴───────────────┘
```

### Key Security & Credential Rules:
1. **No Plaintext Secrets in Repository:** No passwords, private keys, or API tokens are stored in the codebase or git history.
2. **Runtime Injection:** Workflows inject secrets at runtime via `env:` mapping:
   ```yaml
   env:
     TELEGRAM_TOKEN: ${{ secrets.TELEGRAM_TOKEN }}
     TELEGRAM_CHAT_ID: ${{ secrets.TELEGRAM_CHAT_ID }}
     GH_TOKEN: ${{ secrets.GITHUB_TOKEN }}
   ```
3. **Data Provider Independence:** Market data requires **zero paid subscriptions or private API keys**. The multi-tier provider chain (`yfinance -> yahoo_chart -> nasdaq_or_binance -> ohlc_cache`) provides institutional-grade resilience with zero recurring credential dependencies.
4. **Automated Workflow Self-Healing:** The watchdog (`health_check.py`) uses the runner's built-in `GITHUB_TOKEN` to call the GitHub REST API (`POST /repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches`) to automatically revive any stalled workflows if GitHub Actions experiences scheduler delays.

---

## 3. System Architecture & Component Map

```
┌────────────────────────────────────────────────────────────────────────┐
│                   GITHUB ACTIONS ORCHESTRATION                         │
│   • bot.yml (Market Scans: 06:30, 15:15, 18:30 IST + Hourly Intraday) │
│   • live_pnl.yml (5-min Tick Monitor: US/India Market Hours, 30m Cryp) │
│   • fade_scan.yml (Specialized NSE Fade & Long-Bounce Monitors)        │
│   • gap_down.yml (1-min Gap-Down Recovery Scanner)                    │
│   • health_check.py (Heartbeat watchdog with auto self-healing dispatches)
└──────────────────┬─────────────────────────────────┬───────────────────┘
                   │                                 │
                   ▼                                 ▼
         ┌───────────────────┐             ┌───────────────────┐
         │     SCANNERS      │             │  LIVE PNL UPDATER │
         │  • scanner.py     │             │live_pnl_updater.py│
         │• scanner_intraday │             └─────────┬─────────┘
         │• scanner_fade.py  │                       │ (Polls live OHLC,
         │• scanner_gap_down │                       │  Checks SL/TP/Expiry)
         └─────────┬─────────┘                       │
                   │ (Fired Signals)                 │
                   ▼                                 ▼
         ┌─────────────────────────────────────────────────────┐
         │                 PAPER TRADER ENGINE                 │
         │                  (paper_trader.py)                  │
         │  • calculate_qty()   [1% Risk, Price Hard Caps]    │
         │  • check_entry_allowed() [Cooldown, Max Overlap]    │
         │  • enter_trade()     [Writes SHA-256 Provenance]    │
         │  • update_trades()   [Executes Exits & Deducts Fees]│
         │  • rebuild_portfolio_from_csv() [Single Truth]      │
         └─────────┬─────────────────────────┬─────────────────┘
                   │                         │
                   ▼                         ▼
         ┌───────────────────┐     ┌───────────────────────────┐
         │ IMMUTABLE LEDGER  │     │   REPORTING & AUDIT       │
         │ • paper_trades.csv│     │ • strategy_report.py      │
         │ • portfolio.json  │     │ • logs/strategy_report.xlsx│
         │ • signal_events   │     │ • bot.py (Telegram Alerts)│
         └───────────────────┘     └───────────────────────────┘
```

### Core Python Modules & Direct GitHub Links

| Module | Purpose | Lines of Code | GitHub Link |
|---|---|:---:|---|
| **`config.py`** | Central configuration: Capital pools, realistic charges per market, strategy filters, circuit breaker params | ~1,150 | [`config.py`](https://github.com/thokfoot/free-4-market-master/blob/main/config.py) |
| **`paper_trader.py`** | Core execution engine: Compounding sizing, ledger writes, SL/TP checks, split guards, deterministic rebuild | ~2,300 | [`paper_trader.py`](https://github.com/thokfoot/free-4-market-master/blob/main/paper_trader.py) |
| **`scanner.py`** | Daily swing scanner: Factor calculations (RSI, EMAs, SMAs, Bollinger, Candle Colors), signal generation | ~480 | [`scanner.py`](https://github.com/thokfoot/free-4-market-master/blob/main/scanner.py) |
| **`scanner_intraday.py`** | 1-hour intraday scanner for US and Crypto assets | ~320 | [`scanner_intraday.py`](https://github.com/thokfoot/free-4-market-master/blob/main/scanner_intraday.py) |
| **`live_pnl_updater.py`** | 5-minute high-frequency monitor: Market hours gating, live tick SL/TP breaches, portfolio synchronization | ~850 | [`live_pnl_updater.py`](https://github.com/thokfoot/free-4-market-master/blob/main/live_pnl_updater.py) |
| **`strategy_report.py`** | Automated Excel report generator (`Summary` + `All Trades` sheets with formulas & formatting) | ~760 | [`strategy_report.py`](https://github.com/thokfoot/free-4-market-master/blob/main/strategy_report.py) |
| **`health_check.py`** | Heartbeat watchdog: Gap detection, Telegram alerts, and automated workflow self-healing via GitHub API | ~180 | [`health_check.py`](https://github.com/thokfoot/free-4-market-master/blob/main/health_check.py) |
| **`bot.py`** | Main CLI orchestrator, cron router, and Telegram broadcast composer | ~2,230 | [`bot.py`](https://github.com/thokfoot/free-4-market-master/blob/main/bot.py) |

---

## 4. Capital Allocation Model & Compounding Position Sizing

The system operates on an invariant base capital of **₹8,00,000 INR**, segregated into isolated risk pools to prevent cross-market contagion:

```
Total Base Capital: ₹8,00,000 INR
├── INTRADAY (1h US & Crypto):  ₹2,00,000 Base (Current: ₹2,03,734.71)
├── US Swing (1d):              ₹1,00,000 Base (Current: ₹1,04,897.94)
├── Crypto Swing (1d):          ₹1,00,000 Base (Current: ₹1,05,452.60)
├── India Cash (Long-Bounce):   ₹1,00,000 Base (Current: ₹1,00,845.81)
├── Indian Equities (General):  ₹1,00,000 Base (Current: ₹1,00,000.00)
├── NSE Fade (1h Big Player):   ₹1,00,000 Base (Current: ₹1,00,000.00)
├── US Fade (5m Big Player):    ₹1,00,000 Base (Current: ₹1,00,000.00)
└── IPO Strategy Bucket:        ₹0.00     Base (Decommissioned & reallocated to Intraday)
```

### Mathematical Position Sizing Formulation
For any trade entry, position sizing is strictly risk-governed:

$$\text{Risk Amount} = \text{Current Bucket Equity} \times \text{Risk Per Trade (1.0\%)}$$

$$\text{Per-Share Risk} = |\text{Entry Price} - \text{Stop Loss Price}|$$

$$\text{Raw Quantity} = \left\lfloor \frac{\text{Risk Amount}}{\text{Per-Share Risk}} \right\rfloor$$

#### Dynamic Price-Tier Hard Caps
* $\text{Price} < \$0.10$: $\text{Max Qty} = 50,000$
* $\$0.10 \le \text{Price} < \$1.00$: $\text{Max Qty} = 10,000$
* $\text{Price} > \$100.00$: $\text{Max Qty} = 5,000$
* Single-share floor with maximum over-risk factor ($1.5\times$) for high-priced securities.

---

## 5. Live Performance Audit (All-Time Track Record as of Sep 17, 2026)

### High-Level Summary (Rupees Only)
* **Total Portfolio Capital:** **₹8,14,931.06 INR** (+1.87% net capital growth)
* **Total Realized Net Profit:** **+₹14,931.06 INR**
* **Total Executed Trades:** **70** (65 Closed, 5 Currently Open)
* **Realized Win / Loss Record:** **41 Wins / 24 Losses**
* **Live Realized Win Rate:** **63.08%**
* **Ledger Integrity Status:** **INTEGRITY_OK** (`integrity_check.py`)

### Breakdown by Market Category

```
┌──────────────────┬──────────┬──────────┬──────────┬────────────┬─────────────┐
│ Market Section   │ Trades   │ Wins     │ Losses   │ Win Rate   │ Net P&L (₹) │
├──────────────────┼──────────┼──────────┼──────────┼────────────┼─────────────┤
│ 🇺🇸 US Intraday   │ 15       │ 12       │ 3        │ 80.00%     │ +₹9,146.80  │
│ 🇺🇸 US Swing      │ 31       │ 18       │ 13       │ 58.06%     │ +₹4,897.94  │
│ ₿ Crypto Swing   │ 11       │ 8        │ 3        │ 72.73%     │ +₹5,452.60  │
│ ₿ Crypto ID      │ 6        │ 2        │ 4        │ 33.33%     │ -₹5,412.09  │
│ 🇮🇳 India Bounce  │ 2        │ 1        │ 1        │ 50.00%     │ +₹845.81    │
├──────────────────┼──────────┼──────────┼──────────┼────────────┼─────────────┤
│ TOTAL CLOSED     │ 65       │ 41       │ 24       │ 63.08%     │ +₹14,931.06 │
└──────────────────┴──────────┴──────────┴──────────┴────────────┴─────────────┘
```

> **Automated Lockout Event (Sep 17):** Strategy Rank `#1` took 3 consecutive crypto intraday short losses (`LINK-USD`, `XRP-USD`, `SOL-USD`) pushing its cumulative loss to **-₹7,346.04** on 19 trades. Under the pre-registered `PREREGISTERED_DISABLE` rule ($n \ge 10, \text{loss} \le -₹3,000$), Rank 1 was **automatically and permanently locked out** without manual intervention.

### Active Open Positions (5 Trades Tracking Live)

| Ticker | Market / TF | Direction | Qty | Entry | CMP | Move % | Unrealized P&L | Stop Loss | Target | Strategy Reason |
|---|---|---|---|---|---|---|---|---|---|---|
| **`TRX-USD`** | CRYPTO (1d) | LONG | 10,000 | $0.3393 | $0.3356 | -1.08% | **-₹36.75** | $0.3315 | $0.3518 | `#1SW EMA9>EMA20+Price>SMA50+Range<1%` |
| **`XLP`** | US (1d) | LONG | 627 | $83.39 | $83.33 | -0.07% | **-₹37.62** | $81.71 | $86.72 | `#4SW EMA20<EMA50+Range<1%` |
| **`OEF`** | US (1d) | LONG | 137 | $380.39 | $374.83 | -1.46% | **-₹761.72** | $372.74 | $395.56 | `#4SW EMA9>EMA20+Price>SMA20+Range<1%` |
| **`XLC`** | US (1d) | LONG | 466 | $112.61 | $113.00 | +0.35% | **+₹181.74** | $110.35 | $117.10 | `#3SW Price>EMA9+Range<1%` |
| **`XLK`** | US (1d) | LONG | 284 | $183.76 | $183.93 | +0.09% | **+₹48.28** | $180.07 | $191.09 | `#28SW Price>SMA50+EMA20>EMA50+2Red` |

* **Total Unrealized P&L:** **-₹606.07 INR**

---

## 6. Realistic Fee & Expense Modeling

All fee structures simulate real institutional costs:
* **Indian Equities (0.30% round-turn):** STT 0.10% each side (0.20% round-turn) + Stamp Duty 0.015% on buy + Exchange turnover charges (0.00345%) + SEBI turnover charges (0.0001%) + GST 18% on brokerage/charges + clearing fees.
* **US Equities (0.02% round-turn):** SEC transaction fees + FINRA TAF + clearing buffer.
* **Crypto Assets (0.30% round-turn):** Tier-1 exchange taker fee model (0.15% maker/taker $\times$ 2).

---

## 7. Direct Repository Links for Auditors

Auditors and external AI models can inspect the codebase and data files directly at:

* **Primary Trade Ledger:** [`logs/paper_trades.csv`](https://github.com/thokfoot/free-4-market-master/blob/main/logs/paper_trades.csv)
* **Live Portfolio State:** [`logs/portfolio.json`](https://github.com/thokfoot/free-4-market-master/blob/main/logs/portfolio.json)
* **Strategy Audit Ledger:** [`logs/strategy_stats.json`](https://github.com/thokfoot/free-4-market-master/blob/main/logs/strategy_stats.json)
* **Excel Report Source:** [`logs/strategy_report.xlsx`](https://github.com/thokfoot/free-4-market-master/blob/main/logs/strategy_report.xlsx)
* **Configuration Module:** [`config.py`](https://github.com/thokfoot/free-4-market-master/blob/main/config.py)
* **Execution Engine:** [`paper_trader.py`](https://github.com/thokfoot/free-4-market-master/blob/main/paper_trader.py)
* **Watchdog & Self-Healing:** [`health_check.py`](https://github.com/thokfoot/free-4-market-master/blob/main/health_check.py)
* **Live PnL Monitor:** [`live_pnl_updater.py`](https://github.com/thokfoot/free-4-market-master/blob/main/live_pnl_updater.py)
* **Test Suite (377 Tests):** [`tests/core/`](https://github.com/thokfoot/free-4-market-master/tree/main/tests/core)

---

## 8. Master Audit Prompt for External AI Reviewer

Copy and paste the following prompt directly into another AI (e.g. OpenCode Council, Claude 3.5 Sonnet, GPT-4o, Nemotron) to execute an independent audit:

```text
You are an expert Quantitative Trading Systems Architect, Risk Auditor, and Senior Python Core Developer.
Perform a thorough, honest, and critical technical audit of the Free 4-Market Paper Trading System (Repository: https://github.com/thokfoot/free-4-market-master, Commit: cad56b0d).

Please review the codebase, documentation, and live data to answer the following 5 critical questions:

1. ARCHITECTURAL INTEGRITY & STATE CONCURRENCY:
   - Does paper_trader.py:rebuild_portfolio_from_csv() provide true ACID-like single-source-of-truth semantics across concurrent or scheduled GitHub Actions runner executions?
   - How resilient is the watchdog self-healing mechanism in health_check.py against GitHub API rate limits or runner queues?

2. POSITION SIZING & CAPITAL COMPOUNDING:
   - Critically evaluate the dynamic 1% risk-per-trade sizing formulation in paper_trader.py:calculate_qty(). Does the dynamic compounding across isolated capital buckets (e.g., ₹2L Intraday vs ₹1L Swing) protect against ruin while maximizing geometric growth?
   - Are the hard price-tier quantity caps ($0.10, $1.00, $100.00) appropriate to prevent liquidity and slippage distortion?

3. EXECUTION REALISM & SLIPPAGE TOLERANCE:
   - Evaluate the realistic fee models (0.30% round-turn for India/Crypto, 0.02% for US).
   - How well does the 5-minute candle checking in live_pnl_updater.py approximate real exchange matching engine SL/TP fills? What slippage degradation would you predict when transitioning from paper to real broker APIs (Zerodha / Interactive Brokers)?

4. STRATEGY UNIVERSE & OVERFITTING RISK:
   - Audit the strategy taxonomy and factor definitions in scanner.py and scanner_intraday.py.
   - Is the granular blacklisting of 4 counter-trend strategies in config.py:DISABLED_STRATEGIES justified, or does it introduce selection/survivorship bias into out-of-sample forward testing?

5. TOP 3 RECOMMENDATIONS FOR PRODUCTION READINESS:
   - What are the top 3 architectural or quantitative enhancements required before funding this bot with real discretionary capital?
```

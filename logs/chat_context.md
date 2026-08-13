# 🤖 CHAT CONTEXT LOG — free-4-market-master
> **Purpose:** Agar chat window close ho jaye, naya session ye file padh kar pura context samjhega.
> **Rule for AI:** Har turn ke end pe is file ko update karo (naya section append karo). Naye session ki shuruaat me SABSE PEHLE ye file + logs/paper_trades.csv + logs/portfolio.json read karo.
> **Auto-commit:** Ye file logs/ me hai, isliye bot workflows (fade_scan, bot, gap_down, live_pnl) ke commit steps ise git pe rakh dete hain. Manual push bhi kabhi-kabhi karo.

---

## 📌 CURRENT SNAPSHOT (last updated: 2026-08-13 14:37 IST)

### Portfolio (from logs/portfolio.json)
| Bucket | Capital |
|---|---|
| INDIAN | ₹100,000 |
| US | ₹101,232 |
| CRYPTO | ₹99,964 |
| INTRADAY | ₹79,369 |
| FADE | ₹95,490 |
| US_FADE | ₹100,000 |
| **TOTAL** | **₹576,055** |

- Total P&L: **₹-23,945** | Open: 17 | Closed: 74 | Wins 33 / Losses 40

---

## 🗂 KEY FILES (do NOT delete/rename)
| File | Role |
|---|---|
| `bot.py` | Main bot + TG messages + market transition alerts + gating |
| `config.py` | FADE_VARIANTS (40), US_FADE_VARIANTS (5), capital, slippage |
| `scanner_fade.py` | NSE fade scanner (S/G/H variants, entry=next-bar-open) |
| `scanner_fade_us.py` | US fade scanner (U1-U5, VWAP-below) |
| `paper_trader.py` | Trade engine: enter/exit, SL/TP, qty, slippage, portfolio |
| `live_pnl_updater.py` | 5-min live P&L + exit alerts + Excel refresh |
| `strategy_report.py` | Excel (326 strategies) |
| `market_data.py` | Data fallback chain: yfinance → direct Yahoo chart API → Binance |
| `logger.py` | JSON daily logs, CSV trade log, error log |
| `.github/workflows/fade_scan.yml` | FADE every 5 min India hours |
| `.github/workflows/bot.yml` | Scheduled swing/intraday runs |
| `logs/health_check_report.md` | Full health check (2026-08-13) |

---

## 📜 SESSION HISTORY (most recent first)
### 2026-08-13 14:37 IST — Re-check found + fixed union-merge duplicates (13 Aug)

- Second check found: union-merge (git merge=union) created duplicate trade rows when bot + live_pnl both exited the SAME trade concurrently (each wrote own reason string: 'Target Hit' vs '🎯 Target Hit (live)'). GARUDA +2511 and PVP -1122 appeared 2x - P&L double-counted (FADE net showed -5508, real -4510). FIXED: .ai/dedupe_csv.py dedupes by trade identity (Date/Time/Ticker/Direction/Entry/Qty/Exit/Status, ignores Reason); commit_logs.sh runs it after merge. Cleaned paper_trades.csv 96->91. SPY 5x verified distinct (different qty). 339 tests pass, CI green. GARUDA = first FADE WIN +2511 Target Hit! FADE today: 7 closed, net -4510 (expected pattern: 36-47% win rate RR 1:3).

---

### 2026-08-13 14:20 IST — Fixed git push conflict issue (13 Aug)

- Found real issue: scheduled FADE SCAN run (07:57 UTC) FAILED - git push conflict when bot/fade/gapdown/live_pnl workflows commit same logs simultaneously. Commit 70204cf was lost. No trade data lost (0 entries in that run). FIXED: (1) daily_scan files now mode-specific (daily_scan_{MODE}_{date}.json) - no JSON conflict between workflows; (2) .gitattributes *.csv merge=union - concurrent paper_trades.csv appends both survive; (3) shared .ai/commit_logs.sh robust commit (retry+rebase+merge fallback) used by all 4 workflows. 339 tests pass, CI green. KERNEX pending item CLOSED (SL hit -1.72%). FADE closed trades: 4/4 losses -4694 (expected: 36-47% win rate, RR 1:3).

---

### 2026-08-13 14:04 IST — FADE trade verification complete (13 Aug)

- Re-verified all FADE trades from independent data: (1) 10/10 fired signals valid at exact time (entry price = next-bar open, no lookahead); (2) 4 closed trades SL exits verified; (3) 37 skipped entries all 'Duplicate already open' = valid 1-stock-1-trade rule; (4) MODISONLTD H3 mystery solved - cut by per-variant daily cap (H3=2/day), weakest shoot lost; (5) G24 not firing was correct - AVROIND not near day-high (0.9492 < 0.98). Fixed logging gap: cap-cut signals now recorded in skipped_entries. Committed b768fb9/c2e6dc5, 339 tests pass.

---

### 2026-08-13 12:30 IST — Chat-log system created

- logs/chat_context.md stores full project state
- python .ai/update_chat_log.py refreshes snapshot or appends entries
- Committed ea6cc5b + pushed (remote verified)

---


### 2026-08-13 — Session: FADE fixes + alerts + health check
1. **FADE backtest-spec fixes (v5.18, commit 9105363):**
   - WINDOWS bug: `1030_1300` was (300,420)=10:30-12:30 IST → fixed (300,450)=10:30-13:00 (S5/S9 30min late cut tha)
   - VolAvg20: rolling(20) included today → fixed to prior-trading-days-only baseline
   - Entry price: signal close → **next bar Open** (no lookahead) — both scanners emit `entry_price`
   - New `fade_scan.yml`: bot.py --mode=fade every 5 min India hours (was missing — old 1h crons skipped by GH Actions queue)
   - 339 tests pass
2. **FADE first real trades (11:35 + 12:17 IST):** 10 trades entered, signals verified (PRECWIRE G24: RSI 77.4, Shoot 5.02%, vol 1.6x)
3. **TG alerts (v5.19, commit ebb031d):**
   - `check_market_transitions()` → 🔔 MARKET STATUS alert only on real open/close transitions (state file logs/market_status_state.json)
   - Gating: fade/gapdown 5-min runs only message on events (entries/closed/fired) — was spamming ~75 msg/day
   - Scheduled both/swing/intraday still send summary every time
4. **Health check report:** logs/health_check_report.md (commit 385d714)
5. **PRECWIRE loss analysis:** momentum-continuation — stock shot up, faded short, but stock resumed rally → SL hit. Expected at 36.55% win rate + RR 1:3.

### 2026-08-13 (early) — US FADE added (v5.18)
- scanner_fade_us.py: 129 US large-caps, SPY gap filter, ET windows (UTC→America/New_York DST-aware), VWAP-below filter
- 5 US_FADE_VARIANTS (U1-U5) ranks 870-874, ₹1L US_FADE bucket
- Fixed US timezone bug (data UTC, windows were ET — 5/6 windows matched 0%)
- US 5m fade positive (+7-11%/mo) but 1h 2yr OOS failed — experimental, cap 2/day

### 2026-08-12 — FADE variants added (35 more)
- User asked: keep original 5 + add 35 = 40 NSE FADE variants (G1-G25 + H1-H5 + S1-S10)
- G-series: 15m variants (shoot 2.5-4%, vol 1.5-3x, RSI 60-75, SL 0.7-1.5%, TP 2-3.75%)
- H-series: 1h variants (3-4% shoot, SL 1.3-1.5%, TP 3-3.9%, max 2/day)

---

## ⚠️ KNOWN ISSUES / NOTES
1. **Yahoo rate-limit** (HTTP 401 Invalid Crumb): occasional — market_data fallback handles, few tickers skip (195/198 typical)
2. **GitHub Actions cron delay:** scheduled runs sometimes late/skipped (platform issue) — manual dispatch works
3. **US_FADE:** 0 trades so far — US market opens at night (7PM-1:30AM IST), signals come then
4. **Gap-down (GD) strategies (#997/#998):** historically bad (-₹31k) — stale-data guard active, skip on stale data
5. **FADE India:** momentum-continuation stocks (RSI 80+, vol 20x+) bleed shorts — G24/G11-type low-RSI-threshold entries riskier
6. **health_check_report.md** = full verified system state

---

## 🚀 NEXT STEPS (pending)
- [ ] Check KERNEX exit (was -3.3% underwater)
- [ ] Analyze FADE loss pattern (RSI/vol/momentum common factors) → possible filter
- [ ] Verify fade_scan.yml scheduled runs fire reliably next market hours
- [ ] US_FADE first trades when US market opens
- [ ] 30-60 day paper trade consistency before live

## 📞 USER PREFERENCES
- Hinglish (Hindi + English mix) — simple words, short answers
- Wants PROOF for every claim (verify from real data)
- Wants TG alerts on: trade entry, exit, market open/close
- Wants Excel + portfolio updated promptly
- Wants chat context saved so new session resumes seamlessly
- Frustrated by repeated issues → wants honesty + confidence, not false promises

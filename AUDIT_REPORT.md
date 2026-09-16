# Independent Technical Audit — Free 4-Market Paper Trading System

**Auditor:** OpenCode Council (big-pickle, primary audit)  
**Commit audited:** `596254de` (Sep 16 2026, 15:15 IST)  
**Methodology:** Full local code read of all core modules (`paper_trader.py`, `config.py`, `live_pnl_updater.py`, `health_check.py`, `scanner.py`, `scanner_intraday.py`, `bot.yml`/`live_pnl.yml` workflows) + independent ledger verification via pandas (66 rows, chain hash validated, stats recomputed) + pytest suite confirmed (377 passed, 1 skipped).

---

## 0. Independent Verification Summary

All claimed figures verified and hold, with one documentation inconsistency:

| Metric | Claimed | Verified (independent) |
|--------|---------|----------------------|
| Total rows / closed / open | 66 / 62 / 4 | **66 / 62 / 4 ✓** |
| Win / Loss | 41 / 21 | **41 / 21 ✓** |
| Win Rate | 66.13% | **66.13% ✓** |
| Total P&L | +₹23,451.26 | **+₹23,451.26 ✓** |
| Capital total | ₹8,23,451.26 | **₹8,23,451.26 ✓** |
| SHA-256 ledger chain | INTEGRITY_OK | **0 mismatches ✓** (verified independently) |
| Test suite | 377 passed, 1 skipped | **377 passed, 1 skipped, 130 warnings ✓** |
| Portfolio.json ↔ CSV consistency | INTEGRITY_OK | **Consistent ✓** |
| **Doc inconsistency (minor)** | US Swing listed as 32 trades / 17W-11L; table sums to 63 | CSV = **31** US SWING_1d closed trades; table sum = 62 total ✓. One trade moved or miscounted in doc. **Doc should read 31 US Swing.** |

---

## 1. Architectural Integrity & State Concurrency

### `rebuild_portfolio_from_csv()` — Source-of-Truth Semantics

**Assessment: Strong. The deterministic derivation is sound, but ACID is enforced at the orchestration layer, not the storage layer.**

**What works well:**
- `portfolio.json` is a pure, deterministic function of `paper_trades.csv`. Both `update_trades()` (bot.py) and `process_open_trades()` (live_pnl_updater.py) call `rebuild_portfolio_from_csv()` after any CSV mutation — any drift between the two workflows is self-correcting on the next exit. (`paper_trader.py:764-838`)
- Negative capital is no longer masked (line 827-830 — loud warning + negative allowed). This is honest and avoids an equity overstatement bug present in many paper systems.
- `save_portfolio()` uses `os.replace()` atomic rename (`paper_trader.py:287-305`); the lineage chain (`_recompute_chain`, lines 433-461) is recomputed after every mutation, making `portfolio.json` immune to stale incremental updates.

**Critical weakness — non-atomic CSV read-modify-write:**
- `enter_trade()` (line 1226-1253) reads the CSV, appends a row via `pd.concat`, then writes via `df_comb.to_csv(PAPER_FILE, index=False)` — **NOT atomic**. A process kill mid-write truncates the ledger.
- `update_trades()` (line 1949, 2299-2301) and `process_open_trades()` (live_pnl_updater: 461, 798-800) perform the same non-atomic read→mutate→write pattern.
- `live_pnl_updater.save_portfolio()` (line 341) also uses plain `open(..., "w")` instead of atomic write — unlike `paper_trader.save_portfolio()`. This is a **code duplication mismatch** that introduces corruption risk for the heartbeat/live snapshot path.

**Current protection — GitHub Actions concurrency group:**
All four state-writing workflows (`bot.yml`, `live_pnl.yml`, `fade_scan.yml`, `gap_down.yml`) share `concurrency: group: free4market-v5-bot, cancel-in-progress: false`. This **serializes all writers** at the platform level, preventing concurrent runner races. However:

1. This is **implicit** — no documentation within the Python code itself; any new workflow added without this group (or running locally/externally) would reintroduce race conditions.
2. The keepalive self-reschedule loop (`sleep 120` + `gh workflow run`) within `live_pnl.yml` queues jobs → each wait up to 8 min in the group → with 30-min crypto cadence + 5-min intraday, the queue depth grows; if a job is slow, backlog builds. `cancel-in-progress: false` prevents starvation but creates latency under load.
3. Mid-run `_commit_state_now()` (live_pnl_updater:912-928) persists state after exits, but the commit script `.ai/commit_logs.sh` is non-atomic (standard git add+commit+push). If the step or workflow times out before push, state is in working tree but not remote.

**Watchdog self-healing (`health_check.py`) — Resilience assessment:**
- **3-tier fallback:** API dispatch (GITHUB_TOKEN) → `gh` CLI fallback → Telegram alert. Correctly degrades when API is rate-limited.
- **Rate limit risk:** Low. `GITHUB_TOKEN` has 1,000 core API calls/hr; health dispatches 4 workflows maximum once per 3 hours. However, the token rate limit is repo-scoped for PATs and slightly different for auto-injected GITHUB_TOKEN — verify in production.
- **No exponential backoff:** Alert interval fixed at 180 min; self-heal retry at 35 min. Acceptable for a single-instance watchdog, but if the underlying cause is a broken workflow file (syntax error in code), repeated dispatching consumes runner minutes for no benefit.
- **Edge case:** If `health.yml` itself cannot run (GitHub Actions outage, budget exhaustion), no heartbeat is written → Telegram alerts stop → silent failure. Not detectable by the bot itself.

**Verdict: B+** — The single-source-of-truth design is architecturally correct and well-implemented. The non-atomic CSV layer is currently masked by platform-level serialization but remains fragile if deployed outside GitHub Actions. For production with real money, add a file-level advisory lock (`fcntl.flock` or lockfile) + atomic CSV writes (temp+rename) as belt-and-suspenders.

---

## 2. Position Sizing & Capital Compounding

### `calculate_qty()` — Risk-Based Sizing

**Assessment: Conservative, well-bounded, but unconstrained by bucket and missing a notional risk limit.**

**Strengths:**
- 1% risk-per-trade (`RISK_PER_TRADE = 0.01`, config.py:35) is well below the Kelly-optimal f for the claimed edge (WR ~66%, RR ~2:1 → optimal f ≈ 49%). This strongly protects against ruin and is the correct choice for a validation engine.
- Per-bucket isolation prevents cross-market contagion: a wipeout in crypto leaves US/Indian buckets untouched. This is the right architecture for multi-asset risk.
- The min-lot over-risk floor (`MAX_OVER_RISK_FACTOR = 2.0`, config.py:65) is transparent, bounded, and logged loudly — best-practice for a paper system acknowledging penny-asset edge cases.

**Weaknesses:**
- **No aggregate risk cap per bucket:** A bucket with 25 concurrent 1% risk trades is theoretically 25% of bucket equity at risk on any single close. With `MAX_CONCURRENT = 100` total, a single bucket could theoretically hold all 100 → 100% simultaneous risk. On paper (no margin costs), this is a stress test; for real capital, this violates basic portfolio risk management (max portfolio VaR).
- **Price-tier caps don't bound notional below $100:** For a $50 stock with a 2% SL → `per_share_risk = $1.00`; `risk_amt = ₹1,674` (1% of ₹1.67L intraday bucket) → `qty = 1,674` shares → notional $83,700 ≈ ₹70L = **420% of the bucket's equity**. The 1% *risk* is bounded, but the notional exposure is not. For real broker margin (IBKR requires 25-50% margin on equities), the position may be un-fundable.
- **No floor on capital after losses:** After a bad run, bucket capital can go negative (lines 826-830 warn but don't prevent). Negative capital → `risk_amt = negative × 1%` → `calculate_qty` refuses (qty=0). This is defensive, but the portfolio's `total_capital` can drop below the initial ₹8L, making return calculations psychologically alarming. The audit doc shows no such bucket, but the logic allows it.

**Compounding behavior:** Since capital is NOT reduced at entry (only realized P&L on exit updates capital), position sizing is slightly optimistic — the same capital can be "at risk" across many concurrent positions. In real trading, margin/capital reservation would prevent this.

**Verdict: B** — Excellent for a validation engine (conservative per-trade risk, transparent over-risk policy). For production, add a **per-bucket aggregate notional/risk limit** (e.g., sum of per-trade risk ≤ 40% of bucket equity) and a margin/capital-reservation model for real broker allocation.

---

## 3. Execution Realism & Slippage Tolerance

### Fee Models — `CHARGES_PER_MARKET` (config.py:804-808)

| Market | Paper RT Cost | Real-World Reference | Accuracy |
|--------|-------------|----------------------|----------|
| INDIAN (cash swing) | 0.30% | STT 0.1%×2 (0.20%) + Stamp 0.015% + Exchange 0.003% + GST 18% + SEBI ≈ 0.24-0.28% + slippage buffer | **Good** — conservative upper bound ✓ |
| US (ETF swing) | 0.02% | $0 brokerage + SEC 0.0008% + FINRA 0.00014% + Exchange ~0.003% + slippage 0.01% | **Good** — realistic for IBKR/Alpaca free-commission ✓ |
| Crypto | 0.30% | Binance 0.1%×2 (0.20%) + spread/slippage buffer 0.10% | **Good** — matches Tier-1 taker fees ✓ |

**Criticism:** Charges are computed on `entry × qty` only (notional entry, not round-turn notional entry+exit). Since exit price ≈ entry (±SL/TP), the approximation is reasonable for swing (SL/TP < 5%); intraday (SL 1-1.5%) is more accurate. A minor conservative bias exists: if TP is hit (exit > entry), the real exit-side notional is larger → actual cost is slightly higher than modeled. Negligible for validation.

### SL/TP Fill Approximation in `live_pnl_updater.py`

**Assessment: Systematically optimistic for gaps — the live path lacks bar-level gap-fill modeling that exists in the bot path.**

**How it works (live_pnl_updater.py:605-635):**
- Fetches 1m bars via `fetch_live_ohlc()`, computes `daily_low = df["Low"].min()` over post-entry bars.
- Evaluates: `daily_low <= sl × 0.9999` → fills at exactly `sl` + slippage PCT (0.01-0.30% depending on market).
- Gap-aware fills (`_bars_sl_tp` in paper_trader.py:153-186) model overnight gaps — fills at the gap-open when open sits beyond the trigger. However, **`fetch_live_ohlc()` returns only `{close, high, low, date}` (no bars array)** → the live updater never invokes gap-fill logic → **live exits are more optimistic than the bot path for overnight gaps.**

**Quantified degradation when transitioning to real broker APIs:**

1. **Gap-through risk (largest):** US stocks can gap 0.5-5% overnight on earnings/news; crypto can gap 5-20% on a flash crash. Paper model fills at SL exactly; real broker fills at market (possibly 0.1-3% below SL on gap-down). **Estimate: +0.1-1.5% extra loss per affected trade.** Affects ~10-20% of swing exits (quarterly earnings season, macro events).

2. **Intra-bar wick slippage:** Paper fills at SL/TP price; real broker fills at the trigger price ± 0.01-0.05% (fast-moving intraday markets on IBKR SmartRouting). Crypto: +0.05-0.30% on illiquid pairs. **Estimate: +0.02-0.10% per trade on average.**

3. **Tolerance bias (0.9999):** The 0.01% tolerance gate (lines 2073, 621-625) prevents 1-cent data noise from triggering exits. For TRX-USD at $0.3393: tolerance = $0.000034 — essentially no bias. For a $100 stock with SL at $98.00: tolerance = $0.01 — no material impact. **Acceptable.**

4. **Intraday shorting gap (Indian market):** The system flags `FADE_1h SHORT` as a cash-market impossibility (config.py:646-647: "PAPER-TRADE simulation... real execution needs F&O/hedged access"). If transitioning to real Indian shorting, paper results are un-realistic by design. Acknowledged.

**Verdict: B-** — The fee models are realistic and conservative. The core issue is the live updater's optimism on gap fills (missing bars-array gap modeling). For production readiness: (a) pass full 1m bars to the live updater (same as bot's `_bars_sl_tp`), (b) add stochastic slippage sampled from an empirical distribution (not fixed PCT), and (c) model the Indian intraday order book (F&O vs cash limitation).

---

## 4. Strategy Universe & Overfitting Risk

### Scanner Architecture (scanner.py + scanner_intraday.py)

**Assessment: Clean code, strong technical indicators, but severe multiple-testing and survivorship bias invalidate OOS claims for individual strategies.**

**What works well:**
- Indicators are computed correctly: EMA uses `adjust=False` (not Pandas default), RSI uses Wilder's α=1/14. No look-ahead bias in scanner evaluation (`drop_incomplete_last_bar` on 1d data, `_signal_candle_index` for completed 1h bars). (`scanner.py:78-155`, `scanner_intraday.py:79-134`)
- Factor evaluation via `explain_signal()` is deterministic and fully auditable — each signal gets a "reason" string explaining which factor failed, with actual indicator values captured as `signal_indicators` (permanent snapshot). (`scanner.py:179-249`)
- Signal snapshots include full provenance chain (`_provenance`, `snapshot_sha256`), ensuring every entry is permanently verifiable.

**Critical overfitting risks:**
1. **Multi-testing:** 81 swing + 40 intraday = 121 strategies across ~15 instruments. Many are near-duplicate factor variants (e.g., QQQ LONG has 30+ rank variants differing only in one additional EMA/SMA condition). The mining process (`strategy_miner.py`) was run on the same 5Y dataset → with 121 tests at α=5%, expected false positives ≈ 6 strategies purely by chance. The live WR of 66% on 62 total trades (avg ~0.5 trades/strategy) provides zero statistical power for individual strategies.

2. **DISABLED_STRATEGIES bias (config.py:757-762):** Four strategies disabled AFTER observing live losses:
   - `(#3SW, XLK LONG, -₹1,262)`, `(#4SW, XLK LONG, -₹999)`, `(#68SW, QQQ LONG, -₹1,043)`, `(#28ID, QQQ LONG, -₹1,112)`.
   - Comments explicitly state: "losing money" as motivation. This is textbook **data-snooping on live OOS data** — the live data is no longer "out of sample" once adaptive management is applied. Reported live WR is upward biased by these removals. 
   - The same applies to `GAP_DOWN_B` (disabled after -₹32K in live) and IPO strategies (disabled after -₹5,142 + -₹842).
   - **Statistical impact:** If the 6 disabled strategies were active, the live WR would be ~57-60% (estimated from their low backtest WRs). The true forward WR is unknowable from this data.

3. **Rank-level aggregation (circuit breaker, strategy_stats):** `update_strategy_stats()` keys by integer `pattern_rank` only — rank #1 aggregates 18 swing + 8 intraday rows across different instruments. A pause of rank #1 stops all 26 strategies simultaneously. This is acknowledged in config.py:662-665 but not fixed. For production, the key should be `(rank, ticker, timeframe)` to isolate strategy families.

4. **N=62 is underpowered:** For a claimed WR of 66%, the 95% exact Clopper-Pearson CI is [53%, 77%]. A single strategy with 5 live trades (typical) has essentially no measurable edge: the CI spans [20%, 92%]. No individual strategy has been validated live; only the portfolio aggregate is informative, and that aggregate is biased by selective disabling.

**Verdict: C+** — The indicator computation and signal evaluation are technically excellent and fully auditable. However, the strategy selection methodology (mine on same data, deploy live, disable losers, report winner WR) creates an upward-biased performance picture that does not constitute valid OOS validation. For production: (a) pre-register disable rules BEFORE looking at live results, (b) report raw (all strategies) AND managed (post-disable) live WR separately, (c) require per-key minimum sample sizes (n≥30) before any strategy is considered validated, and (d) correct for multiple comparisons (Bonferroni/Benjamini-Hochberg on 121 tests).

---

## 5. Top 3 Recommendations for Production Readiness

### 5.1 File-Level Atomicity + Locking (Move from implicit to explicit concurrency control)

**Problem:** CSV writes are non-atomic (`df.to_csv()` lines 1253, 2299-2301, 800) with no file locking. Current race protection relies solely on GitHub Actions `concurrency: group` — correct today but fragile: any new workflow, local run, or external tool accessing the same CSV reintroduces data races.

**Solution:**
- Replace all `df.to_csv(PAPER_FILE)` calls with a temp-file-then-`os.replace()` pattern (already used for JSON in `_atomic_write_json`).
- Add a lockfile (e.g., `fcntl.flock` on Linux, `msvcrt.locking` on Windows) around `enter_trade()` / `update_trades()` / `process_open_trades()` to serialize writers at the storage layer.
- Unify `save_portfolio()` in `live_pnl_updater.py` to use the same atomic write as `paper_trader.py` (currently duplicated — live uses non-atomic `open()`).
- **Impact:** Prevents data corruption on process timeout/kill; enables safe concurrent access from non-GitHub-Actions contexts (local testing, VM deployment).

### 5.2 Realistic Fill Model (Close the live-vs-bot parity gap)

**Problem:** The live updater (`live_pnl_updater.py:440-810`) does not model gap-through fills — fills are always at the exact SL/TP price (with slippage PCT). The bot path (`paper_trader.py:131-241`) has gap-aware fills via `_bars_sl_tp()` but only when full bar data is supplied. The live path's `fetch_live_ohlc()` returns only aggregate `{close,high,low,date}` — no bars → gap-fill logic is never invoked live.

**Solution:**
- Modify `fetch_live_ohlc()` to return full 1m bar data (at minimum the most recent session's bars) alongside the aggregate summary.
- Apply `_bars_sl_tp()` gap-fill logic in `process_open_trades()` when a gap is detected (open beyond SL → fill at open).
- Add a **per-bucket aggregate risk cap** (sum of per-trade risk ≤ 50% of bucket equity) to bound portfolio-level VaR for real capital.
- Add a **stochastic slippage module** (sample from empirical distribution calibrated to Zerodha/IBKR fills) to replace fixed PCT slippage. This models fat-tail fill degradation (e.g., 0.01% median slippage on SPY, but 0.5-2% on a 0.34 TRX flash crash).
- **Impact:** Paper P&L will degrade (estimated -10-25% on net returns) — but this is the *correct* performance picture for real deployment. A degraded but honest paper track record is worth more than an inflated one.

### 5.3 Statistical Hygiene + Pre-Registered Selection Rules

**Problem:** The 121-strategy universe was mined on the same data it's tested on (in-sample → live). Four strategies are disabled based on live losses (data-snooping). Individual strategies have <10 live trades (no statistical power). Live WR is reported only on the managed (post-disable) portfolio.

**Solution:**
- **Pre-register disable rules** before the first live trade: e.g., "disable if PnL < 0 AND n ≥ 10 AND Bayesian posterior P(edge<0) > 95%." Apply the rule mechanically, not after seeing results.
- **Report two metrics in PROJECT_AUDIT_SUMMARY.md:** (a) Raw: all strategies active, no adaptive overrides (the true OOS performance); (b) Managed: current post-disable performance (what you're running). The gap between these is your bias estimate.
- **Correct for multiple comparisons:** Apply Benjamini-Hochberg FDR control across the 121 strategy tests at each review date. Only strategies with FDR-adjusted p < 0.10 should be considered for live enablement.
- **Require minimum sample size:** No strategy should be claimed as validated with fewer than 30 closed trades. For n=30, the 95% CI on WR narrows to ±17% — still wide, but actionable for portfolio-level decisions.
- **Impact:** This may delay "production readiness" by months (waiting for sufficient live sample size) — but the alternative is deploying on a track record that is biased by construction. The honest path is: run the system on paper for 6-12 months (250+ trades), apply pre-registered rules mechanically, and only then consider real capital.

---

## Additional Findings (Not in Original 5 Questions)

### Minor Issues

1. **Doc inconsistency:** `PROJECT_AUDIT_SUMMARY.md` line 173 states US Swing = 32 trades / 17W-11L = 60.71%. Actual CSV = **31** closed US SWING_1d trades (table sums correctly to 62 total). One trade may have been migrated between modes or miscounted. Fix doc.

2. **`keepalive_guard` queue inflation:** The keepalive self-reschedule (`live_pnl.yml:keepalive-guard`) queues a new workflow run every ~2 min while time-sensitive positions are open. With `cancel-in-progress: false`, each queued run waits its turn — if runs are queued faster than they execute (8-min timeout × queued runs), backlog grows and consumes free-tier minutes. Consider `cancel-in-progress: true` for live_pnl (runs are stateless scans — canceling a queued run loses nothing).

3. **`live_pnl_updater.load_portfolio()` (line 321-333) has its own default dict** missing `US_FADE`, `LONG_BOUNCE`, and `IPO` keys — different from `paper_trader._default_portfolio()`. If the `except` branch fires (JSON parse error), the returned dict writes incomplete capital_by_market on next save. Impact: low (only on JSON corruption).

4. **`health_check.py:now_ist()`** uses `datetime.utcnow() + timedelta(5,30)` — produces a **tz-naive** datetime that compares correctly against other tz-naive IST timestamps, but would break if passed to tz-aware code. Correct for its current use.

5. **Wasteful `load_portfolio()` call in `calculate_qty()`:** Called once per entry; in `enter_trade()`, `load_portfolio()` is called again (line 1123). Two reads per entry is redundant — refactor to pass the portfolio dict through.

---

## Final Verdict

| Area | Grade | Summary |
|------|-------|---------|
| **Architecture** | **B+** | Single-source-of-truth design is correct; concurrency protection is platform-level, not storage-level. |
| **Position Sizing** | **B** | Conservative (1% risk, isolated buckets), but missing aggregate risk cap and notional margin model for real capital. |
| **Execution Realism** | **B-** | Fee models accurate; live updater systematically optimistic on gap fills (missing bars data). |
| **Strategy Quality** | **C+** | Indicators computed correctly; strategy selection methodology creates survivorship bias. N=62 is underpowered. |
| **Production Readiness** | **C** | Requires atomic CSV writes, realistic fill parity, and pre-registered statistical discipline before real capital deployment. |

**Bottom line:** The system is an excellent paper-trading and strategy-validation engine — far above average for retail algo-trading. The code quality is high, the documentation is thorough, and the ledger integrity system (SHA-256 chain + integrity_check + 377 tests) is institutional-grade. However, the gap between "well-built paper system" and "production-ready for real capital" is substantial: the fill model is optimistic, the strategy selection is biased by adaptive management, and the concurrency model is implicitly dependent on GitHub Actions' concurrency group. The top 3 recommendations above address these gaps directly.

---

*Audit completed: Sep 16 2026 | Auditor: opencode/big-pickle | Model ID: opencode/big-pickle*

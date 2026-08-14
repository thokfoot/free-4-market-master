
## 2026-08-13 22:01 IST — RAW-DATA INDEPENDENT RE-VERIFICATION (user: 'check again, independently')

User ne doubt kiya ki backtest inflation ho sakta hai. MAIN NE FRESH CODE + RAW PARQUET se sab kuch re-verify kiya (koi npz/engine/prep code share nahi):

=== 1h FADE (config: shoot 3% vs prev 1h close, vol 2.2x, RSI>=70, 10:00-12:59 IST, prev-day-high-break, SL1.5/TP3.75, costs 0.1%) ===
- Raw 2390 files se fresh build (indep_verify_raw.py)
- OOS window (last 40% days, ~8 months): 3640 signals -> realistic 1-pos-at-a-time: 333 trades, win 44.3%, avg +0.282%/trade
- REALISTIC: Rs 1L -> Rs 2.87L = +187.3% (maxDD -19.3%) | after 15% tax ~+159%
- => 1h edge INDEPENDENTLY CONFIRMED on raw data, OOS me bhi strong.

=== 5m FADE (config: shoot 3%/90m, vol 3x, RSI>=65, SL1.5/TP3.75, prev-day-high-break) ===
- Raw 2370 files se fresh build (indep_verify_5m_raw.py)
- Full 57d: 9637 signals -> realistic 561 trades = +87.1%
- BUT day-split: TRAIN +90.7% vs TEST -3.4%  => 5m edge OOS me FAIL (overfit)
- Matches pichla audit (5m fragile).

CONCLUSION: Sirf 1h FADE deploy-worthy hai. 5m wale configs paper trade ke liye nahi.
Files: indep_verify_raw.py, indep_verify_5m_raw.py

## 2026-08-13 22:26 IST — V1 (raw-verified 1h FADE) PAPER TRADE ADD (v5.19)

User: 'theek hai jo tumko confirm hai or lagta hai ki beneficial hai usko paper trade me add kr do.'

- config.py: V1 added to FADE_VARIANTS (rank 991, 1h, shoot 3% vs prev-1h-close [close_close],
  vol 2.2x, RSI>=70, prev-day-high break, SL 1.5%/TP 3.75%, 10:00-12:59 IST, cap 2/day,
  own FADE ₹1L bucket). Total FADE variants: 41.
- scanner_fade.py: _shoot_series shoot_mode param (roll_low default / close_close), WINDOWS 1000_1300
  added, shoot_cache mode-aware (fixed read-site KeyError).
- Verified: close_close shoot unit tests pass, V1 fires in scanner (rank 991 in signals),
  bot imports OK, full pytest suite 343 passed.
- Backtest backing (indep_verify_raw.py, fresh raw parquet): OOS window ~8mo, realistic
  1-pos-at-a-time +187.3% (333 trades, win 44.3%, maxDD -19.3%, after 15% tax ~+159%).

## 2026-08-13 22:45 IST — V1 FULL RE-CHECK (user: 'check again. sab kuch')

1. config.py: V1 block correct (rank 991, 1h, 3.0% close_close, vol 2.2x, RSI>=70,
   prev-high break, SL1.5/TP3.75, 10:00-12:59, cap 2). FADE_VARIANTS=41, no dup ranks
   (46 total with US/LONG, all unique).
2. scanner_fade.py: diff clean - close_close mode, WINDOWS 1000_1300 (270,450 UTC),
   shoot_cache mode-aware (read-site KeyError fixed).
3. bot.py: counts dynamic len(FADE_VARIANTS)=41, FADE scan label dynamic.
   Entry uses variant sl_pct/tp_pct (V1 1.5/3.75 flow correct). TF=FADE_1h.
4. strategy_report: V1 in defs (41 FADE defs, rank 991 present), Never Fired sheet
   includes #991FD. Per-strategy sheets only for traded strats (V1 untraded - normal).
5. Tests: unit tests for close_close + _variant_fired pass, scanner live run shows
   rank 991 in signals (82 sig, 41 variants), bot imports OK, pytest 343 passed.
ALL GOOD.

## 2026-08-14 ~11:00 IST — TG MSG DATA VERIFICATION (user: "doubtfull msg data")

User ne 14 Aug 02:53 IST wali TG message ke data pe doubt kiya (^DJT BUY, BTC expiry exit > OHLC High, Data 39/739, FADE 11T).

### Verification (real market data se, yfinance + git history origin/main):
1. ^DJT BUY 21928.05 = REAL Aug 13 daily close (21928.050781). SL 21489.49 = x0.98, TGT 22805.17 = x1.04 ✓ signal real.
2. BTC-USD expiry: exit 63572.72 = live close 63509.21 x 1.001 (SHORT cover slippage). Entry 63558.02 = signal close 63621.64 x 0.999 (SHORT entry slip). Charges 0.3% crypto. P&L -205.37 math EXACT. "Exit > OHLC High" = slippage model, by design, not a bug.
3. FADE 11 trades = real (paper_trades.csv, entries match market prices at those times; GARUDA TP hit verified on real 15m data).
4. **CONFIRMED BUG #1 — ^DJT trade LOST**: message announced BUY ^DJT but trade exists in NO commit (paper_trades=92, portfolio open=13, audit=307 items, zero DJT anywhere). Bot run at ~21:23 UTC added trade + sent alert, but NO "v5.12 scan" commit exists in that window → commit step failed (timeout 10min / push) → ephemeral GitHub runner destroyed the state. Message claimed 93 trades; repo has 92. **Alert ≠ portfolio.**
5. **CONFIRMED BUG #2 — Data 39/739**: live runner data fetch mostly fails (Yahoo rate limits on shared runner IP). Local test: 40/40 downloads OK. Runner: only 39/739 tickers OK → most strategies can't evaluate (19/8136), most buckets 0T.
6. **CONFIRMED #3 — V1 (1h FADE) NOT deployed**: local config has 41 FADE variants (V1 rank 991), live message shows FADE 40S + header v5.17. V1 changes uncommitted → not running on GitHub.

### Pending decisions (asked user):
- Deploy V1 (commit+push config/scanner_fade/bot version bump)
- Lost-trade fix: bot.yml timeout 10→20min + immediate post-scan commit
- Data reliability: dedupe downloads / reduce per-run load

## 2026-08-14 — LOST-TRADE FIX (user: "Lost-trade fix apply karo")
Root cause of ^DJT loss: GitHub Actions ephemeral runner — bot added trade + sent TG alert, but final "Commit logs" step never ran (job timeout/push fail) → state destroyed. Fix applied:
1. Timeouts: bot.yml 10→20, fade_scan.yml 12→20, gap_down.yml 10→20 (20 min).
2. New _commit_state_now() helper (bot.py + live_pnl_updater.py): commits state files via .ai/commit_logs.sh IMMEDIATELY after scans/trade-closes, BEFORE any TG send — so entries/exits survive even if the job dies after.
Verified: py_compile OK, imports OK, call placed before TG send in both files.

## 2026-08-14 — DATA RELIABILITY FIX (user: "Data reliability fix")
Root cause of live "Data 39/739": GitHub runner pe Yahoo rate-limit + swing/intraday scans SEQUENTIAL downloads (230 tickers, 3 retries each) → too slow (10-min timeout me ~39 tickers hi mil paaye). Local: 40/40 OK.
Fix applied:
1. bot.py: swing + intraday downloads parallelized (ThreadPoolExecutor 10 workers, per-ticker 3 retries, error logged) — sequential → ~5-8x faster.
2. fade_scan.yml: */5 -> */15 (3x less Yahoo volume; 15m variants don't need 5-min freshness, sirf S5/S9 5m variants up to 10 min late).
Verified: py_compile OK, parallel functional test 12/12 in 13s, pytest 343 passed.

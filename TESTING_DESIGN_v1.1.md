# Testing Design Document v1.1 — FREE 3-Market Paper Trade Bot

**Date:** 29 Jul 2026  
**Version:** v1.1 (Design — No Implementation Yet)  
**Status:** ⏳ Awaiting Approval Before Any Test Code Is Written

**Changes from v1.0:**
1. Risk-based coverage model (hybrid: ≥85% floor + 100% critical-path gate)
2. Updated replay verification — all observable outputs, not just files
3. Added Mutation Testing plan (Section 8)
4. Versioned replay baselines with symlink approach
5. Explicit replay baseline change rules table

---

## 1. Test Architecture

### 1.1 Test Framework

| Decision | Choice | Rationale |
|:---------|:-------|:----------|
| Framework | **pytest** (≥7.0) | Python standard, excellent fixture system, parametrize support, no boilerplate |
| Runner | `pytest -v` | Verbose output, clear pass/fail per test |
| Assertions | Built-in `assert` | No need for `unittest.TestCase` — pytest rewrites assert messages |
| Coverage | `pytest-cov` | Standard, integrates with GitHub Actions, supports per-file thresholds |
| Determinism | `pytest --randomly` | Detects test order dependencies (long-term hardening) |

### 1.2 Folder Structure

```
free-4-market-master/
├── tests/
│   ├── __init__.py
│   ├── conftest.py              # Shared fixtures (pytest auto-discovery)
│   ├── fixtures/
│   │   ├── __init__.py
│   │   ├── sample_trades.py     # Reusable trade dict factories
│   │   ├── sample_portfolio.py  # Reusable portfolio state factories
│   │   └── sample_data.py       # Pre-computed OHLC/indicator data for deterministic tests
│   ├── core/
│   │   ├── test_position_sizing.py
│   │   ├── test_trade_lifecycle.py
│   │   ├── test_exit_logic.py
│   │   ├── test_pnl.py
│   │   ├── test_strategy_stats.py
│   │   ├── test_portfolio.py
│   │   └── test_data_validation.py
│   ├── regression/
│   │   └── test_historical_bugs.py
│   ├── replay/
│   │   └── test_deterministic_replay.py
│   └── helpers/
│       ├── assertions.py         # Custom assertion helpers (e.g., assert_trade_equal)
│       └── file_comparator.py    # Deep compare CSV/JSON outputs
├── .github/workflows/
│   ├── bot.yml                   # Existing: production trading
│   ├── live_pnl.yml              # Existing: live P&L monitoring
│   └── test.yml                  # NEW: regression + replay + coverage CI
```

### 1.3 Naming Conventions

| Element | Convention | Example |
|:--------|:-----------|:--------|
| Test files | `test_<behavior>.py` | `test_position_sizing.py` |
| Test functions | `test_<behavior>_<scenario>` | `test_long_swing_us_reasonable_qty` |
| Test classes | *Avoid classes* — use plain functions + fixtures | — |
| Fixture functions | `<descriptive_name>` | `long_swing_us_trade`, `crypto_intraday_short_trade` |
| Fixture files | `<category>_fixtures.py` | `sample_trades.py` |
| Markers | `@pytest.mark.slow`, `@pytest.mark.replay` | — |

### 1.4 Fixture Strategy

**Layered fixtures** — each layer builds on the previous:

```
Layer 0: Constants
  → CAPITAL_BY_MARKET, SL_PCT, TP_PCT, CHARGES_PER_MARKET, etc.
  → Direct import from config.py

Layer 1: Trade Dicts
  → Pre-built trade rows for every combination:
    - LONG/SHORT × SWING/INTRADAY × India/US/Crypto
  → Each is a plain dict matching COLUMNS schema
  → e.g., `long_swing_us_trade` → `{"Ticker": "SPY", "Direction": "LONG", ...}`

Layer 2: Portfolio States
  → Pre-built portfolio.json shapes:
    - Empty portfolio (no open positions)
    - Portfolio with 1-3 open positions
    - Portfolio with mixed swing + intraday
    - Portfolio with per-market P&L history

Layer 3: Mocked Dependencies
  → monkeypatch for:
    - datetime.now(IST) → fixed timestamp
    - yfinance → returns fixture data (not real API)
    - os.getenv → returns test tokens or empty
    - pd.read_csv → returns fixture DataFrame
```

### 1.5 Mocking Strategy

| External Dependency | Mock Strategy |
|:--------------------|:--------------|
| **yfinance** (`yf.download`) | Monkeypatch with pre-downloaded fixture data (frozen CSV from known dates). All tests use the same historical slice so outputs are deterministic. |
| **Telegram API** (`requests.post`) | Monkeypatch with a no-op that captures the sent message into a `list` for assertion. Never calls real Telegram. |
| **File I/O** (`pd.read_csv`, `json.load`, `json.dump`) | Tests that write files use `tmp_path` fixture. Tests that read use fixture data in `tests/fixtures/` |
| **`datetime.now(IST)`** | Freeze with a known timestamp using monkeypatch + fixed `datetime` |
| **Environment variables** | Set in `conftest.py` or per-test via monkeypatch |

### 1.6 Data Fixtures

**Static (committed to repo):**
```
tests/fixtures/data/
├── spy_3mo_daily.csv        # 60 rows of daily SPY data
├── iwm_10d_1h.csv           # ~240 rows of 1h IWM data
├── ada_3mo_daily.csv        # 60 rows of daily ADA data
├── ^NSEBANK_3mo_daily.csv   # 60 rows of daily Bank Nifty data
└── empty.csv                # Empty DataFrame for edge-case testing
```

These are **small slices** (not the full backtest files) — just enough to exercise indicator computation and signal generation.

### 1.7 Replay Fixtures (Versioned)

A versioned fixture directory for the deterministic replay testing. Each engine version gets its own baseline directory, preserving historical outputs for cross-version comparison.

```
tests/fixtures/replay/
├── v5.8/
│   ├── replay_input.csv              # Historical ticker data (frozen)
│   ├── replay_expected_trades.csv    # Expected paper_trades.csv output
│   ├── replay_expected_portfolio.json
│   ├── replay_expected_audit.json
│   ├── replay_expected_strategy_stats.json
│   ├── replay_expected_telegram.json # Captured Telegram payloads (from mock)
│   └── replay_expected_capital.csv   # Capital timeline per day
├── v5.9/
│   └── ...
├── v6.0/
│   └── ...
└── current -> v5.8/                  # Symlink — compared against by default
```

**Version management:**
- New baselines are generated with `--replay-version=v5.9` flag
- A symlink `current` is updated to point to the active version
- Historical versions remain in the repo for reproducibility
- To compare across versions: `python -c "compare_replay(v5.8, v5.9)"`

### Replay Baseline Change Rules

Not every code change requires a new replay baseline. The following table defines which changes require regeneration:

| Change | New Baseline Required? |
|:-------|:---------------------:|
| Refactor (no output change) | ❌ No |
| Bug fix affecting trade outputs (SL/TP/P&L/qty) | ✅ Yes |
| New strategy added | ✅ Yes |
| Position sizing change | ✅ Yes |
| SL/TP logic change | ✅ Yes |
| Charges formula change | ✅ Yes |
| Entry/exit conditions change | ✅ Yes |
| Logger formatting | ❌ No |
| Telegram wording | ❌ No |
| Comment/doc changes only | ❌ No |
| Import reordering | ❌ No |
| Test-only additions | ❌ No |

Baseline regeneration is always an **explicit action** (`--replay-generate-golden`) — never automatic.

---

## 2. Test Matrix

### 2.1 Position Sizing (`test_position_sizing.py`)

Covers `paper_trader.calculate_qty()` — 22 test cases:

| # | Test Case | Direction | Market | Timeframe | Entry | SL | Expected Qty | Rationale |
|:-:|:----------|:---------:|:------:|:---------:|:-----:|:--:|:------------:|:----------|
| 1 | Long Swing US | LONG | US | SWING_1d | 500.00 | 490.00 | 200 | ₹1,000 risk / ₹10 risk-per-share |
| 2 | Long Swing India | LONG | INDIAN | SWING_1d | 2500.00 | 2450.00 | 20 | ₹1,000 risk / ₹50 risk-per-share |
| 3 | Long Swing Crypto | LONG | CRYPTO | SWING_1d | 0.16 | 0.1568 | 62500→**50000** | Capped at 50,000 (entry < 0.1? No → check) |
| 4 | Short Swing US | SHORT | US | SWING_1d | 100.00 | 102.00 | 500 | ₹1,000 risk / ₹2 risk-per-share |
| 5 | Short Swing India (should fail) | SHORT | INDIAN | SWING_1d | 100.00 | 102.00 | 0 | `ALLOW_SHORT["INDIAN"] = False` |
| 6 | Short Swing Crypto | SHORT | CRYPTO | SWING_1d | 0.50 | 0.51 | 100000→**10000** | Capped at 10,000 (entry < 1) |
| 7 | Long Intraday US | LONG | US | INTRADAY_1h | 200.00 | 198.00 | 500 | Uses INTRADAY capital (₹1,00,000) |
| 8 | Short Intraday Crypto | SHORT | CRYPTO | INTRADAY_1h | 1.50 | 1.53 | 3333 | Uses INTRADAY capital |
| 9 | Zero Risk Distance | LONG | US | SWING_1d | 100.00 | 100.00 | 0 | SL = Entry → no risk → qty = 0 |
| 10 | High Price Cap (>100) | LONG | US | SWING_1d | 1000.00 | 990.00 | 100→**5000** | Capped at 5,000 |
| 11 | Low Price Cap (<0.1) | LONG | CRYPTO | SWING_1d | 0.05 | 0.049 | 200000→**50000** | Capped at 50,000 |
| 12 | Mid Price Cap (<1) | LONG | US | SWING_1d | 0.80 | 0.79 | 100000→**10000** | Capped at 10,000 |
| 13 | Exact SL Distance | LONG | US | SWING_1d | 100.00 | 99.00 | 100 | ₹1,000 / ₹1.00 = 100 |
| 14 | Fractional SL Distance | LONG | US | SWING_1d | 50.00 | 49.50 | 200 | ₹1,000 / ₹0.50 = 200 |
| 15 | Minimum Qty Floor | LONG | US | SWING_1d | 10000.00 | 9999.00 | 1 | ₹1,000 / ₹1 = 1000 but min(1) is floor |
| 16 | Duplicate Ticker Rejection | — | — | — | — | — | None | Test in lifecycle, not sizing |
| 17-22 | Parametrized: all 6 market+tf combos | — | — | — | — | — | — | `@pytest.mark.parametrize` |

### 2.2 Trade Lifecycle (`test_trade_lifecycle.py`)

Covers `paper_trader.enter_trade()` — 12 test cases:

| # | Test Case | Description |
|:-:|:----------|:------------|
| 1 | Successful Entry | Enter valid LONG → returns trade dict with correct fields |
| 2 | Successful Entry (SHORT) | Enter valid SHORT → SL above entry, TP below entry |
| 3 | Duplicate Prevention | Enter same ticker+direction twice → second returns None |
| 4 | Different Direction Allowed | Enter LONG then SHORT on same ticker → both allowed |
| 5 | Different Ticker Allowed | Enter SPY + QQQ → both allowed |
| 6 | Max Concurrent Swing | Fill 5 swing positions → 6th returns None |
| 7 | Max Concurrent Intraday | Fill 3 intraday positions → 4th returns None |
| 8 | Swing + Intraday Both Allowed | 5 swing + 3 intraday = 8 total allowed (separate limits) |
| 9 | CSV Appended | After enter, CSV has 1 more row |
| 10 | Portfolio Updated | After enter, portfolio.open_positions count increments |
| 11 | Audit Logged | After enter, trade_audit.json has ENTRY event |
| 12 | Strategy Rank in Reason | Reason field contains `#46SW` format |

### 2.3 Exit Logic (`test_exit_logic.py`)

Covers `paper_trader.update_trades()` — 20 test cases:

| # | Test Case | OHLC Data | Expected Exit |
|:-:|:----------|:----------|:--------------|
| 1 | LONG SL by Low | low=SL-0.01, high=unchanged, close=above | ✅ EXIT: SL Hit (intraday) |
| 2 | LONG SL by Close only | low=above SL, close=SL-0.01 | ✅ EXIT: SL Hit (close) |
| 3 | LONG TP by High | high=TP+0.01, low=unchanged, close=below | ✅ EXIT: Target Hit |
| 4 | LONG TP by Close only | high=below TP, close=TP+0.01 | ✅ EXIT: Target Hit (close) |
| 5 | SHORT SL by High | high=SL+0.01, low=unchanged, close=below | ✅ EXIT: SL Hit (intraday) |
| 6 | SHORT SL by Close only | low=above SL, close=SL+0.01 | ✅ EXIT: SL Hit (close) |
| 7 | SHORT TP by Low | low=TP-0.01, high=unchanged, close=above | ✅ EXIT: Target Hit |
| 8 | SHORT TP by Close only | high=below TP, close=TP-0.01 | ✅ EXIT: Target Hit (close) |
| 9 | No Exit — Prices in Range | All prices between SL and TP | ❌ No exit |
| 10 | LONG Expiry (daily) | MaxHold=5 days, entry+6 days | ✅ EXIT: Expiry 6d |
| 11 | Intraday Expiry | MaxHold=6 hours, entry+7 hours | ✅ EXIT: Expiry 7h |
| 12 | Tolerance Guard — Within 0.01% LOW | low=SL×0.99995 (above tolerance) | ❌ No exit (data noise) |
| 13 | Tolerance Guard — Beyond 0.01% LOW | low=SL×0.99985 (below tolerance) | ✅ EXIT: SL Hit (intraday) |
| 14 | NaN Low | low=NaN, high=valid, close=valid | ❌ No exit (OHLC validation) |
| 15 | NaN High | low=valid, high=NaN, close=valid | ❌ No exit (OHLC validation) |
| 16 | NaN Close | low=valid, high=valid, close=NaN | ❌ No exit (OHLC validation) |
| 17 | Zero Low | low=0, high=valid, close=valid | ❌ No exit (OHLC validation) |
| 18 | Inf High | low=valid, high=inf, close=valid | ❌ No exit (OHLC validation) |
| 19 | None Close | low=valid, high=valid, close=None | ❌ No exit (OHLC validation) |
| 20 | LONG → SHORT SL checked correctly | LONG direction with high>=SL | ❌ No exit (LONG checks LOW for SL) |

### 2.4 P&L Calculation (`test_pnl.py`)

Covers P&L formulas across both files — 10 test cases:

| # | Test Case | Direction | Entry | Exit | Qty | Gross P&L | Charges | Net P&L |
|:-:|:----------|:---------:|:-----:|:----:|:---:|:---------:|:-------:|:--------|
| 1 | LONG Profit | LONG | 100 | 110 | 10 | ₹100 | ₹0.12 | ₹99.88 |
| 2 | LONG Loss | LONG | 100 | 90 | 10 | -₹100 | ₹0.12 | -₹100.12 |
| 3 | SHORT Profit | SHORT | 100 | 90 | 10 | ₹100 | ₹0.12 | ₹99.88 |
| 4 | SHORT Loss | SHORT | 100 | 110 | 10 | -₹100 | ₹0.12 | -₹100.12 |
| 5 | LONG P&L% | LONG | 100 | 110 | — | — | 0.12% | +9.88% |
| 6 | SHORT P&L% | SHORT | 100 | 90 | — | — | 0.02% | +9.98% (US rate) |
| 7 | Crypto Charges (0.30%) | LONG | 0.16 | 0.18 | 10000 | ₹200 | ₹4.80 | ₹195.20 |
| 8 | India Charges (0.12%) | LONG | 2500 | 2600 | 40 | ₹4,000 | ₹12.00 | ₹3,988.00 |
| 9 | SHORT US Charges (0.02%) | SHORT | 500 | 480 | 10 | ₹200 | ₹0.10 | ₹199.90 |
| 10 | Charges Deducted Once | LONG | 100 | 110 | 10 | ₹100 | verify not double-counted |

### 2.5 Strategy Stats (`test_strategy_stats.py`)

Covers `paper_trader.update_strategy_stats()` — 6 test cases:

| # | Test Case | Description |
|:-:|:----------|:------------|
| 1 | First Trade for Strategy | New rank key created with 1 loss, factors from reason |
| 2 | Second Trade Increases Count | Same rank → increments, does not duplicate key |
| 3 | Win vs Loss Classification | pnl > 0 → wins++; pnl < 0 → losses++; pnl=0 → neither? (decide: pnl=0 → loss?) |
| 4 | Rank Extraction from Reason | `#30ID ...` → rank=30, `#46SW ...` → rank=46, `No rank` → rank=0 (skipped) |
| 5 | Factors Truncated to 80 chars | Reason >80 chars → factors=reason[:80] |
| 6 | Factors Updated When Longer | Shorter stored first, longer reason later → factors updated |

### 2.6 Portfolio (`test_portfolio.py`)

Covers `paper_trader.load_portfolio/save_portfolio/enter_trade/update_trades` portfolio updates:

| # | Test Case | Description |
|:-:|:----------|:------------|
| 1 | Default Portfolio | load_portfolio() → correct initial capital_by_market |
| 2 | Capital Unchanged on Entry | Enter trade → capital_by_market values unchanged |
| 3 | Capital Updated on Exit | Exit with P&L +50 → capital_by_market += 50 |
| 4 | Capital Never Negative | Exit with P&L -200,000 → capital_by_market = max(0, cap + pnl) |
| 5 | Per-Market P&L Tracking | Exit INDIAN trade → total_pnl_by_market["INDIAN"] incremented |
| 6 | Total Wins/Losses | 3 wins, 2 losses → total_wins=3, total_losses=2 |
| 7 | Migration from Old Format | Old portfolio.json with "capital" key → migrated to per-market |
| 8 | Atomic Write Crash Recovery | Kill write mid-way → temp file cleaned up, original intact |

### 2.7 Data Validation (`test_data_validation.py`)

Covers OHLC validation guard in `update_trades()`:

| # | Test Case | Input | Expected |
|:-:|:----------|:------|:---------|
| 1 | valid_ohlc | close=100, high=105, low=95 | passes validation |
| 2 | none_close | close=None, high=105, low=95 | rejected, no exit |
| 3 | none_high | close=100, high=None, low=95 | rejected, no exit |
| 4 | none_low | close=100, high=105, low=None | rejected, no exit |
| 5 | nan_close | close=NaN, high=105, low=95 | rejected, no exit |
| 6 | nan_high | close=100, high=NaN, low=95 | rejected, no exit |
| 7 | nan_low | close=100, high=105, low=NaN | rejected, no exit |
| 8 | inf_high | close=100, high=inf, low=95 | rejected, no exit |
| 9 | zero_close | close=0, high=105, low=95 | rejected, no exit |
| 10 | zero_low | close=100, high=105, low=0 | rejected, no exit |
| 11 | negative_high | close=100, high=-5, low=95 | rejected, no exit |

### 2.8 Live P&L Updater Consistency (`test_exit_logic.py` — additional)

Verify that `live_pnl_updater.process_open_trades()` applies identical logic:

| # | Test Case | Description |
|:-:|:----------|:------------|
| 1-20 | Same 20 SL/TP cases as 2.3 | Run same OHLC data through both `update_trades()` and `process_open_trades()` → identical exits |
| 21 | Same tolerance guard | Invalid OHLC data rejected in both |
| 22 | Same charges formula | P&L after charges matches exactly |

---

## 3. Historical Bug Regression Matrix

Every bug from git log → permanent test. Tests live in `tests/regression/test_historical_bugs.py`.

| # | Git Commit | Bug Description | Expected Behavior | Regression Test Name |
|:-:|:-----------|:----------------|:-----------------|:---------------------|
| 1 | `b6fe89b` | **False SL exits at 09:30 IST** — yfinance returned corrupt OHLC data causing `daily_low <= sl` to trigger incorrectly. Prices were ₹2+ above SL. | OHLC validation rejects NaN/0/inf. Trading continues without exit. | `test_no_false_exit_on_corrupt_ohlc` |
| 2 | `0b92bfd` | **Strategy stats double-counting** — `update_trades()` iterated ALL closed rows at the bottom of the function, re-counting previously closed trades on every scan run. | Strategy stats increment **exactly once** per trade — when the trade exits, not on every scan. | `test_strategy_stats_increment_once_per_trade` |
| 3 | `04dd20c` | **No SL/TP telemetry** — When SL/TP hit during live P&L check, no Telegram alert was sent. User had to check manually. | SL/TP/Expiry hit → Telegram alert sent with OHLC values. | `test_sl_tp_alert_sent_on_exit` |
| 4 | `079f307` | **OHLC validation missing in live_pnl_updater** — The live P&L checker could still false-exit on corrupt yfinance 1m data because it lacked the NaN/0/inf guard. | Both `paper_trader.update_trades()` and `live_pnl_updater.process_open_trades()` apply identical OHLC validation. | `test_live_updater_has_ohlc_validation` |
| 5 | `817f398` | **Missing audit fields** — `_log_audit_exit()` in live_pnl_updater was passing `Expected_WinRate` and `Pattern_Factors` to the function, but the function silently dropped them. | Audit log entries contain `expected_win_rate` and `pattern_factors` fields. | `test_audit_log_contains_all_fields` |
| 6 | `4579a81` | **Strategy stats mismatch in live_pnl_updater** — Called with original reason (no exit context) and unrounded PnL. Additionally, missing factors update check and print log. | Both files call with `full_reason` (includes exit_reason suffix) and `round(pnl, 2)`. Both have factors update check. | `test_strategy_stats_identical_between_live_and_paper` |

### Each Regression Test Template

```python
def test_<descriptive_name>(<fixtures>):
    """
    Regression for commit <hash>: <one-line description>
    
    Bug: <detailed description>
    Fix: <what was changed>
    
    This test ensures the bug never returns.
    """
    # 1. SETUP — reproduce the conditions that caused the bug
    # 2. ACT — run the function that had the bug
    # 3. ASSERT — verify the fix holds
```

---

## 4. Replay Testing Design

### 4.1 Purpose

**Guarantee:** Given identical input data, the bot produces **identical output every time**. This catches:
- Non-deterministic behavior (e.g., unseeded randomness, dictionary iteration order, floating point accumulation)
- Regression in indicator computation
- Regression in entry/exit logic
- Data corruption from file IO

### 4.2 Historical Dataset

A frozen snapshot of real market data — **committed to the repo** (small, representative slice):

```
# Daily data (last 60 trading days)
SPY, IWM, ^GSPC, ^BSESN, ^NSEI, ^NSEBANK, BTC-USD, ADA-USD

# 1h data (last 10 days for intraday tickers)
QQQ, ^GSPC, IWM, SPY, XLK, ADA-USD
```

Each file has columns: `Date, Open, High, Low, Close, Volume`  
Exactly 60 daily rows and ~240 1h rows per ticker.

### 4.3 Replay Procedure

```
1. Load frozen price data (bypass yfinance)
2. Run scan_strategies() → produce signal list
3. Run enter_trade() for each signal → produce trade list
4. Advance time by 1 day
5. Run update_trades() → check SL/TP
6. Repeat steps 2-5 for N days
7. Save ALL output files
8. Compare against golden files
```

### 4.4 Expected Outputs — Full Observable Behaviour

Replay verifies **every observable output** the bot produces, not just file contents. This ensures the entire system is deterministic:

| Observable | Compared? | Match Condition | Notes |
|:-----------|:---------:|:----------------|:------|
| `logs/paper_trades.csv` | ✅ Exact match | Every row, every column identical | Including Trade_ID, Entry_Time, Exit_Time |
| `logs/portfolio.json` | ✅ Exact match | JSON structure + numeric values identical | Capital by market, P&L by market, open positions |
| `logs/trade_audit.json` | ✅ Exact match | Every audit event identical | ENTRY and EXIT events with all fields |
| `logs/strategy_stats.json` | ✅ Exact match | Every strategy key identical | Wins, losses, total_pnl per rank |
| `logs/portfolio_snapshots.csv` | ✅ Exact match | Every row identical | Per-scan snapshot of portfolio state |
| **Trade IDs** | ✅ Exact match | UUID values identical | Deterministic via `uuid.uuid4()` monkeypatch |
| **Entry timestamps** | ✅ Exact match | ISO format strings identical | Time frozen via monkeypatch |
| **Exit timestamps** | ✅ Exact match | ISO format strings identical | Time frozen via monkeypatch |
| **Capital timeline** | ✅ Exact match | Per-day capital values identical | Derived from portfolio.json sequence |
| **Telegram payloads** | ✅ Captured & compared | Messages captured in mock list, compared as JSON | Mock replaces `requests.post` with capture |
| **Live P&L snapshots** | ✅ Exact match | Unrealized P&L per position per scan | Derived from OHLC data at each time step |

**Telegram verification detail:** The mock `requests.post` stores every call as `{"url": url, "data": data}` in a list. At the end of replay, this list is compared against golden `replay_expected_telegram.json`. This proves that Telegram messages are deterministic — same markets, same entries, same exits, same formatting.

### 4.5 Tolerance Rules

| Data Type | Tolerance | Rationale |
|:----------|:---------:|:----------|
| P&L values | **Exact** (0.0) | Same data × same formula → same result |
| Capital values | **Exact** (0.0) | Same P&L × same capital → same result |
| Timestamps | **Exact** (0.0) | Time frozen with monkeypatch |
| Qty values | **Exact** (0) | Integer division always same |
| Strategy factors | **Exact** (empty string) | Same rank extraction → same key |

**If any output differs:** CI fails, developer must investigate whether the change is intentional (golden file updated) or a regression (fix required).

### 4.6 Golden File Generation (Versioned)

Baselines are generated **per version** and stored in versioned directories:

```
# Generate baseline for current version:
pytest tests/replay/ --replay-generate-golden --replay-version=v5.9
  → Creates tests/fixtures/replay/v5.9/replay_expected_*.csv/json
  → Updates symlink: tests/fixtures/replay/current -> v5.9/

# Verify against current baseline (default):
pytest tests/replay/
  → Compares actual output against tests/fixtures/replay/current/
  → FAIL if any difference detected

# Run against a specific historical version:
pytest tests/replay/ --replay-compare-against=v5.8
  → Useful when testing whether a change broke backward compatibility
```

**Golden file regeneration is always deliberate:**
- Only when you explicitly run `--replay-generate-golden`
- Only after confirming the changes are correct
- The `git diff` on golden files must be inspected before committing

---

## 5. CI Pipeline

### 5.1 Workflow File

`.github/workflows/test.yml` — triggered on every push to `main`:

```yaml
name: Regression Tests
on:
  push:
    branches: [main]
  pull_request:
    branches: [main]
  workflow_dispatch:

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: '3.11'
      - name: Install dependencies
        run: |
          pip install pytest pytest-cov pandas pyyaml
      - name: Core regression tests
        run: |
          pytest tests/core/ -v
      - name: Historical bug regression tests
        run: |
          pytest tests/regression/ -v
      - name: Deterministic replay tests
        run: |
          python -c "from tests.replay.test_deterministic_replay import run_replay; run_replay()"
      - name: Coverage report
        run: |
          pytest tests/ --cov=paper_trader --cov=live_pnl_updater --cov=scanner \
            --cov=scanner_intraday --cov=logger --cov-report=term-missing
      - name: Check coverage thresholds
        run: |
          pytest tests/ --cov=paper_trader --cov-fail-under=95
          pytest tests/ --cov=live_pnl_updater --cov-fail-under=90
          pytest tests/ --cov=scanner --cov-fail-under=85
      - name: Verify no tracked files changed
        run: |
          git diff --quiet HEAD -- '*.py' '*.csv' '*.json' '*.yml' '*.md' \
          || (echo "❌ Tracked files changed unexpectedly — was a golden file updated?" && exit 1)
```

### 5.2 Pipeline Flow

```
Push to main
    │
    ▼
┌─────────────────────────────┐
│ 1. Install dependencies     │  ← pytest, pytest-cov, pandas
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 2. Core regression tests    │  ← ~60 tests, expected <30s
│    test_position_sizing.py  │
│    test_trade_lifecycle.py  │
│    test_exit_logic.py       │
│    test_pnl.py              │
│    test_strategy_stats.py   │
│    test_portfolio.py        │
│    test_data_validation.py  │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 3. Bug regression tests     │  ← 6 tests, expected <10s
│    test_historical_bugs.py  │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 4. Deterministic replay     │  ← 1 test (multi-step), expected <60s
│    test_deterministic_replay│
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 5. Coverage report          │  ← Generates % coverage per file
│    fail if below thresholds │
└─────────────────────────────┘
    │
    ▼
┌─────────────────────────────┐
│ 6. Verify tracked files     │  ← No unintended file changes
│    (golden file integrity)  │
└─────────────────────────────┘
    │
    ▼
✅ Pass / ❌ Fail
```

### 5.3 Scheduled Run

In addition to push-triggered, run **once daily at midnight UTC** to catch any environmental drift:

```yaml
on:
  schedule:
    - cron: '0 0 * * *'   # Every day at midnight UTC
```

---

## 6. Coverage — Risk-Based Model

### 6.1 Principle

Coverage percentages are a **signal**, not a goal. The goal is that every **risk-relevant code path** is tested. A file with 98% coverage can still miss the one branch that loses money.

This section defines two tiers:
1. **Risk Coverage** (primary) — critical functions + branches + historical bugs at 100%
2. **File Coverage** (secondary) — minimum percentage floor per file

### 6.2 Risk Coverage — Must Be 100% Tested

| Risk Category | What It Covers | Why |
|:-------------|:---------------|:----|
| **Critical Functions** | `calculate_qty()`, `enter_trade()`, `update_trades()`, `_calc_unrealized_pnl()`, `_send_sl_tp_alert()`, `update_strategy_stats()`, `_extract_rank()`, `load_portfolio()`, `save_portfolio()` | Every trade's correctness depends on these |
| **Critical Branches** | All 22 position-sizing combos, all 20 SL/TP exit combos, all 11 OHLC validation combos, all 5 charges deduction paths, all 4 P&L formulas | Edge cases that cause financial loss |
| **Historical Bugs** | All 6 regression tests from Section 3 | Permanently prevents regression |
| **Cross-File Consistency** | paper_trader vs live_pnl_updater: identical P&L, identical exit logic, identical audit fields | Ensures both files stay in sync |

### 6.3 File Coverage — Minimum Floor

These provide a basic **safety net** against untested new code:

| File | Minimum Target | Rationale |
|:-----|:-------------:|:----------|
| `paper_trader.py` | **≥85%** | Core engine — risk coverage already catches critical paths. Floor prevents large untested additions. |
| `live_pnl_updater.py` | **≥85%** | Second core file — same reasoning. |
| `scanner.py` | **≥80%** | Mathematical logic — some branches hard to fixture. |
| `scanner_intraday.py` | **≥80%** | Mirror of scanner.py. |
| `logger.py` | **≥65%** | File IO — some error branches impractical to test. |
| `config.py` | **≥90%** | Constants + `get_region()` — low risk. |
| `bot.py` | **~40-50%** | Orchestration — expensive to mock end-to-end. Critical-path only. |

**These floors are deliberately lower than v1.0 targets.** The real quality comes from risk coverage, not line percentages.

### 6.4 Exclusions

The following are **explicitly excluded** from coverage targets:

- `requests.post` to Telegram API — mocked in all tests
- `yf.download` — monkeypatched in all tests
- `logger.log_error` — tested implicitly via side effect capture
- `generate_portfolio_report()` — HTML generation, tested in replay only

---

## 7. Deliverables & Phasing (Updated for v1.1)

| Phase | Deliverable | Files | Estimated Effort | Depends On |
|:------|:-----------|:------|:----------------:|:-----------|
| **1** ✅ | **Design Document** | `TESTING_DESIGN_v1.1.md` | 1 session | — |
| **2** | **Fixtures** | `tests/conftest.py`, `tests/fixtures/*.py`, fixture data CSVs | 1 session | Phase 1 approval |
| **3** | **Core Regression Tests** | `tests/core/test_position_sizing.py`, `test_trade_lifecycle.py`, `test_exit_logic.py`, `test_pnl.py` | 2 sessions | Phase 2 |
| **4** | **Extended Core Tests** | `tests/core/test_strategy_stats.py`, `test_portfolio.py`, `test_data_validation.py` | 1 session | Phase 2 |
| **4.5** 🔥 **NEW** | **Test Validation (Mutation Testing)** | `tests/mutation/` — deliberate breakage tests | 1 session | Phase 4 |
| **5** | **Historical Bug Regression** | `tests/regression/test_historical_bugs.py` | 1 session | Phase 2 |
| **6** | **Deterministic Replay** | `tests/replay/test_deterministic_replay.py`, versioned golden files | 2 sessions | Phase 2 |
| **7** | **CI Automation** | `.github/workflows/test.yml` | 1 session | Phase 3-6 |
| **8** | **Coverage Hardening** | Backfill uncovered branches | 1 session | Phase 7 |

### Phase 4.5 Detail — Test Validation (Mutation Testing)

After the core regression suite is built, we intentionally break the engine to prove the tests catch every breakage:

| Mutation | What Changes | Expected Result |
|:---------|:-------------|:----------------|
| SL comparison `>=` → `>` | `daily_low <= sl * tolerance` → `daily_low < sl * tolerance` | Exit with exact SL at LOW should **not** exit — but should have. Test fails = bug detected. |
| LONG P&L → SHORT P&L | `(price - entry) * qty` → `(entry - price) * qty` | P&L calculation inverted. Test fails. |
| Remove charges deduction | Comment out `pnl -= charges` | P&L inflated. Test fails. |
| Remove OHLC validation guard | Comment out `_invalid_ohlc` block | NaN data causes false exit. Test fails. |
| Remove duplicate prevention | `if exists: return None` → removed | Duplicate entries allowed. Test fails. |
| Strategy stats double-count | `stats[key]["wins"] += 1` called twice | Stats overcounted. Test fails. |

Each mutation is a **standalone test** in `tests/mutation/test_suite_catches_mutations.py`. It monkeypatches the production function, runs the relevant regression tests, and asserts that **at least one test fails**. If all tests pass → the mutation went undetected → the test suite has a gap.

### Total Estimated Effort: **~10-11 sessions** (v1.1 adds Phase 4.5)

### Risk Assessment

| Risk | Likelihood | Mitigation |
|:-----|:-----------|:-----------|
| Fixture data drifts from live data | Low | Replay golden files detect drift |
| Pytest version differences | Low | Pin `pytest>=7.0` in CI |
| Monkeypatch conflicts between tests | Medium | Each test gets fresh monkeypatch via `monkeypatch` fixture |
| Replay test too slow | Medium | Limit to 5 tickers × 20 days replay |
| Floating point tolerance mismatches | Low | Exact match — if P&L differs by ₹0.0001, golden file must be regenerated |
| Mutation tests flaky | Low | Each mutation patches one function, runs subset of tests — deterministic

---

## 8. Mutation Testing — Proving the Tests Have Teeth

### 8.1 Why Mutation Testing?

A normal regression suite proves one thing only: **existing code still passes the tests.** It does NOT prove that the tests would catch a bug. Mutation testing fills this gap by intentionally breaking the engine and asserting the tests detect every breakage.

If a mutation passes all tests → the test suite has a blind spot → the mutation must be covered.

### 8.2 Mutation Test Design

Each mutation is a **standalone pytest test** in `tests/mutation/test_suite_catches_mutations.py`.

Pattern:
```python
def test_mutation_<name>(monkeypatch):
    """
    MUTATION: <what was changed>
    
    Expected result: <regression test name> should fail.
    """
    # 1. Apply mutation via monkeypatch
    monkeypatch.setattr("paper_trader._original_function", mutated_function)
    
    # 2. Run the relevant regression test
    # Must be done via subprocess to get fresh import state
    result = subprocess.run(
        ["pytest", "tests/core/test_position_sizing.py", "-x", "--no-header", "-q"],
        capture_output=True, text=True, timeout=30
    )
    
    # 3. Assert that the test FAILED (mutation was detected)
    assert result.returncode != 0, (
        f"MUTATION UNDETECTED: {description}\n"
        f"Test suite passed despite breaking change.\n"
        f"Add a test that covers this branch."
    )
```

### 8.3 Mutation Catalogue

| # | Mutation | What Changes | Expected Detection |
|:-:|:---------|:-------------|:-------------------|
| M1 | **SL threshold `<=` → `<`** | `daily_low <= sl * _TOLERANCE` → `daily_low < sl * _TOLERANCE` | `test_exit_logic` — SL at exact boundary no longer triggers |
| M2 | **TP threshold `>=` → `>`** | `daily_high >= target / _TOLERANCE` → `daily_high > target / _TOLERANCE` | `test_exit_logic` — TP at exact boundary no longer triggers |
| M3 | **LONG P&L → SHORT P&L** | `(price - entry) * qty` → `(entry - price) * qty` for LONG | `test_pnl` — profit becomes loss |
| M4 | **Remove charges deduction** | `pnl -= charges` commented out | `test_pnl` — net P&L equals gross (inflated by charges) |
| M5 | **Remove OHLC validation guard** | `if _invalid_ohlc: continue` removed | `test_data_validation` — NaN data causes false exit |
| M6 | **Remove duplicate prevention** | `if ticker_exists: return None` removed | `test_trade_lifecycle` — duplicate entry succeeds |
| M7 | **Strategy stats double-count** | `stats[key]["wins"] += 1` called twice | `test_strategy_stats` — win count overcounted |
| M8 | **Swap LONG/SHORT SL check** | LONG checks High for SL, SHORT checks Low for SL | `test_exit_logic` — wrong SL direction allows false exit |
| M9 | **Remove tolerance guard** | `_TOLERANCE = 0.9999` → `_TOLERANCE = 1.0` | `test_exit_logic` — 1-cent noise triggers false exit |
| M10 | **Invert capital update sign** | `capital += pnl` → `capital -= pnl` | `test_portfolio` — capital moves wrong direction |

### 8.4 When Mutation Tests Run

Mutations are **NOT run on every CI push** — they take too long (10 subprocess calls × ~20s each = ~200s). Instead:

- **Trigger:** `workflow_dispatch` only (manual)
- **Frequency:** After major changes or before release
- **Result:** If any mutation passes, the test suite has a gap and must be fixed before release

### 8.5 Mutation Test Workflow

```
Manually trigger mutation test run
    │
    ▼
┌────────────────────────────────┐
│ Run mutation M1                │
│ Patch paper_trader.py          │
│ Run affected tests              │
│ Assert: at least 1 test FAILS  │
└────────────────────────────────┘
    │
    ▼ (repeat for M2-M10)
┌────────────────────────────────┐
│ All 10 mutations DETECTED?     │ ← If NO → add missing test case
└────────────────────────────────┘
    │
    ▼
✅ Suite validated / ❌ Gap found
```

---

## Approval

**Status:** ⏳ Awaiting review and approval before any test code is written.

Once approved:
1. Phase 2 begins — fixtures directory + reusable test objects
2. No core engine files are modified during any testing phase
3. Golden files are generated in a controlled environment and committed verbatim

---

*End of Testing Design Document v1.1*

# 🤖 HEALTH CHECK REPORT
*Generated:* 2026-08-13 12:20 IST

## 1. Portfolio

| Bucket | Capital |
|---|---|
| INDIAN | ₹100,000 |
| US | ₹101,232 |
| CRYPTO | ₹99,964 |
| INTRADAY | ₹79,369 |
| FADE | ₹98,777 |
| US_FADE | ₹100,000 |
| **Total** | **₹579,342** |

- **Total P&L:** ₹-20,658
- **Open:** 22 | **Closed:** 68
- **Wins:** 32 | **Losses:** 35

## 2. Paper Trades

| TimeFrame | Total | Open |
|---|---|---|
| SWING_1d | 33 | 13 |
| INTRADAY_1h | 33 | 0 |
| FADE_1h | 10 | 9 |
| GAP_DOWN_1m | 14 | 0 |
| US_FADE_5m | 0 | 0 |

## 3. FADE — Naya (v5.18-5.19, backtest-spec match)

Sabse naya: **6 entries 12:18 IST** (naye fixed code se — entry = next bar open).

### FADE trades:

| Time | Ticker | Entry | SL | TGT | Status |
|---|---|---|---|---|---|
| 08-13 11:37 | MANAKSTEEL | 86.84 | 87.92 | 84.27 | 🟢 OPEN |
| 08-13 11:37 | GARUDA | 188.07 | 189.48 | 184.21 | 🟢 OPEN |
| 08-13 11:37 | LT | 4052.97 | 4083.38 | 3969.84 | 🟢 OPEN |
| 08-13 11:37 | PRECWIRE | 459.62 | 463.07 | 450.19 | 🔴 CLOSED |
| 08-13 12:18 | AVROIND | 9.4 | 9.49 | 9.12 | 🟢 OPEN |
| 08-13 12:18 | PVP | 35.96 | 36.41 | 34.9 | 🟢 OPEN |
| 08-13 12:18 | SALSTEEL | 68.82 | 69.33 | 67.4 | 🟢 OPEN |
| 08-13 12:18 | BDL | 1372.21 | 1382.51 | 1344.07 | 🟢 OPEN |
| 08-13 12:18 | RPEL | 1363.32 | 1373.55 | 1335.36 | 🟢 OPEN |
| 08-13 12:18 | KERNEX | 2350.52 | 2386.98 | 2281.15 | 🟢 OPEN |

### Closed FADE example:

- **PRECWIRE.NS**: entry 459.62 → exit 463.3, P&L ₹-1,223.36 (#933ID Fade G24: 15m +4.0%/60m vol1.5x RSI65 dayhigh | SL Hit (intraday))

## 4. Strategies (spec compliance — verified)

- **S1–S10** (NSE 15m/5m fade): 10/10 params backtest spec se exact match ✅
- **US U1–U5** (5m fade): 5/5 match, VWAP-below filter ON ✅
- **H1–H5** (1h) + **G1–G25**: factor strings vs values consistent ✅
- **40 NSE + 5 US = 45 fade variants** in Excel (Never Fired sheet) ✅

## 5. Alerts (Telegram)

| Event | Status |
|---|---|
| Trade entry | ✅ Gated summary me (fade 5-min runs pe turant) |
| Trade exit | ✅ live_pnl turant alag msg |
| Market open/close | ✅ NEW — transition alert (state-file based, sirf real change pe) |
| Spam guard | ✅ fade/gapdown 5-min runs sirf events pe msg |

## 6. Files / CI

- **strategy_report.xlsx**: 326 strategies, 45 FADE rows ✅ (fresh)
- **portfolio_report.html**: fresh, 6 buckets ✅
- **paper_trades.csv**: 84+ trades, naye FADE entries committed ✅
- **CI (test.yml)**: last 3 pushes all ✅ success
- **fade_scan.yml**: active on remote, manual run success (198 stocks, 6 entries, 95.9s)

## 7. Known issues / notes

1. **Yahoo rate-limit** (HTTP 401 Invalid Crumb): occasional — market_data fallback handles it, kuch tickers skip ho sakte hain. 195/198 aaye.
2. **PRECWIRE fade SL hit** (entry 459.62, exit 463.30, -₹1223): first closed fade trade — expected risk (SL 1% ka part).
3. **GitHub scheduled cron delay**: manual dispatch se kaam sahi chal raha hai; scheduled runs kabhi-kabhi delay hote hain (platform issue).
4. **US_FADE**: abhi 0 trades — US market raat me kholta hai, wahan signals aayenge.

---
*FREE 3-Market v5.19 • Paper Trade Health Check*
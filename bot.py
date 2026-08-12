"""
FREE 3-Market v5.10 — PROFESSIONAL PAPER TRADING BOT
====================================================
Daily scan: loads 81 strategies, downloads yfinance data,
computes indicators (adjust=False), checks patterns,
enters best trades (1 per ticker per day), sends Telegram,
logs everything.

Author: Finance Manager
Run: python bot.py
"""

import os, sys, json, time, traceback
from datetime import datetime
import pandas as pd
import requests
import pytz

from config import (
    CAPITAL, CAPITAL_BY_MARKET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    YF_PERIOD, YF_INTERVAL, get_region, get_market_status,
    INTRADAY_PERIOD, INTRADAY_INTERVAL, INTRADAY_CAPITAL,
    GAP_DOWN_MAX_SIGNALS_PER_RUN, GAP_DOWN_RANK_A, GAP_DOWN_RANK_B,
    FADE_ALLOW_SHORT, FADE_SL_PCT, FADE_TP_PCT, FADE_MAX_HOLD_HOURS,
    FADE_RANK, FADE_MAX_TRADES_PER_DAY, FADE_CAPITAL, FADE_VARIANTS,
)
from scanner import load_strategies, unique_tickers, compute_indicators, scan_strategies, get_best_entries
from scanner_intraday import (
    load_intraday_strategies, unique_tickers as intraday_ut,
    compute_indicators_1h, scan_intraday_strategies, get_best_intraday_entries,
)
from scanner_gap_down import (
    scan_all_gap_down, get_current_ohlc,
)
from scanner_fade import (
    scan_fade,
)
from paper_trader import (
    enter_trade, update_trades, load_portfolio, round_price,
    generate_portfolio_report, get_strategy_stats,
    initialize_system, check_entry_allowed, PAPER_FILE,
)
from logger import log_scan, log_trade_run, log_portfolio, log_error, now_ist
from strategy_report import generate_strategy_report
import market_data


def _ohlc_bars(df):
    """Normalize a yfinance DataFrame to [(utc_ts, high, low, close), ...].

    Handles MultiIndex columns (auto_adjust=False) and preserves tz-aware
    UTC bar timestamps so paper_trader can do post-entry bar filtering.
    """
    out = []
    try:
        for ts, row in df.iterrows():
            def _v(col):
                v = row[col]
                if hasattr(v, "iloc"):
                    v = v.iloc[0]
                try:
                    return float(v)
                except Exception:
                    return None
            hi, lo, cl = _v("High"), _v("Low"), _v("Close")
            if hi is None or lo is None or cl is None:
                continue
            if df.index.tz is None:
                t = pd.Timestamp(ts).tz_localize("UTC")
            else:
                t = pd.Timestamp(ts).tz_convert("UTC")
            out.append((t, hi, lo, cl))
    except Exception as e:
        print(f"[Bot] _ohlc_bars failed: {e}")
    return out


# Portfolio report file
PORTFOLIO_REPORT_FILE = "logs/portfolio_report.html"


def send_telegram(msg: str) -> str:
    """Send Telegram message with Markdown formatting."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return "NoToken"
    
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"}
    
    for attempt in range(3):
        try:
            r = requests.post(api_url, data=data, timeout=15)
            resp = r.json() if r.text else {}
            if r.status_code == 200 and resp.get("ok"):
                print(f"[TG] Sent OK ({len(msg)} chars)")
                return "Sent"
            else:
                err = resp.get("description", r.text[:200])
                print(f"[TG] Attempt {attempt+1} failed: {err}")
                time.sleep(2)
        except Exception as e:
            print(f"[TG] Attempt {attempt+1} exception: {e}")
            time.sleep(2)
    
    print("[TG] All 3 attempts failed")
    return "Failed"


def send_telegram_document(file_path: str, caption: str = "") -> str:
    """
    Send an HTML file as a Telegram document attachment.
    The user can download and open it in their browser for proper rendering.
    """
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return "NoToken"
    if not os.path.exists(file_path):
        print(f"[TG] File not found: {file_path}")
        return "NoFile"
    
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument"
    
    for attempt in range(3):
        try:
            with open(file_path, "rb") as f:
                files = {"document": (os.path.basename(file_path), f, "text/html")}
                data = {"chat_id": TELEGRAM_CHAT_ID, "caption": caption, "parse_mode": "Markdown"}
                r = requests.post(api_url, data=data, files=files, timeout=30)
            resp = r.json() if r.text else {}
            if r.status_code == 200 and resp.get("ok"):
                file_size = os.path.getsize(file_path)
                print(f"[TG] Document sent: {os.path.basename(file_path)} ({file_size:,} bytes)")
                return "Sent"
            else:
                err = resp.get("description", r.text[:200])
                print(f"[TG] Document attempt {attempt+1} failed: {err}")
                time.sleep(2)
        except Exception as e:
            print(f"[TG] Document attempt {attempt+1} exception: {e}")
            time.sleep(2)
    
    print("[TG] All 3 document attempts failed")
    return "Failed"




def _intraday_market_split():
    """Per-market split of the shared INTRADAY capital bucket for Telegram.

    The engine tracks ONE 'INTRADAY' bucket (both INTRADAY_1h and GAP_DOWN_1m
    draw from it regardless of market). For display we split it into
    Indian / US / Crypto using:
        market_capital = INTRADAY_CAPITAL/3 + realized_PnL(market)

    The initial capital is allocated equally across the 3 markets (display
    assumption) and each market's realized intraday P&L is added on top,
    so the three values always sum EXACTLY to the real INTRADAY bucket.
    """
    result = {"INDIAN": {"capital": 0.0, "pnl": 0.0, "trades": 0},
              "US": {"capital": 0.0, "pnl": 0.0, "trades": 0},
              "CRYPTO": {"capital": 0.0, "pnl": 0.0, "trades": 0}}
    try:
        df = pd.read_csv("logs/paper_trades.csv", on_bad_lines="warn")
        idf = df[df["TimeFrame"].astype(str).isin(["INTRADAY_1h", "GAP_DOWN_1m"])]
        idf = idf[idf["Status"].astype(str).str.upper() == "CLOSED"]
        for _, r in idf.iterrows():
            mode = str(r.get("Mode", "US")).upper()
            if mode == "INDIA":
                mode = "INDIAN"
            if mode not in result:
                continue
            try:
                pnl = float(r.get("P&L"))
            except (TypeError, ValueError):
                continue
            result[mode]["pnl"] += pnl
            result[mode]["trades"] += 1
    except Exception as e:
        print(f"[TG] _intraday_market_split error: {e}")
    base = INTRADAY_CAPITAL / 3.0 if INTRADAY_CAPITAL > 0 else 0
    for mode in result:
        result[mode]["capital"] = base + result[mode]["pnl"]
    return result


def _fade_stats():
    """FADE bucket stats for Telegram: capital, pnl, trades (own ₹1L bucket)."""
    result = {"capital": FADE_CAPITAL, "pnl": 0.0, "trades": 0}
    try:
        df = pd.read_csv("logs/paper_trades.csv", on_bad_lines="warn")
        fd = df[df["TimeFrame"].astype(str) == "FADE_1h"]
        fd = fd[fd["Status"].astype(str).str.upper() == "CLOSED"]
        for _, r in fd.iterrows():
            try:
                pnl = float(r.get("P&L"))
            except (TypeError, ValueError):
                continue
            result["pnl"] += pnl
            result["trades"] += 1
    except Exception as e:
        print(f"[TG] _fade_stats error: {e}")
    result["capital"] = FADE_CAPITAL + result["pnl"]
    return result


# ── 14-day country/market summary helpers (v5.16) ─────────────────────
_SECTION_TREE = {"INDI": "├", "ID-IND": "├", "FADE": "└",
                 "USA": "├", "ID-US": "└",
                 "CRYP": "├", "ID-₿": "└"}


def _section_of(tf, mode):
    """Map (TimeFrame, Mode) -> summary section key."""
    tf = str(tf).upper()
    mode = str(mode).upper()
    if mode == "INDIA":
        mode = "INDIAN"
    if tf == "FADE_1H":
        return "FADE"
    if tf == "SWING_1D":
        return {"INDIAN": "INDI", "US": "USA", "CRYPTO": "CRYP"}.get(mode, mode)
    if tf in ("INTRADAY_1H", "GAP_DOWN_1M"):
        return {"INDIAN": "ID-IND", "US": "ID-US", "CRYPTO": "ID-₿"}.get(mode, "ID-" + mode)
    return tf


def _section_stats(days: int = 14) -> dict:
    """Per-section {T, W, L, pnl, ret_pct} over the last `days` calendar days
    (incl. today) from logs/paper_trades.csv. ret_pct = pnl / 1,00,000 (the
    display assumption used in the Telegram summary)."""
    sections = {k: {"T": 0, "W": 0, "L": 0, "pnl": 0.0} for k in
                ("INDI", "ID-IND", "FADE", "USA", "ID-US", "CRYP", "ID-₿")}
    try:
        df = pd.read_csv("logs/paper_trades.csv", on_bad_lines="warn")
        if df is None or len(df) == 0:
            return sections
        df = df.copy()
        df["Date"] = pd.to_datetime(df["Date"], errors="coerce")
        cutoff = df["Date"].max() - pd.Timedelta(days=days - 1)
        df = df[df["Date"] >= cutoff]
        for _, r in df.iterrows():
            key = _section_of(r.get("TimeFrame"), r.get("Mode"))
            if key not in sections:
                continue
            sections[key]["T"] += 1
            try:
                pnl = float(r.get("P&L"))
                if pnl != pnl:  # NaN guard (CSV can contain empty P&L)
                    pnl = 0.0
            except (TypeError, ValueError):
                pnl = 0.0
            sections[key]["pnl"] += pnl
            if str(r.get("Status", "")).upper() == "CLOSED":
                if pnl > 0:
                    sections[key]["W"] += 1
                elif pnl < 0:
                    sections[key]["L"] += 1
    except Exception as e:
        print(f"[TG] _section_stats error: {e}")
    for k in sections:
        sections[k]["ret_pct"] = sections[k]["pnl"] / 100000.0 * 100.0
    return sections


def _strategy_counts() -> dict:
    """How many strategies are FED (configured) per summary section."""
    counts = {"INDI": 0, "ID-IND": 0, "FADE": 0,
              "USA": 0, "ID-US": 0, "CRYP": 0, "ID-₿": 0}
    try:
        sw = pd.read_csv("data/strategies.csv", on_bad_lines="warn")
        for _, r in sw.iterrows():
            region = str(r.get("Region", "")).strip().lower()
            if region == "india":
                counts["INDI"] += 1
            elif region == "us":
                counts["USA"] += 1
            elif region == "crypto":
                counts["CRYP"] += 1
    except Exception:
        pass
    try:
        it = pd.read_csv("data/intraday_strategies.csv", on_bad_lines="warn")
        for _, r in it.iterrows():
            region = str(r.get("Region", "")).strip().upper()
            if region == "INDIAN":
                counts["ID-IND"] += 1
            elif region == "US":
                counts["ID-US"] += 1
            elif region == "CRYPTO":
                counts["ID-₿"] += 1
    except Exception:
        pass
    counts["FADE"] = len(FADE_VARIANTS)
    return counts


def _country_summary_lines() -> list:
    """Country-wise 14-day summary lines (matches user's requested format)
    with per-section strategy counts appended."""
    stats = _section_stats()
    strat = _strategy_counts()
    lines = []
    for country, sections, flag in (
            ("INDIA", ["INDI", "ID-IND", "FADE"], "🇮🇳"),
            ("USA", ["USA", "ID-US"], "🇺🇸"),
            ("CRYPTO", ["CRYP", "ID-₿"], "₿")):
        lines.append(f"{flag} {country} (14 Days):")
        cT = cW = cL = 0
        cpnl = 0.0
        for i, key in enumerate(sections):
            s = stats[key]
            cT += s["T"]; cW += s["W"]; cL += s["L"]; cpnl += s["pnl"]
            tree = _SECTION_TREE.get(key, "├")
            ret = f"{s['ret_pct']:+.1f}%"
            line = (f"{tree} {key} [{strat[key]}S]: {s['T']}T | "
                    f"{s['W']}W/{s['L']}L | {ret}")
            if s["T"] > 0:
                sign = "+" if s["pnl"] >= 0 else "-"
                line += f" | {sign}₹{abs(s['pnl']):,.0f}"
            lines.append(line)
        c_ret = cpnl / 200000.0 * 100.0
        lines.append(f"=> Total: {cT}T | {cW}W/{cL}L | {c_ret:+.1f}%")
    return lines


def build_telegram_msg(date_str: str, time_str: str, entries: list,
                       closed_msgs: list, cape: float, open_count: int,
                       total_pnl: float, wins: int = 0, losses: int = 0,
                       closed_count: int = 0,
                       capital_by_market: dict = None,
                       open_positions: list = None,
                       market_status: dict = None,
                       strategy_stats: dict = None,
                       scan_summary: dict = None,
                       current_prices: dict = None) -> list:
    """
    Professional Telegram message(s) with everything embedded.
    Returns a LIST of message strings (pages) for pagination.
    Shows newest trades first.
    """
    MAX_CLOSED_PER_PAGE = 5   # Max closed trades per page
    MAX_OPEN_PER_PAGE = 3      # Max open positions per page
    TG_CHAR_LIMIT = 4000       # Telegram max chars per message
    ret_pct = ((cape - CAPITAL) / CAPITAL) * 100 if CAPITAL > 0 else 0
    # Win-rate denominator = ALL closed trades (incl. breakeven), matching
    # strategy_report.xlsx (wins / len(closed)). Fallback to wins+losses
    # only when closed_count is not supplied.
    total_closed = closed_count if closed_count and closed_count > 0 else wins + losses
    win_rate = round(wins / total_closed * 100) if total_closed > 0 else 0
    
    # Return arrow & color indicator
    if ret_pct > 0:
        arrow = "🟢"
        ret_sign = "+"
    elif ret_pct < 0:
        arrow = "🔴"
        ret_sign = ""
    else:
        arrow = "⚪"
        ret_sign = ""
    
    lines = []
    
    # ===== HEADER =====
    short_time = time_str.split(":")[0] + ":" + time_str.split(":")[1]
    lines.append(f"🤖 *PAPER TRADE v5.12* | 🇮🇳🇺🇸₿ {date_str} {short_time}")
    
    # ===== MARKET STATUS =====
    if market_status:
        lines.append("")
        lines.append(market_status.get("message", ""))
    
    # ===== SCAN SUMMARY (always shown) =====
    if scan_summary:
        scanned = scan_summary.get("tickers_ok", 0)
        total_t = scan_summary.get("tickers_total", 0)
        strats_total = scan_summary.get("strategies_total", 0)
        strats_fired = scan_summary.get("strategies_fired", 0)
        errors = scan_summary.get("errors", 0)
        summary_parts = []
        if total_t > 0:
            ok_icon = "✅" if errors == 0 else "⚠️"
            summary_parts.append(f"📡 {ok_icon} Data {scanned}/{total_t} ok")
        if strats_total > 0:
            if strats_fired > 0:
                summary_parts.append(f"🎯 {strats_fired}/{strats_total} fired")
            else:
                summary_parts.append(f"💤 0/{strats_total} strategies")
        if errors > 0:
            summary_parts.append(f"🔴 {errors} errors")
        if summary_parts:
            lines.append("  " + " | ".join(summary_parts))
    
    # ===== NEW TRADE ENTRIES =====
    if entries:
        lines.append("")
        lines.append("━━━ *NEW TRADES* ━━━")
        for t in entries:
            action = "🟢 BUY" if t["direction"] == "LONG" else "🔴 SELL SHORT"
            rank_str = f" #{t.get('rank','')}" if t.get('rank') else ""
            tf_tag = t.get("tf", "") or ""
            tf_badge = {"FADE_1h": "⚡FD", "INTRADAY_1h": "⚡ID",
                        "GAP_DOWN_1m": "📉GD", "SWING_1d": "🌙SW"}.get(tf_tag, "")
            tf_txt = f" {tf_badge}" if tf_badge else ""
            lines.append(
                f"{action} `{t['ticker']}`{tf_txt}{rank_str}\n"
                f"┣ Price: {round_price(t['close'])} | Qty: {t['qty']}\n"
                f"┣ SL: {t['sl']} | TGT: {t['target']}"
            )
        lines.append("")    
    # ===== CLOSED TRADES (Daily Trade Summary) — Latest First =====
    if closed_msgs:
        lines.append("━━━ *CLOSED TODAY* ━━━")
        # Reverse to show newest first
        reversed_closed = list(reversed(closed_msgs))
        total_closed_count = len(reversed_closed)
        shown_closed = reversed_closed[:MAX_CLOSED_PER_PAGE]
        for c in shown_closed:
            lines.append(f"✅ {c}")
        if total_closed_count > MAX_CLOSED_PER_PAGE:
            lines.append(f"*...and {total_closed_count - MAX_CLOSED_PER_PAGE} more*")
        lines.append("")
    
    # ===== OPEN POSITIONS (count only — holdings detail omitted from TG) =====
    if open_positions and len(open_positions) > 0:
        lines.append(f"━━━ *OPEN POSITIONS* ━━━ {len(open_positions)} total")
        lines.append("")
    
    # ===== PER-STRATEGY WIN RATE =====
    if strategy_stats:
        top = strategy_stats.get("top", [])
        bottom = strategy_stats.get("bottom", [])
        if top:
            lines.append("━━━ *TOP STRATEGIES* ━━━")
            for s in top:
                icon = "🏆" if s["win_rate"] >= 70 else "👍"
                lines.append(
                    f"{icon} #{s['rank']} {s['win_rate']}% "
                    f"({s['wins']}W/{s['losses']}L) "
                    f"₹{s['avg_pnl']:+,.0f}/trd"
                )
            lines.append("")
        if bottom:
            lines.append("━━━ *WORST STRATEGIES* ━━━")
            for s in bottom:
                lines.append(
                    f"👎 #{s['rank']} {s['win_rate']}% "
                    f"({s['wins']}W/{s['losses']}L) "
                    f"₹{s['avg_pnl']:+,.0f}/trd"
                )
            lines.append("")
    
    # ===== CAPITAL SUMMARY (country-wise, v5.16) =====
    pnl_icon = "🟢" if total_pnl > 0 else ("🔴" if total_pnl < 0 else "⚪")
    win_icon = "🏆" if win_rate >= 70 else ("👍" if win_rate >= 50 else "👎")
    lines.append("━━━ *PORTFOLIO* ━━━")
    lines.append(f"{arrow} *Capital:* Rs {cape:,.0f} ({ret_sign}{ret_pct:.2f}%)")
    lines.append("")
    for cl in _country_summary_lines():
        lines.append(cl)
    lines.append("")
    lines.append(f"💰 *TOTAL:* Rs {cape:,.0f} ({ret_sign}{ret_pct:.2f}%) | "
                 f"{pnl_icon} *P&L:* Rs {total_pnl:+,.0f} | {win_icon} *Win:* "
                 f"{wins}/{total_closed} ({win_rate}%)")
    lines.append(f"📊 *Open:* {open_count} | *Closed:* {closed_count} | "
                 f"*Total:* {closed_count + open_count}")
    
    # Build full message
    full_msg = "\n".join(lines)
    
    # Split into pages if too long (by section boundaries)
    sections = full_msg.split("\n━━━ *")
    
    pages = []
    current_page = sections[0] if sections else ""
    
    for i, section in enumerate(sections[1:], 1):
        section_with_header = "\n━━━ *" + section
        if len(current_page) + len(section_with_header) > TG_CHAR_LIMIT:
            pages.append(current_page.strip())
            current_page = section_with_header
        else:
            current_page += section_with_header
    
    if current_page.strip():
        pages.append(current_page.strip())
    
    # If still over limit (a single section is too long), truncate last page
    pages = [p[:TG_CHAR_LIMIT] if len(p) > TG_CHAR_LIMIT else p for p in pages]
    
    # Add page indicator (Page 1/3) to each page except if only 1 page
    if len(pages) > 1:
        pages[0] = pages[0] + f"\n\n—— *Page 1/{len(pages)}* ——"
        for i in range(1, len(pages)):
            pages[i] = pages[i] + f"\n\n—— *Page {i+1}/{len(pages)}* ——"
    
    return pages

def _save_swing_scan_date(date_str: str):
    """Save the date of the last completed swing scan."""
    try:
        os.makedirs("logs", exist_ok=True)
        with open("logs/_last_swing_date.txt", "w") as f:
            f.write(date_str)
    except Exception as e:
        print(f"[Swing] Could not save scan date: {e}")


def _load_swing_scan_date() -> str:
    """Load the date of the last completed swing scan."""
    try:
        if os.path.exists("logs/_last_swing_date.txt"):
            with open("logs/_last_swing_date.txt") as f:
                return f.read().strip()
    except Exception:
        pass
    return None


def run_swing_scan() -> dict:
    """Run the SWING (daily) scan — 81 strategies, daily data.
    
    Skips the full scan if no new daily data since the last run
    (prevents redundant scans when market is closed).
    """
    start_time = time.time()
    now = now_ist()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    
    print(f"\n{'='*60}")
    print(f"  SWING SCAN v5.10 — Daily Data")
    print(f"  {date_str} {time_str}")
    print(f"{'='*60}")
    
    # 1. Load strategies
    strategies = load_strategies()
    
    # 2. Get unique tickers
    tickers = unique_tickers(strategies)
    print(f"[Swing] {len(tickers)} unique tickers")
    
    # 3. Download data
    ticker_data = {}
    scan_errors = 0
    for yf_ticker in tickers:
        for attempt in range(3):
            try:
                print(f"[Swing] Downloading {yf_ticker}...", end=" ")
                df = market_data.download(yf_ticker, interval=YF_INTERVAL,
                                          period=YF_PERIOD)
                if df is None or len(df) < 60:
                    print(f"INSUFFICIENT ({len(df) if df is not None else 0} rows)")
                    if attempt < 2: time.sleep(1); continue
                    break
                df = compute_indicators(df)
                ticker_data[yf_ticker] = df
                print(f"OK ({len(df)} rows)")
                break
            except Exception as e:
                print(f"ERROR: {e}")
                if attempt < 2: time.sleep(2); continue
                scan_errors += 1
                log_error(f"Swing download failed {yf_ticker}: {e}")
    
    print(f"[Swing] Data: {len(ticker_data)}/{len(tickers)} tickers, {scan_errors} errors")
    
    # 4. Check if we already ran a full SWING scan today
    #    SWING needs fresh data once per day (after market closes).
    #    Intraday is handled by the separate intraday scanner.
    last_scan_date = _load_swing_scan_date()
    today_date = now.strftime("%Y-%m-%d")
    
    if last_scan_date == today_date:
        print(f"[Swing] Already scanned today ({today_date}), "
              f"skipping full scan — will still check exits")
        # ── Still check exits using downloaded data ──
        empty_result = {
            "mode": "SWING",
            "ticker_data": ticker_data,
            "market_status": {},
            "current_prices": {},
            "ohlc_data": {},
            "all_signals": [],
            "fired_signals": [],
            "best_entries": [],
            "entries": [],
            "closed_msgs": [],
            "scan_errors": 0,
            "duration": time.time() - start_time,
        }
        current_prices_sw = {}
        ohlc_data_sw = {}
        for yf_ticker, df in ticker_data.items():
            if df is not None and len(df) > 0:
                last = df.iloc[-1]
                try:
                    cv = float(last["Close"].iloc[0] if hasattr(last["Close"], 'iloc') else last["Close"])
                    hv = float(last["High"].iloc[0] if hasattr(last["High"], 'iloc') else last["High"])
                    lv = float(last["Low"].iloc[0] if hasattr(last["Low"], 'iloc') else last["Low"])
                    current_prices_sw[yf_ticker] = cv
                    ohlc_data_sw[yf_ticker] = {"close": cv, "high": hv, "low": lv,
                                               "date": str(df.index[-1].date()),
                                               "bars": _ohlc_bars(df)}
                except:
                    pass
        
        closed_msgs = update_trades(ohlc_data_sw)
        empty_result["closed_msgs"] = closed_msgs
        empty_result["current_prices"] = current_prices_sw
        print(f"[Swing] Exit check: {len(closed_msgs)} closed trades | {len(current_prices_sw)} prices")
        return empty_result
    
    # 4. Market status
    market_status = {}
    for yf_ticker, df in ticker_data.items():
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            cv = float(last["Close"].iloc[0] if hasattr(last["Close"], 'iloc') else last["Close"])
            hv = float(last["High"].iloc[0] if hasattr(last["High"], 'iloc') else last["High"])
            lv = float(last["Low"].iloc[0] if hasattr(last["Low"], 'iloc') else last["Low"])
            market_status[yf_ticker] = {
                "data_ok": True, "latest_close": cv, "latest_high": hv, "latest_low": lv,
                "latest_date": str(df.index[-1].date()), "region": get_region(yf_ticker),
            }
        else:
            market_status[yf_ticker] = {"data_ok": False}
    
    # 5. Scan
    all_signals = scan_strategies(strategies, ticker_data)
    fired_signals = [s for s in all_signals if s.get("fired")]
    print(f"[Swing] Strategies: {len(all_signals)}, fired: {len(fired_signals)}")
    
    # 6. Best entries
    best_entries = get_best_entries(all_signals)
    print(f"[Swing] Best entries: {len(best_entries)}")
    
    # 7. Update positions
    current_prices = {}
    ohlc_data = {}
    for yf_ticker, st in market_status.items():
        if st.get("data_ok"):
            current_prices[yf_ticker] = st["latest_close"]
            ohlc_data[yf_ticker] = {"close": st["latest_close"], "high": st["latest_high"],
                                    "low": st["latest_low"], "date": st.get("latest_date", "")}
        # Swing ohlc_data carries full daily bars for post-entry bar-level SL/TP
        for yf_ticker, df in ticker_data.items():
            if yf_ticker in ohlc_data and df is not None and len(df) > 0:
                ohlc_data[yf_ticker]["bars"] = _ohlc_bars(df)
    
    closed_msgs = update_trades(ohlc_data)
    print(f"[Swing] Closed: {len(closed_msgs)}")
    
    # 8. Enter new trades (SWING)
    entries = []
    skipped_entries = []
    for entry in best_entries:
        region = get_region(entry["ticker"], entry.get("region"))
        trade = enter_trade(
            mode=region, ticker=entry["ticker"], direction=entry["direction"],
            entry_price=entry["close"], reason=entry.get("factors", "")[:60],
            pattern_rank=entry.get("rank"), expected_win_rate=entry.get("win_rate"),
            pattern_factors=entry.get("factors", ""), tf="SWING_1d",
            signal_indicators=entry.get("signal_indicators"),
        )
        if trade:
            entries.append({"ticker": entry["ticker"], "direction": entry["direction"],
                "close": entry["close"], "qty": trade["Qty"], "sl": trade["SL"],
                "target": trade["Target"], "rank": entry.get("rank"), "win_rate": entry.get("win_rate"),
                "tf": "SWING_1d"})
        else:
            skipped_entries.append({
                "ticker": entry["ticker"], "direction": entry["direction"],
                "close": entry["close"], "rank": entry.get("rank"),
                "win_rate": entry.get("win_rate"),
                "reason": check_entry_allowed(entry["ticker"], entry["direction"],
                                              pattern_rank=entry.get("rank"))
                          or "Rejected (position sizing / unknown)",
            })
    print(f"[Swing] New entries: {len(entries)}")
    
    # Save today's date ONLY AFTER full scan completes successfully
    _save_swing_scan_date(today_date)
    
    return {
        "mode": "SWING",
        "ticker_data": ticker_data,
        "market_status": market_status,
        "current_prices": current_prices,
        "ohlc_data": ohlc_data,
        "all_signals": all_signals,
        "fired_signals": fired_signals,
        "best_entries": best_entries,
        "entries": entries,
        "skipped_entries": skipped_entries,
        "closed_msgs": closed_msgs,
        "scan_errors": scan_errors,
        "duration": time.time() - start_time,
    }


def run_intraday_scan() -> dict:
    """Run the INTRADAY (1h) scan — 40 strategies, 1h data."""
    start_time = time.time()
    now = now_ist()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    
    print(f"\n{'='*60}")
    print(f"  INTRADAY SCAN v5.10 — 1h Data")
    print(f"  {date_str} {time_str}")
    print(f"{'='*60}")
    
    # 1. Load strategies
    strategies = load_intraday_strategies()
    
    # 2. Get unique tickers
    tickers = intraday_ut(strategies)
    print(f"[Intraday] {len(tickers)} unique tickers")
    
    # 3. Download 1h data
    ticker_data = {}
    scan_errors = 0
    for yf_ticker in tickers:
        for attempt in range(3):
            try:
                print(f"[Intraday] Downloading {yf_ticker}...", end=" ")
                df = market_data.download(yf_ticker, interval=INTRADAY_INTERVAL,
                                          period=INTRADAY_PERIOD)
                if df is None or len(df) < 200:
                    print(f"INSUFFICIENT ({len(df) if df is not None else 0} rows)")
                    if attempt < 2: time.sleep(1); continue
                    break
                df = compute_indicators_1h(df)
                ticker_data[yf_ticker] = df
                print(f"OK ({len(df)} rows, {df.index[-1].date()})")
                break
            except Exception as e:
                print(f"ERROR: {e}")
                if attempt < 2: time.sleep(2); continue
                scan_errors += 1
                log_error(f"Intraday download failed {yf_ticker}: {e}")
    
    print(f"[Intraday] Data: {len(ticker_data)}/{len(tickers)} tickers, {scan_errors} errors")
    
    # 4. Market status
    market_status = {}
    for yf_ticker, df in ticker_data.items():
        if df is not None and len(df) > 0:
            last = df.iloc[-1]
            cv = float(last["Close"].iloc[0] if hasattr(last["Close"], 'iloc') else last["Close"])
            hv = float(last["High"].iloc[0] if hasattr(last["High"], 'iloc') else last["High"])
            lv = float(last["Low"].iloc[0] if hasattr(last["Low"], 'iloc') else last["Low"])
            # Actual daily range from today's 1h candles (accurate SL/TP detection)
            same_date_mask = df.index.date == df.index[-1].date()
            same_date_df = df[same_date_mask]
            daily_h = float(same_date_df["High"].max()) if len(same_date_df) > 0 else hv
            daily_l = float(same_date_df["Low"].min()) if len(same_date_df) > 0 else lv
            market_status[yf_ticker] = {
                "data_ok": True, "latest_close": cv, "latest_high": hv, "latest_low": lv,
                "daily_high": daily_h, "daily_low": daily_l,
                "latest_date": str(df.index[-1].date()), "region": get_region(yf_ticker),
            }
        else:
            market_status[yf_ticker] = {"data_ok": False}
    
    # 5. Scan
    all_signals = scan_intraday_strategies(strategies, ticker_data)
    fired_signals = [s for s in all_signals if s.get("fired")]
    print(f"[Intraday] Strategies: {len(all_signals)}, fired: {len(fired_signals)}")
    
    # 6. Best entries
    best_entries = get_best_intraday_entries(all_signals)
    print(f"[Intraday] Best entries: {len(best_entries)}")
    
    # 7. Update positions (same OHLC exit logic — uses daily range for SL/TP)
    current_prices = {}
    ohlc_data = {}
    for yf_ticker, st in market_status.items():
        if st.get("data_ok"):
            current_prices[yf_ticker] = st["latest_close"]
            # Use daily range (NOT latest candle) for accurate SL/TP detection
            ohlc_data[yf_ticker] = {
                "close": st["latest_close"],
                "high": st.get("daily_high", st["latest_high"]),
                "low": st.get("daily_low", st["latest_low"]),
                "date": st.get("latest_date", ""),
            }
        # Intraday ohlc_data carries full 1h bars for post-entry bar-level SL/TP
        for yf_ticker, df in ticker_data.items():
            if yf_ticker in ohlc_data and df is not None and len(df) > 0:
                ohlc_data[yf_ticker]["bars"] = _ohlc_bars(df)
    
    closed_msgs = update_trades(ohlc_data)
    print(f"[Intraday] Closed: {len(closed_msgs)}")
    
    # 8. Enter new trades (INTRADAY)
    entries = []
    skipped_entries = []
    for entry in best_entries:
        region = get_region(entry["ticker"], entry.get("region"))
        trade = enter_trade(
            mode=region, ticker=entry["ticker"], direction=entry["direction"],
            entry_price=entry["close"], reason=entry.get("factors", "")[:60],
            pattern_rank=entry.get("rank"), expected_win_rate=entry.get("win_rate"),
            pattern_factors=entry.get("factors", ""), tf="INTRADAY_1h",
            signal_indicators=entry.get("signal_indicators"),
        )
        if trade:
            entries.append({"ticker": entry["ticker"], "direction": entry["direction"],
                "close": entry["close"], "qty": trade["Qty"], "sl": trade["SL"],
                "target": trade["Target"], "rank": entry.get("rank"), "win_rate": entry.get("win_rate"),
                "tf": "INTRADAY_1h"})
        else:
            skipped_entries.append({
                "ticker": entry["ticker"], "direction": entry["direction"],
                "close": entry["close"], "rank": entry.get("rank"),
                "win_rate": entry.get("win_rate"),
                "reason": check_entry_allowed(entry["ticker"], entry["direction"],
                                              tf="INTRADAY_1h",
                                              pattern_rank=entry.get("rank"))
                          or "Rejected (position sizing / unknown)",
            })
    print(f"[Intraday] New entries: {len(entries)}")
    
    return {
        "mode": "INTRADAY",
        "ticker_data": ticker_data,
        "market_status": market_status,
        "current_prices": current_prices,
        "ohlc_data": ohlc_data,
        "all_signals": all_signals,
        "fired_signals": fired_signals,
        "best_entries": best_entries,
        "entries": entries,
        "skipped_entries": skipped_entries,
        "closed_msgs": closed_msgs,
        "scan_errors": scan_errors,
        "duration": time.time() - start_time,
    }


def run_fade_scan() -> dict:
    """Run the NSE FADE scan — 35 variants, SHORT stocks that just shot up."""
    start_time = time.time()
    now = now_ist()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")

    print(f"\n{'='*60}")
    print(f"  FADE SCAN v5.15 — NSE Big-Player-Exit (35 variants)")
    print(f"  {date_str} {time_str}")
    print(f"{'='*60}")

    res = scan_fade()
    all_signals = res["all_signals"]
    fired = res["fired_signals"]
    print(f"[Fade] Signals: {len(all_signals)}, fired: {len(fired)}")

    # 1. Check exits for open FADE_1h positions (using variant-appropriate bars)
    portfolio = load_portfolio()
    fade_open = [p for p in portfolio.get("open_positions", [])
                 if p.get("TimeFrame") == "FADE_1h"]
    ohlc_data = {}
    current_prices = {}
    for p in fade_open:
        t = p["Ticker"]
        rank = int(p.get("Pattern_Rank") or 0)
        # find the variant's interval for this open position
        interval = "1h"
        for v in FADE_VARIANTS:
            if v["rank"] == rank:
                interval = v["interval"]
                break
        df = res["ticker_data"].get((interval, t))
        if df is not None and len(df):
            last = df.iloc[-1]
            ohlc_data[t] = {
                "close": float(last["Close"]),
                "high": float(last["High"]),
                "low": float(last["Low"]),
                "date": str(df.index[-1].date()),
                "bars": _ohlc_bars(df),
            }
            current_prices[t] = float(last["Close"])
    closed_msgs = update_trades(ohlc_data)
    print(f"[Fade] Closed: {len(closed_msgs)}")

    # 2. Enter new FADE_1h SHORT trades — TRUE PER-VARIANT PER-DAY caps.
    # Backtest caps were per calendar day per strategy; count today's entries
    # per rank (open or already closed) so crons can't stack up beyond cap.
    today_by_rank = {}
    try:
        if os.path.exists(PAPER_FILE):
            pt = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
            if len(pt):
                mask = (pt["TimeFrame"].astype(str) == "FADE_1h") & \
                       (pt["Date"].astype(str) == date_str)
                for _, row in pt[mask].iterrows():
                    rk = row.get("Pattern_Rank")
                    try:
                        rk = int(rk)
                    except (TypeError, ValueError):
                        rk = 0
                    today_by_rank[rk] = today_by_rank.get(rk, 0) + 1
    except Exception as e:
        log_error(f"Fade daily-count check failed: {e}")

    entries = []
    skipped_entries = []
    for s in fired:
        rank = int(s.get("rank") or FADE_RANK)
        variant = next((v for v in FADE_VARIANTS if v["rank"] == rank), None)
        max_day = variant["max_per_day"] if variant else FADE_MAX_TRADES_PER_DAY
        used = today_by_rank.get(rank, 0)
        if used >= max_day:
            skipped_entries.append({"ticker": s["ticker"], "direction": "SHORT",
                                    "close": s["close"], "rank": rank,
                                    "win_rate": s.get("win_rate"),
                                    "reason": f"Daily cap reached ({used}/{max_day})"})
            continue
        if not FADE_ALLOW_SHORT:
            skipped_entries.append({"ticker": s["ticker"], "direction": "SHORT",
                                    "close": s["close"], "reason": "FADE_ALLOW_SHORT=False"})
            continue
        entry_price = s["close"]
        sl_price = entry_price * (1 + s.get("sl_pct", FADE_SL_PCT))
        tp_price = entry_price * (1 - s.get("tp_pct", FADE_TP_PCT))
        trade = enter_trade(
            mode="INDIAN", ticker=s["ticker"], direction="SHORT",
            entry_price=entry_price,
            reason=s.get("factors", variant["factors"] if variant else "Fade")[:60],
            pattern_rank=rank,
            expected_win_rate=s.get("win_rate", 41.61),
            pattern_factors=s.get("factors", ""),
            tf="FADE_1h",
            sl_override=sl_price,
            tp_override=tp_price,
            max_hold_override=5,
            signal_indicators=s.get("signal_indicators"),
        )
        if trade:
            entries.append({"ticker": s["ticker"], "direction": "SHORT",
                            "close": entry_price, "qty": trade["Qty"],
                            "sl": trade["SL"], "target": trade["Target"],
                            "rank": rank, "win_rate": s.get("win_rate"),
                            "tf": "FADE_1h"})
            today_by_rank[rank] = used + 1
            if s["ticker"] not in current_prices:
                current_prices[s["ticker"]] = entry_price
        else:
            skipped_entries.append({
                "ticker": s["ticker"], "direction": "SHORT",
                "close": entry_price, "rank": rank, "win_rate": s.get("win_rate"),
                "reason": check_entry_allowed(s["ticker"], "SHORT", tf="FADE_1h",
                                              pattern_rank=rank)
                          or "Rejected (position sizing / unknown)",
            })
    print(f"[Fade] New entries: {len(entries)}, skipped: {len(skipped_entries)}")

    return {
        "mode": "FADE",
        "ticker_data": res["ticker_data"],
        "market_status": {},
        "current_prices": current_prices,
        "ohlc_data": ohlc_data,
        "all_signals": all_signals,
        "fired_signals": fired,
        "best_entries": fired,
        "entries": entries,
        "skipped_entries": skipped_entries,
        "closed_msgs": closed_msgs,
        "scan_errors": res["scan_errors"],
        "duration": time.time() - start_time,
    }


def run_gap_down_scan() -> dict:
    """Run the GAP-DOWN 1m intraday scan.
    
    Scans all 97 Indian tickers for gap-down mean reversion signals.
    Enters trades using strategy-specific SL/TP and 5-minute holding period.
    Checks exits for all open GAP_DOWN_1m positions.
    """
    start_time = time.time()
    now = now_ist()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    
    print(f"\n{'='*60}")
    print(f"  GAP-DOWN SCAN v5.10 — 1m Intraday")
    print(f"  {date_str} {time_str}")
    print(f"{'='*60}")
    
    # 1. Scan all Indian tickers for gap-down signals
    entries = []
    skipped_entries = []
    scan_errors = 0
    try:
        all_signals = scan_all_gap_down(progress_interval=20)
        print(f"[GapDown] Total signals found: {len(all_signals)}")
        
        # 2. Check exits for open GAP_DOWN_1m positions FIRST
        # Get current OHLC data for all tickers that have open gap-down trades
        portfolio = load_portfolio()
        gap_down_tickers = []
        for pos in portfolio.get("open_positions", []):
            if pos.get("TimeFrame") == "GAP_DOWN_1m":
                gap_down_tickers.append(pos["Ticker"])
        
        ohlc_data = {}
        current_prices = {}
        for tkr in gap_down_tickers:
            ohlc = get_current_ohlc(tkr)
            if ohlc:
                ohlc_data[tkr] = ohlc
                current_prices[tkr] = ohlc["close"]
        
        closed_msgs = update_trades(ohlc_data)
        print(f"[GapDown] Closed: {len(closed_msgs)}")
        
        # ── CRASH DETECTOR ──
        # If N+ stocks gap down simultaneously, it's a market-wide event.
        # In a crash, gaps DON'T fill — every entry would hit SL.
        # Skip ALL entries until the next scan cycle.
        if len(all_signals) >= GAP_DOWN_MAX_SIGNALS_PER_RUN:
            print(f"[GapDown] ⚠️ MARKET-WIDE EVENT: {len(all_signals)} signals > "
                  f"threshold ({GAP_DOWN_MAX_SIGNALS_PER_RUN}). "
                  f"Skipping ALL entries to prevent crash losses.")
            log_error(f"GapDown crash: {len(all_signals)} signals >= {GAP_DOWN_MAX_SIGNALS_PER_RUN}, skipped all entries")
        else:
            # 3. Enter new trades using SL/TP overrides
            # Sort: Strategy A (75% WR) before Strategy B (70% WR) for priority
            # Assign rank IDs for strategy_stats tracking + consecutive loss guard
            all_signals.sort(key=lambda s: (
                0 if s["strategy"] == "gap_down_52wk_low" else 1
            ))
            for s in all_signals:
                rank_id = GAP_DOWN_RANK_A if s["strategy"] == "gap_down_52wk_low" else GAP_DOWN_RANK_B
                trade = enter_trade(
                    mode="INDIAN",
                    ticker=s["ticker"],
                    direction="LONG",
                    entry_price=s["entry_price"],
                    reason=s["strategy"],
                    pattern_rank=rank_id,  # 997 for A, 998 for B — enables stats + loss guard
                    expected_win_rate=75.0 if s["strategy"] == "gap_down_52wk_low" else 70.0,
                    pattern_factors=f"f_gap_down + f_52wk_low" if s["strategy"] == "gap_down_52wk_low" else "f_gap_down",
                    tf="GAP_DOWN_1m",
                    sl_override=s["sl"],
                    tp_override=s["tp"],
                    max_hold_override=s["max_hold_minutes"],
                )
                if trade:
                    entries.append({
                        "ticker": s["ticker"],
                        "direction": "LONG",
                        "close": s["entry_price"],
                        "qty": trade["Qty"],
                        "sl": trade["SL"],
                        "target": trade["Target"],
                        "rank": rank_id,
                        "win_rate": 75.0 if s["strategy"] == "gap_down_52wk_low" else 70.0,
                        "tf": "GAP_DOWN_1m",
                    })
                    # Also add to current_prices for live P&L
                    if s["ticker"] not in current_prices:
                        current_prices[s["ticker"]] = s["entry_price"]
                else:
                    skipped_entries.append({
                        "ticker": s["ticker"], "direction": "LONG",
                        "close": s["entry_price"], "rank": rank_id,
                        "win_rate": 75.0 if s["strategy"] == "gap_down_52wk_low" else 70.0,
                        "reason": check_entry_allowed(s["ticker"], "LONG",
                                                      tf="GAP_DOWN_1m",
                                                      pattern_rank=rank_id)
                                  or "Rejected (position sizing / unknown)",
                    })
    except Exception as e:
        log_error(f"GapDown scan failed: {e}")
        print(f"[FATAL] GapDown scan: {e}")
        traceback.print_exc()
        scan_errors += 1
        closed_msgs = []
        current_prices = {}
        all_signals = []
    
    print(f"[GapDown] New entries: {len(entries)}")
    
    return {
        "mode": "GAPDOWN",
        "ticker_data": {},
        "market_status": {},
        "current_prices": current_prices,
        "ohlc_data": {},
        "all_signals": all_signals,
        "fired_signals": [{"fired": True} for _ in all_signals],
        "best_entries": [],
        "entries": entries,
        "skipped_entries": skipped_entries,
        "closed_msgs": closed_msgs,
        "scan_errors": scan_errors,
        "duration": time.time() - start_time,
    }


def main():
    """Main bot entry point. Supports --mode swing (default), intraday, both, or gapdown."""
    start_time = time.time()
    now = now_ist()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    
    # ── Initialize system files FIRST (before any operation) ──
    initialize_system()
    
    # Parse mode from CLI args
    mode = "swing"
    if len(sys.argv) > 1:
        mode_arg = sys.argv[1].lower().replace("--mode=", "").replace("--", "")
        if mode_arg in ("intraday", "id"):
            mode = "intraday"
        elif mode_arg in ("both", "all"):
            mode = "both"
        elif mode_arg in ("swing", "daily"):
            mode = "swing"
        elif mode_arg in ("gapdown", "gd", "gap"):
            mode = "gapdown"
        elif mode_arg in ("fade", "fd"):
            mode = "fade"
    
    print(f"\n{'='*60}")
    print(f"  FREE 3-Market v5.10 PAPER TRADE BOT")
    print(f"  Mode: {mode.upper()} | {date_str} {time_str}")
    print(f"{'='*60}")
    
    # Run scans based on mode
    scan_results = []
    if mode in ("swing", "both"):
        try:
            sr = run_swing_scan()
            scan_results.append(sr)
        except Exception as e:
            log_error(f"Swing scan failed: {e}")
            print(f"[FATAL] Swing scan: {e}")
            traceback.print_exc()
    
    if mode in ("intraday", "both"):
        try:
            sr = run_intraday_scan()
            scan_results.append(sr)
        except Exception as e:
            log_error(f"Intraday scan failed: {e}")
            print(f"[FATAL] Intraday scan: {e}")
            traceback.print_exc()
    
    if mode in ("fade", "both"):
        # Fade is an India intraday strategy — only scan while India is open
        # (or within a short window after close for the 3:00-3:30 PM candle).
        mkt = get_market_status()
        fade_ok = mkt.get("INDIAN") in ("OPEN", "PRE-OPEN")
        if fade_ok:
            try:
                sr = run_fade_scan()
                scan_results.append(sr)
            except Exception as e:
                log_error(f"Fade scan failed: {e}")
                print(f"[FATAL] Fade scan: {e}")
                traceback.print_exc()
        else:
            print(f"[Fade] Skipped — India market {mkt.get('INDIAN')}")
    
    if mode == "gapdown":
        try:
            sr = run_gap_down_scan()
            scan_results.append(sr)
        except Exception as e:
            log_error(f"GapDown scan failed: {e}")
            print(f"[FATAL] GapDown scan: {e}")
            traceback.print_exc()
    
    if not scan_results:
        print(f"[Bot] No scans completed successfully")
        return
    
    # Merge results from all scans
    all_entries = []
    all_closed = []
    all_signals = []
    all_skipped = []
    total_fired = 0
    total_errors = 0
    total_tickers = 0
    current_prices = {}
    
    for sr in scan_results:
        all_entries.extend(sr["entries"])
        all_closed.extend(sr["closed_msgs"])
        all_signals.extend(sr["all_signals"])
        all_skipped.extend(sr.get("skipped_entries", []))
        total_fired += len(sr["fired_signals"])
        total_errors += sr["scan_errors"]
        total_tickers += len(sr["ticker_data"])
        current_prices.update(sr["current_prices"])
    
    # Load portfolio
    portfolio = load_portfolio()
    cap_by_mkt = portfolio.get("capital_by_market", dict(CAPITAL_BY_MARKET))
    # Add intraday capital
    if "INTRADAY" not in cap_by_mkt:
        cap_by_mkt["INTRADAY"] = INTRADAY_CAPITAL
    # Add FADE bucket (own ₹1L)
    if "FADE" not in cap_by_mkt:
        cap_by_mkt["FADE"] = FADE_CAPITAL
    total_cape = sum(cap_by_mkt.values())
    open_positions = portfolio.get("open_positions", [])
    total_pnl = portfolio.get("total_pnl", 0)
    closed_cnt = portfolio.get("closed_count", 0)
    wins = portfolio.get("total_wins", 0)
    losses = portfolio.get("total_losses", 0)
    
    # Generate report
    generate_portfolio_report(current_prices=current_prices)
    
    # Market status & stats
    mkt_status = get_market_status()
    strat_stats = get_strategy_stats(top_n=5)
    
    # Total signals count across all scans
    total_strategies = sum(len(sr["all_signals"]) for sr in scan_results)
    scan_summary = {
        "tickers_ok": len(current_prices),
        "tickers_total": total_tickers,
        "strategies_total": total_strategies,
        "strategies_fired": total_fired,
        "errors": total_errors,
    }
    
    # Telegram — now returns list of pages (paginated, newest first)
    tg_pages = build_telegram_msg(
        date_str, time_str, all_entries, all_closed,
        total_cape, len(open_positions), total_pnl,
        wins, losses, closed_cnt,
        capital_by_market=cap_by_mkt,
        open_positions=open_positions,
        market_status=mkt_status,
        strategy_stats=strat_stats,
        scan_summary=scan_summary,
        current_prices=current_prices,
    )
    tg_status_list = []
    for page_idx, page_msg in enumerate(tg_pages):
        status = send_telegram(page_msg)
        tg_status_list.append(status)
        if page_idx < len(tg_pages) - 1:
            time.sleep(0.5)  # Small delay between pages
    tg_status = "/".join(tg_status_list) if tg_status_list else "NoPages"
    
    # Portfolio report document
    if os.path.exists(PORTFOLIO_REPORT_FILE):
        doc_caption = f"📊 *Full Portfolio Report* — {date_str}\n"
        doc_caption += "Download & open in browser for beautiful formatted view"
        send_telegram_document(PORTFOLIO_REPORT_FILE, caption=doc_caption)
    
    # Log scan data
    fired_patterns = []
    for sr in scan_results:
        for s in sr["all_signals"]:
            if s.get("fired"):
                fired_patterns.append({
                    "rank": s["rank"], "market": s["market"], "ticker": s["ticker"],
                    "direction": s["direction"], "factors": s["factors"],
                    "win_rate": s["win_rate"], "reason": s.get("reason", "All factors met"),
                    "signal_indicators": s.get("signal_indicators"),
                })
    
    scan_data = {
        "date": date_str, "time": time_str,
        "mode": mode.upper(),
        "tickers_scanned": list(current_prices.keys()),
        "market_close": {t: c for t, c in current_prices.items()},
        "patterns_checked": len(all_signals),
        "patterns_fired": total_fired,
        "fired_patterns": fired_patterns,
        "entries": all_entries,
        "skipped_entries": all_skipped,
        "portfolio": {
            "capital_by_market": cap_by_mkt,
            "total_capital": total_cape,
            "open_count": len(open_positions),
            "total_pnl": total_pnl, "closed_count": closed_cnt,
            "wins": wins, "losses": losses,
        },
        "telegram_status": tg_status,
        "duration_sec": round(time.time() - start_time, 1),
    }
    log_scan(scan_data)
    
    # Trade run summary
    for sr in scan_results:
        log_trade_run({
            "Date": date_str, "Time": time_str,
            "Mode": sr["mode"],
            "Tickers_Scanned": len(sr["ticker_data"]),
            "Errors": sr["scan_errors"],
            "Patterns_Total": len(sr["all_signals"]),
            "Patterns_Fired": len(sr["fired_signals"]),
            "New_Entries": len(sr["entries"]),
            "Closed_Trades": len(sr["closed_msgs"]),
            "Open_Positions": len(open_positions),
            "Capital": round(total_cape, 0),
            "Total_PnL": round(total_pnl, 0),
            "Telegram": tg_status,
        })
    
    # Portfolio snapshot
    log_portfolio(total_cape, open_positions, closed_cnt, wins, losses, total_pnl,
                  capital_by_market=cap_by_mkt)
    
    # Auto-refresh strategy Excel report
    try:
        generate_strategy_report()
    except Exception as e:
        log_error(f"Strategy Excel report failed: {e}")
        print(f"[WARN] Strategy Excel report: {e}")
    
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"  BOT RUN COMPLETE — Mode: {mode.upper()} | {elapsed:.1f}s")
    print(f"  Tickers: {total_tickers} | Fired: {total_fired}")
    print(f"  Entered: {len(all_entries)} | Closed: {len(all_closed)}")
    print(f"  Capital: Rs {total_cape:,.0f} | PnL: Rs {total_pnl:+,.0f}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error(f"Bot crashed: {e}")
        print(f"[FATAL] Bot crashed: {e}")
        traceback.print_exc()

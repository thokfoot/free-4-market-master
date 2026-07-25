"""
FREE 3-Market v5.0 — PROFESSIONAL PAPER TRADING BOT
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
import yfinance as yf
import pandas as pd
import requests
import pytz

from config import (
    CAPITAL, CAPITAL_BY_MARKET, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    YF_PERIOD, YF_INTERVAL, get_region, get_market_status,
)
from scanner import load_strategies, unique_tickers, compute_indicators, scan_strategies, get_best_entries
from paper_trader import (
    enter_trade, update_trades, load_portfolio, round_price,
    generate_portfolio_report, get_strategy_stats,
)
from logger import log_scan, log_trade_run, log_portfolio, log_error, now_ist


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


def build_telegram_msg(date_str: str, time_str: str, entries: list,
                       closed_msgs: list, cape: float, open_count: int,
                       total_pnl: float, wins: int = 0, losses: int = 0,
                       closed_count: int = 0,
                       capital_by_market: dict = None,
                       open_positions: list = None,
                       market_status: dict = None,
                       strategy_stats: dict = None,
                       scan_summary: dict = None) -> str:
    """
    Professional Telegram message with everything embedded:
    market status, daily trades, open positions, per-strategy stats,
    capital summary, and portfolio breakdown.
    """
    ret_pct = ((cape - CAPITAL) / CAPITAL) * 100 if CAPITAL > 0 else 0
    total_closed = wins + losses
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
    lines.append(f"🤖 *PAPER TRADE v5.0* | 🇮🇳🇺🇸₿ {date_str} {short_time}")
    
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
            action = "🟢 BUY" if t["direction"] == "LONG" else "🔴 SELL"
            rank_str = f" #{t.get('rank','')}" if t.get('rank') else ""
            lines.append(
                f"{action} `{t['ticker']}`{rank_str}\n"
                f"┣ Price: {round_price(t['close'])} | Qty: {t['qty']}\n"
                f"┣ SL: {t['sl']} | TGT: {t['target']}"
            )
        lines.append("")
    
    # ===== CLOSED TRADES (Daily Trade Summary) =====
    if closed_msgs:
        lines.append("━━━ *CLOSED TODAY* ━━━")
        for c in closed_msgs:
            lines.append(f"✅ {c}")
        lines.append("")
    
    # ===== OPEN POSITIONS =====
    if open_positions and len(open_positions) > 0:
        lines.append("━━━ *OPEN POSITIONS* ━━━")
        for pos in open_positions:
            p_dir = "🟢" if pos.get("Direction") == "LONG" else "🔴"
            p_mode = pos.get("Mode", "")
            p_mode_icon = {"INDIAN": "🇮🇳", "US": "🇺🇸", "CRYPTO": "₿"}.get(p_mode, "")
            lines.append(
                f"{p_dir} `{pos.get('Ticker','?')}` {pos.get('Direction','')} "
                f"{p_mode_icon}{p_mode}\n"
                f"┣ Entry: {round_price(pos.get('Entry_Price',0))} | Qty: {pos.get('Qty',0)}\n"
                f"┣ SL: {pos.get('SL','?')} | TGT: {pos.get('Target','?')}\n"
                f"┗ Reason: {str(pos.get('Reason',''))[:50]}"
            )
        lines.append("")
    
    # ===== PER-STRATEGY WIN RATE =====
    if strategy_stats:
        top = strategy_stats.get("top", [])
        bottom = strategy_stats.get("bottom", [])
        if top:
            lines.append("━━━ *TOP STRATEGIES* ━━━")
            for s in top:
                icon = "🏆" if s["win_rate"] >= 70 else "👍"
                pnl_sym = "+" if s["total_pnl"] >= 0 else ""
                lines.append(
                    f"{icon} #{s['rank']} {s['win_rate']}% "
                    f"({s['wins']}W/{s['losses']}L) "
                    f"₹{pnl_sym}{s['total_pnl']:,.0f}"
                )
            lines.append("")
        if bottom:
            lines.append("━━━ *WORST STRATEGIES* ━━━")
            for s in bottom:
                lines.append(
                    f"👎 #{s['rank']} {s['win_rate']}% "
                    f"({s['wins']}W/{s['losses']}L) "
                    f"₹{s['total_pnl']:+,.0f}"
                )
            lines.append("")
    
    # ===== CAPITAL SUMMARY =====
    lines.append("━━━ *PORTFOLIO* ━━━")
    lines.append(f"{arrow} *Capital:* Rs {cape:,.0f} ({ret_sign}{ret_pct:.2f}%)")
    
    # Per-market breakdown table
    if capital_by_market:
        mkt_short = {"INDIAN": "🇮🇳IND", "US": "🇺🇸USA", "CRYPTO": "₿CRYP"}
        lines.append("```")
        lines.append("Market   Capital    Return")
        lines.append("-" * 30)
        for mkt in ["INDIAN", "US", "CRYPTO"]:
            mcap = capital_by_market.get(mkt, 100000)
            minit = CAPITAL_BY_MARKET.get(mkt, 100000)
            mret = ((mcap - minit) / minit * 100) if minit > 0 else 0
            label = mkt_short.get(mkt, mkt)
            lines.append(f"{label:8s} ₹{mcap:>8,.0f}  {mret:+.1f}%")
        lines.append("-" * 30)
        lines.append(f"{'TOTAL':8s} ₹{cape:>8,.0f}  {ret_sign}{ret_pct:.1f}%")
        lines.append("```")
    
    # Stats
    pnl_icon = "🟢" if total_pnl > 0 else ("🔴" if total_pnl < 0 else "⚪")
    win_icon = "🏆" if win_rate >= 70 else ("👍" if win_rate >= 50 else "👎")
    lines.append(f"{pnl_icon} *P&L:* Rs {total_pnl:+,.0f} | {win_icon} *Win:* {wins}/{total_closed} ({win_rate}%)")
    lines.append(f"📊 *Open:* {open_count} | *Closed:* {closed_count} | *Total:* {total_closed + open_count}")
    
    msg = "\n".join(lines)
    if len(msg) > 4000:
        msg = msg[:4000]
    return msg


def main():
    """Main bot entry point."""
    start_time = time.time()
    now = now_ist()
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    
    print(f"\n{'='*60}")
    print(f"  FREE 3-Market v5.0 PAPER TRADE BOT")
    print(f"  {date_str} {time_str}")
    print(f"{'='*60}")
    
    # ===== 1. Load strategies =====
    try:
        strategies = load_strategies()
    except Exception as e:
        log_error(f"Failed to load strategies: {e}")
        print(f"[FATAL] {e}")
        return
    
    # ===== 2. Get unique tickers =====
    tickers = unique_tickers(strategies)
    print(f"[Bot] {len(tickers)} unique tickers to scan: {', '.join(tickers[:10])}{'...' if len(tickers)>10 else ''}")
    
    # ===== 3. Download data for each ticker =====
    ticker_data = {}
    scan_errors = 0
    for yf_ticker in tickers:
        for attempt in range(3):
            try:
                print(f"[Bot] Downloading {yf_ticker}...", end=" ")
                df = yf.download(yf_ticker, period=YF_PERIOD, interval=YF_INTERVAL,
                                 progress=False, auto_adjust=False)
                if df is None or len(df) < 60:
                    print(f"INSUFFICIENT DATA ({len(df) if df is not None else 0} rows)")
                    if attempt < 2:
                        time.sleep(1)
                        continue
                    break
                
                # Compute indicators
                df = compute_indicators(df)
                ticker_data[yf_ticker] = df
                print(f"OK ({len(df)} rows, {df.index[-1].date()})")
                break
            except Exception as e:
                print(f"ERROR: {e}")
                if attempt < 2:
                    time.sleep(2)
                    continue
                scan_errors += 1
                log_error(f"Failed to download {yf_ticker}: {e}")
    
    print(f"[Bot] Data download complete: {len(ticker_data)}/{len(tickers)} tickers, {scan_errors} errors")
    
    # ===== 4. Compute market status =====
    # For each ticker, determine if it's in a region the bot should trade
    market_status = {}
    for yf_ticker, df in ticker_data.items():
        if df is not None and len(df) > 0:
            market_status[yf_ticker] = {
                "data_ok": True,
                "latest_close": float(df.iloc[-1]["Close"]),
                "latest_date": str(df.index[-1].date()),
                "region": get_region(yf_ticker),
            }
        else:
            market_status[yf_ticker] = {"data_ok": False}
    
    # ===== 5. Scan all strategies =====
    all_signals = scan_strategies(strategies, ticker_data)
    fired_signals = [s for s in all_signals if s.get("fired")]
    print(f"[Bot] Strategies checked: {len(all_signals)}, fired: {len(fired_signals)}")
    
    # ===== 6. Get best entries (1 per ticker per direction) =====
    best_entries = get_best_entries(all_signals)
    print(f"[Bot] Best entries: {len(best_entries)}")
    
    # ===== 7. Update existing positions =====
    current_prices = {}
    for yf_ticker, status in market_status.items():
        if status.get("data_ok"):
            current_prices[yf_ticker] = status["latest_close"]
    
    closed_msgs = update_trades(current_prices)
    print(f"[Bot] Closed trades: {len(closed_msgs)}")
    
    # ===== 8. Enter new trades =====
    entries = []
    for entry in best_entries:
        region = get_region(entry["ticker"], entry.get("region"))
        trade = enter_trade(
            mode=region,
            ticker=entry["ticker"],
            direction=entry["direction"],
            entry_price=entry["close"],
            reason=entry.get("factors", "")[:60],
            pattern_rank=entry.get("rank"),
        )
        if trade:
            entries.append({
                "ticker": entry["ticker"],
                "direction": entry["direction"],
                "close": entry["close"],
                "qty": trade["Qty"],
                "sl": trade["SL"],
                "target": trade["Target"],
                "rank": entry.get("rank"),
                "win_rate": entry.get("win_rate"),
            })
    
    print(f"[Bot] New entries: {len(entries)}")
    
    # ===== 9. Load portfolio state =====
    portfolio = load_portfolio()
    cap_by_mkt = portfolio.get("capital_by_market", dict(CAPITAL_BY_MARKET))
    total_cape = sum(cap_by_mkt.values())
    open_positions = portfolio.get("open_positions", [])
    total_pnl = portfolio.get("total_pnl", 0)
    closed_cnt = portfolio.get("closed_count", 0)
    wins = portfolio.get("total_wins", 0)
    losses = portfolio.get("total_losses", 0)
    
    # ===== 10. Generate portfolio report =====
    generate_portfolio_report()
    
    # ===== 11. Get market status & strategy stats =====
    mkt_status = get_market_status()
    strat_stats = get_strategy_stats(top_n=5)
    scan_summary = {
        "tickers_ok": len(ticker_data),
        "tickers_total": len(tickers),
        "strategies_total": len(all_signals),
        "strategies_fired": len(fired_signals),
        "errors": scan_errors,
    }
    
    # ===== 12. Send Telegram =====
    tg_msg = build_telegram_msg(
        date_str, time_str, entries, closed_msgs,
        total_cape, len(open_positions), total_pnl,
        wins, losses, closed_cnt,
        capital_by_market=cap_by_mkt,
        open_positions=open_positions,
        market_status=mkt_status,
        strategy_stats=strat_stats,
        scan_summary=scan_summary,
    )
    tg_status = send_telegram(tg_msg)
    
    # Also send the HTML portfolio report as a downloadable document
    if os.path.exists(PORTFOLIO_REPORT_FILE):
        doc_caption = f"📊 *Full Portfolio Report* — {date_str}\n"
        doc_caption += "Download & open in browser for beautiful formatted view"
        send_telegram_document(PORTFOLIO_REPORT_FILE, caption=doc_caption)
    
    # ===== 12. Log Daily Scan =====
    # Build fired & skipped pattern lists for audit trail
    fired_patterns = []
    for s in all_signals:
        if s.get("fired"):
            fired_patterns.append({
                "rank": s["rank"], "market": s["market"], "ticker": s["ticker"],
                "direction": s["direction"], "factors": s["factors"],                    "win_rate": s["win_rate"],
                "reason": "All factors met",
            })
    
    # Why some fired signals were skipped (duplicate ticker or SHORT not allowed)
    fired_keys = {(e["ticker"], e["direction"]) for e in best_entries}
    skipped_patterns = []
    for s in fired_signals:
        key = (s["ticker"], s["direction"])
        if key not in fired_keys:
            skip_reason = "Better pattern won (higher win rate)"
            if s["direction"] == "SHORT":
                region = s.get("region", "").upper()
                if region in ("INDIA", "INDIAN"):
                    skip_reason = "SHORT not allowed for INDIAN market (cash)"
            skipped_patterns.append({
                "rank": s["rank"], "market": s["market"], "ticker": s["ticker"],
                "direction": s["direction"], "factors": s["factors"],
                "win_rate": s["win_rate"],
                "skip_reason": skip_reason,
            })
    
    scan_data = {
        "date": date_str,
        "time": time_str,
        "tickers_scanned": [
            {"ticker": t, "region": market_status.get(t, {}).get("region", "?"),
             "close": market_status.get(t, {}).get("latest_close", 0),
             "data_ok": market_status.get(t, {}).get("data_ok", False)}
            for t in sorted(ticker_data.keys())
        ],
        "patterns_checked": [
            {"rank": s["rank"], "market": s["market"], "ticker": s["ticker"],
             "direction": s["direction"], "factors": s["factors"],
             "fired": s["fired"], "win_rate": s["win_rate"],
             "reason": s.get("reason", "")}
            for s in all_signals
        ],
        "fired_patterns": fired_patterns,
        "skipped_patterns": skipped_patterns,
        "entries": entries,
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
    
    # ===== 13. Log trade run summary =====
    log_trade_run({
        "Date": date_str,
        "Time": time_str,
        "Tickers_Scanned": len(ticker_data),
        "Errors": scan_errors,
        "Patterns_Total": len(all_signals),
        "Patterns_Fired": len(fired_signals),
        "New_Entries": len(entries),
        "Closed_Trades": len(closed_msgs),
        "Open_Positions": len(open_positions),
        "Capital": round(total_cape, 0),
        "Total_PnL": round(total_pnl, 0),
        "Telegram": tg_status,
    })
    
    # ===== 14. Log portfolio snapshot =====
    log_portfolio(total_cape, open_positions, closed_cnt, wins, losses, total_pnl,
                  capital_by_market=cap_by_mkt)
    
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"  BOT RUN COMPLETE — {elapsed:.1f}s")
    print(f"  Tickers: {len(ticker_data)} | Fired: {len(fired_signals)}")
    print(f"  Entered: {len(entries)} | Closed: {len(closed_msgs)}")
    print(f"  Capital: Rs {total_cape:,.0f} (IND:₹{cap_by_mkt.get('INDIAN',0):,.0f} US:₹{cap_by_mkt.get('US',0):,.0f} CRYP:₹{cap_by_mkt.get('CRYPTO',0):,.0f}) | PnL: Rs {total_pnl:+,.0f}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error(f"Bot crashed: {e}")
        print(f"[FATAL] Bot crashed: {e}")
        traceback.print_exc()

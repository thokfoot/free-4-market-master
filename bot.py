"""
FREE 4-Market v5.0 — PROFESSIONAL PAPER TRADING BOT
====================================================
Daily scan: loads 50 strategies, downloads yfinance data,
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
    CAPITAL, TELEGRAM_TOKEN, TELEGRAM_CHAT_ID,
    YF_PERIOD, YF_INTERVAL, get_region,
)
from scanner import load_strategies, unique_tickers, compute_indicators, scan_strategies, get_best_entries
from paper_trader import enter_trade, update_trades, load_portfolio, round_price
from logger import log_scan, log_trade_run, log_portfolio, log_error, now_ist


def send_telegram(msg: str) -> str:
    """Send Telegram message. Returns status string."""
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TG] Missing TELEGRAM_TOKEN or TELEGRAM_CHAT_ID")
        return "NoToken"
    
    api_url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    for attempt in range(3):
        try:
            r = requests.post(
                api_url,
                data={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
                timeout=15,
            )
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


def build_telegram_msg(date_str: str, time_str: str, entries: list,
                       closed_msgs: list, cape: float, open_count: int,
                       total_pnl: float, wins: int = 0, losses: int = 0,
                       closed_count: int = 0) -> str:
    """
    Professional Telegram message — ALWAYS shows portfolio summary.
    
    - Header with date/time
    - New entries (if any): DIRECTION TICKER Qty @ PRICE SL TGT
    - Closed trades (if any): PnL with exit reason
    - Portfolio summary: Capital, Return %, Open/Closed/WinRate, Total PnL
    """
    ret_pct = ((cape - CAPITAL) / CAPITAL) * 100 if CAPITAL > 0 else 0
    total_closed = wins + losses
    win_rate = round(wins / total_closed * 100) if total_closed > 0 else 0
    
    # Return arrow
    if ret_pct > 0:
        arrow = "▲"
    elif ret_pct < 0:
        arrow = "▼"
    else:
        arrow = "◆"
    
    lines = []
    
    # Header
    short_time = time_str.split(":")[0] + ":" + time_str.split(":")[1]
    lines.append(f"🤖 *PAPER TRADE v5.0* | {date_str} {short_time}")
    
    # New entries
    if entries:
        lines.append("")
        for t in entries:
            action = "BUY" if t["direction"] == "LONG" else "SELL"
            lines.append(
                f"📈 {action} {t['ticker']} {round_price(t['close'])} "
                f"| Qty {t['qty']} SL {t['sl']} TGT {t['target']}"
            )
    
    # Closed trades
    if closed_msgs:
        lines.append("")
        for c in closed_msgs:
            lines.append(f"✅ {c}")
    
    # Portfolio summary (ALWAYS)
    lines.append("")
    lines.append(f"💰 *Capital:* Rs {cape:,.0f}  {arrow} {ret_pct:+.2f}%")
    lines.append(f"📊 Open: {open_count} | Closed: {closed_count} | Win: {win_rate}%")
    lines.append(f"💵 *Total P&L:* Rs {total_pnl:+,.0f}")
    
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
    print(f"  FREE 4-Market v5.0 PAPER TRADE BOT")
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
    cape = portfolio["capital"]
    open_positions = portfolio.get("open_positions", [])
    total_pnl = portfolio.get("total_pnl", 0)
    closed_cnt = portfolio.get("closed_count", 0)
    wins = portfolio.get("total_wins", 0)
    losses = portfolio.get("total_losses", 0)
    
    # ===== 10. Send Telegram =====
    tg_msg = build_telegram_msg(
        date_str, time_str, entries, closed_msgs,
        cape, len(open_positions), total_pnl,
        wins, losses, closed_cnt,
    )
    tg_status = send_telegram(tg_msg)
    
    # ===== 11. Log Daily Scan =====
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
        "entries": entries,
        "portfolio": {
            "capital": cape, "open_count": len(open_positions),
            "total_pnl": total_pnl, "closed_count": closed_cnt,
            "wins": wins, "losses": losses,
        },
        "telegram_status": tg_status,
        "duration_sec": round(time.time() - start_time, 1),
    }
    log_scan(scan_data)
    
    # ===== 12. Log trade run summary =====
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
        "Capital": round(cape, 0),
        "Total_PnL": round(total_pnl, 0),
        "Telegram": tg_status,
    })
    
    # ===== 13. Log portfolio snapshot =====
    log_portfolio(cape, open_positions, closed_cnt, wins, losses, total_pnl)
    
    elapsed = time.time() - start_time
    print(f"\n{'='*60}")
    print(f"  BOT RUN COMPLETE — {elapsed:.1f}s")
    print(f"  Tickers: {len(ticker_data)} | Fired: {len(fired_signals)}")
    print(f"  Entered: {len(entries)} | Closed: {len(closed_msgs)}")
    print(f"  Capital: Rs {cape:,.0f} | PnL: Rs {total_pnl:+,.0f}")
    print(f"{'='*60}\n")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log_error(f"Bot crashed: {e}")
        print(f"[FATAL] Bot crashed: {e}")
        traceback.print_exc()

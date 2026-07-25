"""
FREE 3-Market v5.0 — PAPER TRADER
===================================
Portfolio & trade management for paper trading.
Supports LONG and SHORT positions with SL/TP and max hold.
"""

import os, json, pandas as pd
from datetime import datetime
import pytz
from config import CAPITAL, CAPITAL_BY_MARKET, TOTAL_CAPITAL, RISK_PER_TRADE, SL_PCT, TP_PCT, MAX_HOLD_DAYS, MAX_CONCURRENT, STRATEGY_FILE, CHARGES_PER_MARKET

IST = pytz.timezone("Asia/Kolkata")
LOG_DIR = "logs"
os.makedirs(LOG_DIR, exist_ok=True)

PAPER_FILE = os.path.join(LOG_DIR, "paper_trades.csv")
PORTFOLIO_FILE = os.path.join(LOG_DIR, "portfolio.json")
STRATEGY_STATS_FILE = os.path.join(LOG_DIR, "strategy_stats.json")
COLUMNS = [
    "Date","Time_IST","Mode","Ticker","Direction",
    "Entry_Price","Qty","SL","Target","MaxHold",
    "Exit_Price","Exit_Time","P&L","P&L_%","Status",
    "Pattern_Rank","Expected_WinRate","Pattern_Factors","Reason"
]

AUDIT_FILE = os.path.join(LOG_DIR, "trade_audit.json")


def _load_audit() -> list:
    """Load persistent trade audit log."""
    if os.path.exists(AUDIT_FILE):
        try:
            with open(AUDIT_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return []


def _save_audit(audit: list):
    """Save persistent trade audit log."""
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(AUDIT_FILE, "w") as f:
        json.dump(audit, f, indent=2)


def _log_audit_entry(trade: dict):
    """Log a trade entry to the persistent audit trail."""
    audit = _load_audit()
    audit.append({
        "event": "ENTRY",
        "datetime": f"{trade['Date']} {trade['Time_IST']}",
        "mode": trade["Mode"],
        "ticker": trade["Ticker"],
        "direction": trade["Direction"],
        "entry_price": trade["Entry_Price"],
        "qty": trade["Qty"],
        "sl": trade["SL"],
        "target": trade["Target"],
        "pattern_rank": trade.get("Pattern_Rank", ""),
        "expected_win_rate": trade.get("Expected_WinRate", ""),
        "pattern_factors": trade.get("Pattern_Factors", ""),
        "reason": trade.get("Reason", ""),
    })
    _save_audit(audit)
    print(f"[Audit] ENTRY logged: {trade['Direction']} {trade['Ticker']} Rank#{trade.get('Pattern_Rank','?')} Expected WR={trade.get('Expected_WinRate','?')}%")


def _log_audit_exit(trade_row: dict):
    """Log a trade exit to the persistent audit trail."""
    audit = _load_audit()
    audit.append({
        "event": "EXIT",
        "datetime": trade_row.get("Exit_Time", ""),
        "mode": trade_row.get("Mode", ""),
        "ticker": trade_row.get("Ticker", ""),
        "direction": trade_row.get("Direction", ""),
        "entry_price": trade_row.get("Entry_Price", ""),
        "exit_price": trade_row.get("Exit_Price", ""),
        "qty": trade_row.get("Qty", 0),
        "pnl": trade_row.get("P&L", ""),
        "pnl_pct": trade_row.get("P&L_%", ""),
        "pattern_rank": trade_row.get("Pattern_Rank", ""),
        "expected_win_rate": trade_row.get("Expected_WinRate", ""),
        "pattern_factors": trade_row.get("Pattern_Factors", ""),
        "reason": trade_row.get("Reason", ""),
    })
    _save_audit(audit)
    print(f"[Audit] EXIT logged: {trade_row.get('Direction','?')} {trade_row.get('Ticker','?')} P&L={trade_row.get('P&L','?')} Expected WR={trade_row.get('Expected_WinRate','?')}%")


def round_price(price):
    """Round price appropriately based on magnitude."""
    p = float(price)
    if p >= 1000: return round(p, 2)
    if p >= 100: return round(p, 2)
    if p >= 1: return round(p, 2)
    if p >= 0.1: return round(p, 4)
    if p >= 0.01: return round(p, 6)
    return round(p, 8)


def _default_portfolio() -> dict:
    """Return default portfolio with per-market capital."""
    return {
        "capital_by_market": dict(CAPITAL_BY_MARKET),
        "open_positions": [],
        "closed_count": 0,
        "total_wins": 0,
        "total_losses": 0,
        "total_pnl": 0,
        "total_pnl_by_market": {"INDIAN": 0.0, "US": 0.0, "CRYPTO": 0.0},
    }


def load_portfolio() -> dict:
    """Load portfolio from JSON file. Migrates old format automatically."""
    if os.path.exists(PORTFOLIO_FILE):
        try:
            with open(PORTFOLIO_FILE, "r") as f:
                data = json.load(f)
            # Migrate old format (single capital -> per-market)
            if "capital" in data and "capital_by_market" not in data:
                port = _default_portfolio()
                port["open_positions"] = data.get("open_positions", [])
                port["closed_count"] = data.get("closed_count", 0)
                port["total_wins"] = data.get("total_wins", 0)
                port["total_losses"] = data.get("total_losses", 0)
                port["total_pnl"] = data.get("total_pnl", 0)
                # Distribute old capital proportionally (fallback: equal split)
                old_cap = float(data.get("capital", sum(CAPITAL_BY_MARKET.values())))
                total_init = sum(CAPITAL_BY_MARKET.values())
                ratio = old_cap / total_init if total_init > 0 else 1
                for mkt in port["capital_by_market"]:
                    port["capital_by_market"][mkt] = CAPITAL_BY_MARKET[mkt] * ratio
                # Distribute old PnL
                old_pnl = float(data.get("total_pnl", 0))
                if old_pnl != 0 and port["open_positions"]:
                    # Split PnL equally if we don't have per-market records
                    per_market = old_pnl / 3
                    for mkt in port["total_pnl_by_market"]:
                        port["total_pnl_by_market"][mkt] = round(per_market, 2)
                save_portfolio(port)
                print(f"[Paper] Migrated portfolio to per-market format")
                return port
            return data
        except Exception as e:
            print(f"[Paper] Portfolio load error: {e}, using defaults")
    return _default_portfolio()


def save_portfolio(port: dict):
    """Save portfolio to JSON."""
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(PORTFOLIO_FILE, "w") as f:
        json.dump(port, f, indent=2)


def calculate_qty(entry: float, sl: float, market: str = "US") -> int:
    """Calculate position size based on risk per trade (1% of market capital)."""
    port = load_portfolio()
    mkt_cap = port.get("capital_by_market", {}).get(market, CAPITAL_BY_MARKET.get(market, 100000))
    risk_amt = mkt_cap * RISK_PER_TRADE
    risk_per_share = abs(entry - sl)
    if risk_per_share < 1e-9:
        return 0
    qty = int(risk_amt / risk_per_share)
    # Caps based on price
    if entry < 0.1:
        qty = min(qty, 50000)
    elif entry < 1:
        qty = min(qty, 10000)
    elif entry > 100:
        qty = min(qty, 5000)
    return max(1, qty)


def enter_trade(mode: str, ticker: str, direction: str, entry_price: float,
                 reason: str, pattern_rank: int = None,
                 expected_win_rate: float = None,
                 pattern_factors: str = None) -> dict:
    """
    Enter a paper trade.
    
    Args:
        mode: "INDIAN" | "US" | "CRYPTO"
        ticker: yfinance ticker
        direction: "LONG" | "SHORT"
        entry_price: Entry price
        reason: Signal description
        pattern_rank: Rank from CSV (optional)
    
    Returns:
        Trade dict or None if rejected
    """
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    portfolio = load_portfolio()
    
    # Check max concurrent
    if len(portfolio["open_positions"]) >= MAX_CONCURRENT:
        print(f"[Paper] MAX CONCURRENT ({MAX_CONCURRENT}), skip {ticker}")
        return None
    
    # Check duplicate (same ticker, same direction, open)
    for pos in portfolio["open_positions"]:
        if pos["Ticker"] == ticker and pos["Direction"] == direction:
            print(f"[Paper] Duplicate {ticker} {direction} already open, skip")
            return None
    
    # Calculate SL/TP based on direction
    if direction == "LONG":
        sl = round_price(entry_price * (1 - SL_PCT))
        target = round_price(entry_price * (1 + TP_PCT))
    else:  # SHORT
        sl = round_price(entry_price * (1 + SL_PCT))
        target = round_price(entry_price * (1 - TP_PCT))
    
    entry = round_price(entry_price)
    qty = calculate_qty(entry, sl, mode)
    if qty == 0:
        return None
    
    # Build reason with pattern rank
    full_reason = reason
    if pattern_rank:
        full_reason = f"#{pattern_rank} {reason}"
    
    trade = {
        "Date": date_str,
        "Time_IST": time_str,
        "Mode": mode,
        "Ticker": ticker,
        "Direction": direction,
        "Entry_Price": entry,
        "Qty": qty,
        "SL": sl,
        "Target": target,
        "MaxHold": MAX_HOLD_DAYS,
        "Exit_Price": "",
        "Exit_Time": "",
        "P&L": "",
        "P&L_%": "",
        "Status": "OPEN",
        "Pattern_Rank": pattern_rank if pattern_rank else "",
        "Expected_WinRate": expected_win_rate if expected_win_rate else "",
        "Pattern_Factors": pattern_factors if pattern_factors else "",
        "Reason": full_reason,
    }
    
    # Append to CSV
    df_new = pd.DataFrame([trade])[COLUMNS]
    os.makedirs(LOG_DIR, exist_ok=True)
    os.makedirs(LOG_DIR, exist_ok=True)
    if os.path.exists(PAPER_FILE):
        df_old = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        df_comb = pd.concat([df_old, df_new], ignore_index=True)
    else:
        df_comb = df_new
    df_comb.to_csv(PAPER_FILE, index=False)
    
    # Update portfolio (market-specific capital remains unchanged at entry)
    portfolio["open_positions"].append(trade)
    save_portfolio(portfolio)
    
    print(f"[Paper] ENTER {direction} {ticker} @ {entry} Qty {qty} SL {sl} TGT {target}")
    
    # Log to persistent audit trail
    _log_audit_entry(trade)
    
    return trade


# ===== PER-STRATEGY WIN RATE TRACKING =====

def _extract_rank(reason: str) -> int:
    """Extract pattern rank from Reason field like '#1 Price<SMA50+EMA9>EMA20'."""
    if not reason:
        return 0
    import re
    match = re.match(r"#?(\d+)", reason.strip())
    if match:
        return int(match.group(1))
    return 0


def _load_strategy_stats() -> dict:
    """Load per-strategy win/Loss tracking from JSON."""
    if os.path.exists(STRATEGY_STATS_FILE):
        try:
            with open(STRATEGY_STATS_FILE, "r") as f:
                return json.load(f)
        except:
            pass
    return {}


def _save_strategy_stats(stats: dict):
    """Save per-strategy tracking."""
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(STRATEGY_STATS_FILE, "w") as f:
        json.dump(stats, f, indent=2)


def update_strategy_stats(reason: str, pnl: float):
    """
    Update win/loss tracking for the pattern that generated this trade.
    Called when a trade is closed.
    """
    rank = _extract_rank(reason)
    if rank == 0:
        return
    
    stats = _load_strategy_stats()
    key = str(rank)
    
    if key not in stats:
        stats[key] = {
            "rank": rank,
            "factors": reason[:80],
            "wins": 0,
            "losses": 0,
            "total_pnl": 0.0,
        }
    
    stats[key]["total_pnl"] += pnl
    if pnl > 0:
        stats[key]["wins"] += 1
    else:
        stats[key]["losses"] += 1
    # Update the reason/factors in case it was truncated
    if len(reason) > len(stats[key]["factors"]):
        stats[key]["factors"] = reason[:80]
    
    _save_strategy_stats(stats)
    total = stats[key]["wins"] + stats[key]["losses"]
    wr = round(stats[key]["wins"] / total * 100, 1) if total > 0 else 0
    print(f"[Strategy] Rank #{rank} updated: {stats[key]['wins']}W/{stats[key]['losses']}L ({wr}%) PnL Rs {pnl:+.0f}")


def get_strategy_stats(top_n: int = 5) -> list:
    """
    Get best and worst strategies by win rate.
    
    Returns:
        {
            "top": [{rank, factors, wins, losses, win_rate, total_pnl}, ...],
            "bottom": [{rank, factors, wins, losses, win_rate, total_pnl}, ...],
        }
    """
    stats = _load_strategy_stats()
    if not stats:
        return {"top": [], "bottom": []}
    
    rows = []
    for key, data in stats.items():
        total = data.get("wins", 0) + data.get("losses", 0)
        wr = round(data["wins"] / total * 100, 1) if total > 0 else 0
        rows.append({
            "rank": data["rank"],
            "factors": data.get("factors", "")[:50],
            "wins": data["wins"],
            "losses": data["losses"],
            "total": total,
            "win_rate": wr,
            "total_pnl": round(data.get("total_pnl", 0), 0),
        })
    
    # Sort by win rate descending
    rows.sort(key=lambda x: x["win_rate"], reverse=True)
    top = rows[:top_n]
    
    # Sort by win rate ascending (worst first)
    rows.sort(key=lambda x: x["win_rate"])
    bottom = [r for r in rows if r["total"] >= 2][:top_n]  # Only if 2+ trades
    
    return {"top": top, "bottom": bottom}


def _html_escape(text):
    """Escape text for safe HTML embedding."""
    if text is None:
        return ""
    s = str(text)
    return (s.replace("&", "&amp;")
             .replace("<", "&lt;")
             .replace(">", "&gt;")
             .replace('"', "&quot;")
             .replace("\n", " "))


def _pnl_class(val):
    """Return CSS class for P&L value."""
    try:
        v = float(val)
        if v > 0: return "profit"
        if v < 0: return "loss"
    except:
        pass
    return ""


def generate_portfolio_report():
    """
    Generate a professional HTML portfolio report with EVERYTHING.
    
    Sections:
      1. Summary Cards — Capital, Return, Win Rate, Trades
      2. All Trades Table — Full audit trail
      3. Per-Ticker — Win rate, PnL per ticker
      4. Per-Market — Performance by region
      5. Portfolio History — Capital snapshots
    
    File: logs/portfolio_report.html (viewable in browser)
    """
    now = datetime.now(IST)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H:%M:%S IST")
    
    portfolio = load_portfolio()
    cap_by_mkt = portfolio.get("capital_by_market", dict(CAPITAL_BY_MARKET))
    total_cape = sum(cap_by_mkt.values())
    open_count = len(portfolio.get("open_positions", []))
    closed_cnt = portfolio.get("closed_count", 0)
    wins = portfolio.get("total_wins", 0)
    losses = portfolio.get("total_losses", 0)
    total_pnl = portfolio.get("total_pnl", 0)
    total_closed = wins + losses
    win_rate = round(wins / total_closed * 100, 1) if total_closed > 0 else 0
    ret_pct = round((total_cape - TOTAL_CAPITAL) / TOTAL_CAPITAL * 100, 2)
    total_trades = total_closed + open_count
    
    # Profit/loss direction
    pnl_direction = "▲" if ret_pct > 0 else ("▼" if ret_pct < 0 else "◆")
    pnl_color = "#00c853" if ret_pct > 0 else ("#ff5252" if ret_pct < 0 else "#888")
    
    parts = []
    parts.append(f'''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Portfolio Report — {_html_escape(date_str)}</title>
<style>
  *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
    background: #0d1117; color: #e6edf3; padding: 24px; line-height: 1.6;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ font-size: 1.6rem; font-weight: 700; margin-bottom: 4px; }}
  h2 {{ font-size: 1.1rem; font-weight: 600; margin: 28px 0 14px; color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 6px; }}
  .subtitle {{ color: #8b949e; font-size: 0.85rem; margin-bottom: 20px; }}
  .cards {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 12px; margin-bottom: 24px; }}
  .card {{
    background: #161b22; border: 1px solid #30363d; border-radius: 8px;
    padding: 16px; text-align: center; transition: border-color .2s;
  }}
  .card:hover {{ border-color: #58a6ff; }}
  .card .value {{ font-size: 1.5rem; font-weight: 700; margin: 4px 0 2px; }}
  .card .label {{ font-size: 0.75rem; color: #8b949e; text-transform: uppercase; letter-spacing: 0.5px; }}
  .card .change {{ font-size: 0.85rem; }}
  .mkt-row {{ display: flex; justify-content: space-around; flex-wrap: wrap; gap: 8px; margin-bottom: 16px; }}
  .mkt-item {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 10px 16px; text-align: center; min-width: 130px; }}
  .mkt-item .mkt-label {{ font-size: 0.7rem; color: #8b949e; text-transform: uppercase; }}
  .mkt-item .mkt-value {{ font-size: 1.1rem; font-weight: 700; }}
  table {{
    width: 100%; border-collapse: collapse; font-size: 0.82rem;
    background: #161b22; border: 1px solid #30363d; border-radius: 8px; overflow: hidden;
  }}
  th, td {{ padding: 8px 10px; text-align: left; border-bottom: 1px solid #21262d; }}
  th {{ background: #21262d; color: #8b949e; font-weight: 600; text-transform: uppercase; font-size: 0.7rem; letter-spacing: 0.5px; }}
  tr:hover {{ background: #1c2128; }}
  .profit {{ color: #00c853 !important; font-weight: 600; }}
  .loss {{ color: #ff5252 !important; font-weight: 600; }}
  .badge {{
    display: inline-block; padding: 2px 8px; border-radius: 12px;
    font-size: 0.7rem; font-weight: 600; text-transform: uppercase;
  }}
  .badge-long {{ background: #003d1a; color: #00c853; }}
  .badge-short {{ background: #3d0000; color: #ff5252; }}
  .badge-open {{ background: #003055; color: #58a6ff; }}
  .badge-closed {{ background: #21262d; color: #8b949e; }}
  .badge-sl {{ background: #3d0000; color: #ff5252; }}
  .badge-target {{ background: #003d1a; color: #00c853; }}
  .badge-expiry {{ background: #3d2e00; color: #ffc107; }}
  .section-summary {{ color: #8b949e; font-size: 0.82rem; margin-top: 6px; }}
  .footer {{ margin-top: 30px; padding-top: 16px; border-top: 1px solid #30363d; text-align: center; color: #484f58; font-size: 0.75rem; }}
  .scroll {{ overflow-x: auto; }}
  @media (max-width: 600px) {{
    body {{ padding: 12px; }}
    .cards {{ grid-template-columns: repeat(2, 1fr); gap: 8px; }}
    .mkt-row {{ gap: 4px; }}
    .mkt-item {{ min-width: 100px; padding: 8px; }}
  }}
</style>
</head>
<body>
<div class="container">
  <h1>📊 Portfolio Report — v5.0</h1>
  <div class="subtitle">{_html_escape(date_str)} {_html_escape(time_str)} • Generated by FREE 3-Market Paper Trade Bot • <a href="https://github.com/thokfoot/free-4-market-master" style="color:#58a6ff">thokfoot/free-4-market-master</a></div>
  
  <div class="cards">
    <div class="card"><div class="label">Total Capital</div><div class="value" style="color:{pnl_color}">₹{total_cape:,.0f}</div><div class="change" style="color:{pnl_color}">{pnl_direction} {ret_pct:+.2f}%</div></div>
    <div class="card"><div class="label">Total P&amp;L</div><div class="value" style="color:{"#00c853" if total_pnl>0 else "#ff5252" if total_pnl<0 else "#888"}">₹{total_pnl:+,.0f}</div><div class="change">{total_trades} trades</div></div>
    <div class="card"><div class="label">Win Rate</div><div class="value">{win_rate}%</div><div class="change">{wins}W / {losses}L</div></div>
    <div class="card"><div class="label">Open / Closed</div><div class="value">{open_count} / {closed_cnt}</div><div class="change">{total_closed + open_count} total</div></div>
  </div>
  
  <div class="mkt-row">
''')
    # Per-market cards
    for mkt in ["INDIAN", "US", "CRYPTO"]:
        mkt_cap = cap_by_mkt.get(mkt, 100000)
        mkt_init = CAPITAL_BY_MARKET.get(mkt, 100000)
        mkt_ret = ((mkt_cap - mkt_init) / mkt_init * 100) if mkt_init > 0 else 0
        mkt_arrow = "▲" if mkt_ret > 0 else ("▼" if mkt_ret < 0 else "◆")
        mkt_clr = "#00c853" if mkt_ret > 0 else ("#ff5252" if mkt_ret < 0 else "#888")
        mkt_icon = {"INDIAN": "🇮🇳", "US": "🇺🇸", "CRYPTO": "₿"}
        parts.append(f'    <div class="mkt-item"><div class="mkt-label">{mkt_icon.get(mkt,"")} {mkt}</div>'
                     f'<div class="mkt-value" style="color:{mkt_clr}">₹{mkt_cap:,.0f}</div>'
                     f'<div style="font-size:0.75rem;color:{mkt_clr}">{mkt_arrow} {mkt_ret:+.2f}%</div></div>')
    parts.append('  </div>')
    
    # ============================================================
    # SECTION 2: ALL TRADES
    # ============================================================
    parts.append('<h2>📋 All Trades</h2>')
    
    if os.path.exists(PAPER_FILE):
        df = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        if len(df) > 0:
            parts.append('<div class="scroll"><table>')
            cols = ["Date", "Ticker", "Direction", "Entry_Price", "Qty", "SL", "Target", 
                    "Exit_Price", "P&L", "P&L_%", "Status", "Reason"]
            cols = [c for c in cols if c in df.columns]
            
            parts.append("<tr>" + "".join(f"<th>{c.replace('_', ' ')}</th>" for c in cols) + "</tr>")
            
            for _, row in df.iterrows():
                direction = str(row.get("Direction", ""))
                status = str(row.get("Status", ""))
                pnl = row.get("P&L", "")
                pnl_pct = row.get("P&L_%", "")
                
                badge_dir = f'<span class="badge badge-{"long" if direction=="LONG" else "short"}">{_html_escape(direction)}</span>'
                badge_status = f'<span class="badge badge-{"open" if status=="OPEN" else "closed"}">{_html_escape(status)}</span>'
                
                pnl_display = _html_escape(pnl)
                pnl_class = _pnl_class(pnl)
                pnl_pct_display = _html_escape(pnl_pct)
                pnl_pct_class = _pnl_class(pnl_pct)
                
                exit_price = row.get("Exit_Price", "")
                exit_display = _html_escape(exit_price) if pd.notna(exit_price) and str(exit_price).strip() != "" else "—"
                
                reason = str(row.get("Reason", ""))
                reason_short = reason[:50] + ("..." if len(reason) > 50 else "")
                
                parts.append(f"<tr><td>{_html_escape(row.get('Date',''))}</td>")
                parts.append(f"<td><b>{_html_escape(row.get('Ticker',''))}</b></td>")
                parts.append(f"<td>{badge_dir}</td>")
                parts.append(f"<td>{_html_escape(row.get('Entry_Price',''))}</td>")
                parts.append(f"<td>{_html_escape(row.get('Qty',''))}</td>")
                parts.append(f"<td>{_html_escape(row.get('SL',''))}</td>")
                parts.append(f"<td>{_html_escape(row.get('Target',''))}</td>")
                parts.append(f"<td>{exit_display}</td>")
                parts.append(f'<td class="{pnl_class}">{pnl_display}</td>')
                parts.append(f'<td class="{pnl_pct_class}">{pnl_pct_display}</td>')
                parts.append(f"<td>{badge_status}</td>")
                parts.append(f"<td style=\"font-size:0.75rem;color:#8b949e\">{_html_escape(reason_short)}</td>")
                parts.append("</tr>")
            parts.append('</table></div>')
            parts.append(f'<div class="section-summary">Showing {len(df)} trade(s)</div>')
        else:
            parts.append('<div style="color:#8b949e;padding:20px;text-align:center">No trades recorded yet.</div>')
    else:
        parts.append('<div style="color:#8b949e;padding:20px;text-align:center">No trades recorded yet.</div>')
    
    # ============================================================
    # SECTION 3: PER-TICKER PERFORMANCE
    # ============================================================
    if os.path.exists(PAPER_FILE):
        df_trades = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        if len(df_trades) > 0:
            parts.append('<h2>📈 Per-Ticker Performance</h2>')
            parts.append('<div class="scroll"><table>')
            parts.append("<tr><th>Ticker</th><th>Direction</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>Total P&amp;L</th></tr>")
            
            for (ticker, direction), grp in df_trades.groupby(["Ticker", "Direction"]):
                pnl_vals = []
                for _, r in grp.iterrows():
                    v = r.get("P&L")
                    if pd.notna(v) and str(v).strip() != "":
                        try:
                            pnl_vals.append(float(v))
                        except:
                            pass
                w = sum(1 for v in pnl_vals if v > 0)
                l = sum(1 for v in pnl_vals if v < 0)
                t = len(grp)
                wr = round(w / (w + l) * 100, 1) if (w + l) > 0 else 0
                total_pnl_t = round(sum(pnl_vals), 0)
                pnl_c = "profit" if total_pnl_t > 0 else ("loss" if total_pnl_t < 0 else "")
                badge_d = f'<span class="badge badge-{"long" if direction=="LONG" else "short"}">{_html_escape(direction)}</span>'
                
                parts.append(f"<tr><td><b>{_html_escape(ticker)}</b></td><td>{badge_d}</td>")
                parts.append(f"<td>{t}</td><td>{w}</td><td>{l}</td><td>{wr}%</td>")
                parts.append(f'<td class="{pnl_c}">₹{total_pnl_t:+,.0f}</td></tr>')
            parts.append('</table></div>')
    
    # ============================================================
    # SECTION 4: PER-MARKET PERFORMANCE
    # ============================================================
    if os.path.exists(PAPER_FILE):
        df_trades = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
        if len(df_trades) > 0 and "Mode" in df_trades.columns:
            parts.append('<h2>🌍 Per-Market Performance</h2>')
            parts.append('<div class="scroll"><table>')
            parts.append("<tr><th>Region</th><th>Trades</th><th>Wins</th><th>Losses</th><th>Win Rate</th><th>Total P&amp;L</th></tr>")
            
            for region, grp in df_trades.groupby("Mode"):
                pnl_vals = []
                for _, r in grp.iterrows():
                    v = r.get("P&L")
                    if pd.notna(v) and str(v).strip() != "":
                        try:
                            pnl_vals.append(float(v))
                        except:
                            pass
                w = sum(1 for v in pnl_vals if v > 0)
                l = sum(1 for v in pnl_vals if v < 0)
                t = len(grp)
                wr = round(w / (w + l) * 100, 1) if (w + l) > 0 else 0
                total_pnl_r = round(sum(pnl_vals), 0)
                pnl_c = "profit" if total_pnl_r > 0 else ("loss" if total_pnl_r < 0 else "")
                
                parts.append(f"<tr><td><b>{_html_escape(region)}</b></td><td>{t}</td><td>{w}</td><td>{l}</td><td>{wr}%</td>")
                parts.append(f'<td class="{pnl_c}">₹{total_pnl_r:+,.0f}</td></tr>')
            parts.append('</table></div>')
    
    # ============================================================
    # SECTION 5: PORTFOLIO HISTORY (from snapshots CSV)
    # ============================================================
    PORTFOLIO_LOG = os.path.join(LOG_DIR, "portfolio_snapshots.csv")
    if os.path.exists(PORTFOLIO_LOG):
        df_snap = pd.read_csv(PORTFOLIO_LOG, on_bad_lines='warn')
        if len(df_snap) > 0:
            parts.append('<h2>📅 Portfolio History</h2>')
            parts.append('<div class="scroll"><table>')
            cols = ["Date", "Time", "Capital", "Return_Pct", "Open", "Win_Rate", "Total_PnL"]
            cols = [c for c in cols if c in df_snap.columns]
            parts.append("<tr>" + "".join(f"<th>{c.replace('_', ' ')}</th>" for c in cols) + "</tr>")
            for _, row in df_snap.tail(20).iterrows():
                parts.append("<tr>")
                for c in cols:
                    val = row.get(c, "")
                    if c in ("Capital", "Total_PnL") and pd.notna(val):
                        try:
                            val = f"₹{float(val):,.0f}"
                        except:
                            pass
                    if c == "Return_Pct" and pd.notna(val):
                        cls = "profit" if float(val) > 0 else ("loss" if float(val) < 0 else "")
                        val = f'<span class="{cls}">{_html_escape(val)}%</span>'
                    parts.append(f"<td>{val}</td>")
                parts.append("</tr>")
            parts.append('</table></div>')
    
    # Footer
    parts.append(f'''
  <div class="footer">
    FREE 3-Market v5.0 Paper Trade Bot &bull;
    Generated {_html_escape(date_str)} {_html_escape(time_str)} &bull;
    <a href="https://github.com/thokfoot/free-4-market-master" style="color:#484f58">GitHub</a>
  </div>
</div>
</body>
</html>''')
    
    # Write
    report_file = os.path.join(LOG_DIR, "portfolio_report.html")
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(report_file, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    
    report_size = os.path.getsize(report_file)
    print(f"[Report] Portfolio report generated: {report_file} ({report_size:,} bytes)")
    return report_file


def update_trades(current_prices: dict) -> list:
    """
    Check all open positions for SL/TP/MaxHold exit.
    
    Args:
        current_prices: {ticker: current_price}
    
    Returns:
        List of closed trade messages
    """
    if not os.path.exists(PAPER_FILE):
        return []
    
    df = pd.read_csv(PAPER_FILE, on_bad_lines='warn')
    portfolio = load_portfolio()
    updated = False
    closed_msgs = []
    now = datetime.now(IST)
    time_str = now.strftime("%H:%M:%S IST")
    
    for idx, row in df.iterrows():
        if row["Status"] != "OPEN":
            continue
        
        ticker = row["Ticker"]
        if ticker not in current_prices:
            continue
        
        cmp = current_prices[ticker]
        direction = str(row.get("Direction", "LONG"))
        entry = float(row["Entry_Price"])
        sl = float(row["SL"])
        target = float(row["Target"])
        
        exit_price = None
        exit_reason = None
        
        # Check max hold expiry
        entry_date = datetime.strptime(row["Date"], "%Y-%m-%d").replace(tzinfo=IST)
        days_held = (now - entry_date).days
        mh = row.get("MaxHold")
        trade_max_hold = int(mh) if pd.notna(mh) else MAX_HOLD_DAYS
        
        if days_held >= trade_max_hold:
            exit_price = cmp
            exit_reason = f"Expiry {days_held}d"
        elif direction == "LONG":
            if cmp <= sl:
                exit_price = sl
                exit_reason = "SL Hit"
            elif cmp >= target:
                exit_price = target
                exit_reason = "Target Hit"
        else:  # SHORT
            if cmp >= sl:
                exit_price = sl
                exit_reason = "SL Hit"
            elif cmp <= target:
                exit_price = target
                exit_reason = "Target Hit"
        
        if exit_price:
            # Gross P&L (before charges)
            if direction == "LONG":
                pnl = (exit_price - entry) * row["Qty"]
                pnl_pct = ((exit_price - entry) / entry) * 100
            else:  # SHORT
                pnl = (entry - exit_price) * row["Qty"]
                pnl_pct = ((entry - exit_price) / entry) * 100
            
            # Deduct trading charges per market (Round Turn cost)
            trade_mode = str(row.get("Mode", "US"))
            charge_rate = CHARGES_PER_MARKET.get(trade_mode, 0.001)
            notional = entry * row["Qty"]
            charges = round(notional * charge_rate, 2)
            pnl -= charges
            pnl_pct -= charge_rate * 100
            
            df.at[idx, "Exit_Price"] = round_price(exit_price)
            df.at[idx, "Exit_Time"] = time_str
            df.at[idx, "P&L"] = round(pnl, 2)
            df.at[idx, "P&L_%"] = round(pnl_pct, 2)
            df.at[idx, "Status"] = "CLOSED"
            df.at[idx, "Reason"] = str(row["Reason"]) + f" | {exit_reason}"
            
            # Log exit to persistent audit trail (AFTER values are set)
            _log_audit_exit({
                "Exit_Time": time_str,
                "Mode": row.get("Mode", ""),
                "Ticker": row["Ticker"],
                "Direction": row["Direction"],
                "Entry_Price": entry,
                "Exit_Price": round_price(exit_price),
                "Qty": row["Qty"],
                "P&L": round(pnl, 2),
                "P&L_%": round(pnl_pct, 2),
                "Pattern_Rank": row.get("Pattern_Rank", ""),
                "Expected_WinRate": row.get("Expected_WinRate", ""),
                "Pattern_Factors": row.get("Pattern_Factors", ""),
                "Reason": str(row["Reason"]) + f" | {exit_reason}",
            })
            
            # Update per-market capital
            trade_mode = str(row.get("Mode", "US"))
            mkt_cap = portfolio.setdefault("capital_by_market", dict(CAPITAL_BY_MARKET)).get(trade_mode, 100000)
            mkt_cap = max(0, mkt_cap + pnl)
            portfolio["capital_by_market"][trade_mode] = mkt_cap
            portfolio["total_pnl"] += pnl
            tpnl_by_mkt = portfolio.setdefault("total_pnl_by_market", {"INDIAN":0,"US":0,"CRYPTO":0})
            tpnl_by_mkt[trade_mode] = tpnl_by_mkt.get(trade_mode, 0) + pnl
            portfolio["closed_count"] += 1
            if pnl > 0:
                portfolio["total_wins"] += 1
            else:
                portfolio["total_losses"] += 1
            
            closed_msgs.append(
                f"{direction} {ticker} {exit_reason} "
                f"P&L Rs {pnl:+.0f} ({pnl_pct:+.1f}%)"
            )
            updated = True
            print(f"[Paper] EXIT {direction} {ticker} @ {round_price(exit_price)} | "
                  f"P&L {pnl:+.0f} | {exit_reason}")
    
    if updated:
        df.to_csv(PAPER_FILE, index=False)
        open_df = df[df["Status"] == "OPEN"]
        portfolio["open_positions"] = open_df.to_dict(orient="records")
        save_portfolio(portfolio)
        # Update per-strategy win rates — iterate once over all rows
        for idx, row in df.iterrows():
            if row["Status"] != "CLOSED":
                continue
            exit_price = row.get("Exit_Price", "")
            if pd.isna(exit_price) or str(exit_price).strip() == "":
                continue
            pnl = row.get("P&L", "")
            if pd.notna(pnl) and str(pnl).strip() != "":
                try:
                    pnl_f = float(pnl)
                    reason = str(row.get("Reason", ""))
                    update_strategy_stats(reason, pnl_f)
                except:
                    pass
    
    return closed_msgs

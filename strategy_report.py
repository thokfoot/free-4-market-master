"""
FREE 3-Market v5.10 — STRATEGY EXCEL REPORT GENERATOR
=====================================================
Builds logs/strategy_report.xlsx from live data:

  1. Summary  — every strategy (fired + never-fired), sorted by net P&L desc
  2. Per-strategy sheets — every trade with GROSS, CHARGES, NET P&L + subtotals
  3. Not Fired — strategies with 0 trades + why (from daily scan logs)

Auto-refresh: call generate_strategy_report() after each scan (bot.py main())
and after each live P&L check (live_pnl_updater.py main()).
"""

import glob
import json
import os
import zipfile as _zf

import openpyxl
from openpyxl.styles import Alignment, Border, Font, PatternFill
from openpyxl.utils import get_column_letter

import config

LOG_DIR = config.LOG_DIR if hasattr(config, "LOG_DIR") else os.path.join(os.path.dirname(__file__), "logs")
PAPER_FILE = os.path.join(LOG_DIR, "paper_trades.csv")
SWING_FILE = config.STRATEGY_FILE
INTRADAY_FILE = config.INTRADAY_STRATEGY_FILE
REPORT_FILE = os.path.join(LOG_DIR, "strategy_report.xlsx")

# ── Excel styles ──
HDR_FILL = PatternFill(fill_type="solid", fgColor="1F4E79")
HDR_FONT = Font(bold=True, color="FFFFFF")
SUB_FILL = PatternFill(fill_type="solid", fgColor="D9E2F3")
TOT_FILL = PatternFill(fill_type="solid", fgColor="FFE699")
GRN = PatternFill(fill_type="solid", fgColor="C6EFCE")
RED = PatternFill(fill_type="solid", fgColor="FFC7CE")
YEL = PatternFill(fill_type="solid", fgColor="FFF2CC")
GRY = PatternFill(fill_type="solid", fgColor="EDEDED")
NONE_FILL = PatternFill(fill_type="solid", fgColor="FFFFFF")
THIN = Border()
CENTER = Alignment(horizontal="center")
LEFT = Alignment(horizontal="left")
RIGHT = Alignment(horizontal="right")


def _resolve_ticker(csv_market: str) -> str:
    """Map CSV market name to yfinance ticker.

    Exact TICKER_MAP lookup first, then fuzzy substring match - the SAME
    logic the scanners use (scanner.get_yf_ticker). This resolves aliases
    like 'XLK_Tech' -> XLK and 'XLF_Fin' -> XLF that strategy_report used
    to leave unresolved (causing 'Unmatched Trades' in the report).
    """
    # Region placeholders are NOT tickers - pass through unchanged.
    # (Without this guard, 'INDIAN' would fuzzy-match 'DIA' inside it.)
    if csv_market.upper() in ("INDIAN", "CRYPTO", "US", "INDIA"):
        return csv_market
    if csv_market in config.TICKER_MAP:
        return config.TICKER_MAP[csv_market]
    for key in config.TICKER_MAP:
        if key.lower() in csv_market.lower():
            return config.TICKER_MAP[key]
    return csv_market


def _resolve_tf(file_default: str, csv_tf) -> str:
    """Map the CSV TF column to an internal TimeFrame.

    1m -> GAP_DOWN_1m, 1h -> INTRADAY_1h, 1d* -> SWING_1d.
    Falls back to the file default when the column is missing/unparseable.
    """
    tf = str(csv_tf or "").strip().lower()
    if tf.startswith("1m"):
        return "GAP_DOWN_1m"
    if tf.startswith("1h"):
        return "INTRADAY_1h"
    if tf.startswith("1d"):
        return "SWING_1d"
    return file_default


def _load_strategy_defs():
    """Return list of strategy defs from both CSV files.

    Each def: {tf, market, ticker, region, rank, direction, factors,
               backtest_wr, backtest_pnl, trades, cost_rt}
    """
    import csv as _csv

    defs = []
    for path, file_default in ((SWING_FILE, "SWING_1d"), (INTRADAY_FILE, "INTRADAY_1h")):
        if not os.path.exists(path):
            continue
        with open(path, encoding="utf-8-sig", newline="") as f:
            for row in _csv.DictReader(f):
                try:
                    rank = int(float(row["Final_Rank"]))
                except (KeyError, ValueError, TypeError):
                    continue
                market = str(row.get("Market", "")).strip()
                ticker = _resolve_ticker(market)
                tf = _resolve_tf(file_default, row.get("TF"))
                region = str(row.get("Region", "")).strip().upper()
                if region == "INDIA":
                    region = "INDIAN"
                defs.append({
                    "tf": tf,
                    "market": market,
                    "ticker": ticker,
                    "region": region,
                    "rank": rank,
                    "direction": str(row.get("Direction", "LONG")).strip().upper(),
                    "factors": str(row.get("Factors", "")).strip(),
                    "backtest_wr": _f(row.get("AvgWin%")),
                    "backtest_pnl": _f(row.get("Net_TotalPnL%_After_Charges") or row.get("Net_Total%_After_Charges")),
                    "trades": _f(row.get("Trades")),
                    "cost_rt": _f(row.get("Cost%_per_trade_RT") or row.get("Cost%_RT")),
                })
    return defs


def _f(v):
    """Parse a numeric cell safely -> float or 0.0."""
    try:
        if v is None:
            return 0.0
        s = str(v).strip().replace("%", "").replace(",", "")
        if not s or s in ("nan", "NaN", "-"):
            return 0.0
        return float(s)
    except (ValueError, TypeError):
        return 0.0


def _load_trades():
    """Load paper_trades.csv -> list of dicts."""
    import csv as _csv

    rows = []
    if not os.path.exists(PAPER_FILE):
        return rows
    with open(PAPER_FILE, encoding="utf-8-sig", newline="") as f:
        for r in _csv.DictReader(f):
            rows.append(r)
    return rows


def _charge_rate(mode: str) -> float:
    mode_norm = str(mode or "US").upper()
    if mode_norm == "INDIA":
        mode_norm = "INDIAN"
    return config.CHARGES_PER_MARKET.get(mode_norm, 0.001)


def _gross_net(trade):
    """Return (gross, charges, net) for a CLOSED trade.

    CSV P&L is NET (charges already deducted by paper_trader).
    Gross = Net + charges, where charges = entry*qty*rate (same as paper_trader).
    """
    entry = _f(trade.get("Entry_Price"))
    qty = _f(trade.get("Qty"))
    rate = _charge_rate(trade.get("Mode"))
    charges = round(entry * qty * rate, 2)
    net = _f(trade.get("P&L"))
    gross = round(net + charges, 2)
    return gross, charges, net


def _latest_close():
    """Latest known close per ticker from daily_scan logs."""
    closes = {}
    scans = sorted(glob.glob(os.path.join(LOG_DIR, "daily_scan_*.json")))
    for path in scans:
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for run in data.get("runs", []):
            mc = run.get("market_close") or {}
            for tk, px in mc.items():
                try:
                    closes[tk] = float(px)
                except (ValueError, TypeError):
                    pass
    return closes


def _scan_reasons():
    """Aggregate 'why' info from daily_scan logs.

    Returns:
        scanned_tickers: set of tickers ever scanned
        fired: set of (rank, ticker, direction) that fired in any scan
        skipped: list of {ticker, direction, rank, reason}
    """
    scanned = set()
    fired = set()
    skipped = []
    for path in sorted(glob.glob(os.path.join(LOG_DIR, "daily_scan_*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            continue
        for run in data.get("runs", []):
            scanned.update(run.get("tickers_scanned") or [])
            for fp in run.get("fired_patterns") or []:
                try:
                    r = int(float(fp.get("rank", 0)))
                except (ValueError, TypeError):
                    r = 0
                fired.add((r, fp.get("ticker"), str(fp.get("direction", "")).upper()))
            for se in run.get("skipped_entries") or []:
                try:
                    r = int(float(se.get("rank", 0)))
                except (ValueError, TypeError):
                    r = 0
                skipped.append({
                    "ticker": se.get("ticker"),
                    "direction": str(se.get("direction", "")).upper(),
                    "rank": r,
                    "reason": se.get("reason", "Skipped"),
                })
    return scanned, fired, skipped


def _strategy_key(defn):
    return (defn["tf"], defn["rank"], defn["ticker"], defn["direction"])


def _match_def(defs, tf, rank, ticker, direction, factors):
    """Match a trade to its strategy definition (rank repeats across markets).

    Prefer exact (tf, rank, ticker, direction, factors); fall back to rank+ticker.
    """
    exact = [d for d in defs if d["tf"] == tf and d["rank"] == rank
             and d["ticker"] == ticker and d["direction"] == direction
             and d["factors"] == factors]
    if len(exact) == 1:
        return exact[0]
    by_tk = [d for d in defs if d["tf"] == tf and d["rank"] == rank
             and d["ticker"] == ticker and d["direction"] == direction]
    if by_tk:
        return by_tk[0]
    # Gap-down strategies (997/998) have Market/Ticker = region placeholder
    # ('INDIAN') while trades carry real tickers (PFC.NS, ABFRL.NS...).
    # Match on (tf, rank, direction) so the 14 gap-down trades resolve.
    if rank in (997, 998):
        gd = [d for d in defs if d["tf"] == tf and d["rank"] == rank
              and d["direction"] == direction]
        if gd:
            return gd[0]
    return None


def _label(defn):
    suffix = "ID" if defn["tf"] == "INTRADAY_1h" else "SW"
    return f"#{defn['rank']}{suffix} {defn['ticker']} {defn['direction']}"


def _write_sheet(wb, title, header, rows, widths, center_cols=(), num_cols=()):
    ws = wb.create_sheet(title)
    for c, h in enumerate(header, 1):
        cell = ws.cell(row=1, column=c, value=h)
        cell.fill = HDR_FILL
        cell.font = HDR_FONT
        cell.alignment = CENTER
        cell.border = THIN
    for r, row in enumerate(rows, 2):
        for c, v in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=v)
            cell.border = THIN
            if c in center_cols:
                cell.alignment = CENTER
            elif c in num_cols:
                cell.alignment = RIGHT
            if c in num_cols and isinstance(v, (int, float)):
                cell.number_format = "#,##0.00"
    for c, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = f"A1:{get_column_letter(len(header))}{max(1, len(rows) + 1)}"
    return ws


def generate_strategy_report(report_file=None):
    """Regenerate logs/strategy_report.xlsx. Returns the output path."""
    out = report_file or REPORT_FILE

    defs = _load_strategy_defs()
    trades = _load_trades()
    closes = _latest_close()
    scanned, fired, skipped = _scan_reasons()

    # ── Group trades per strategy ──
    # strategy key = (tf, rank, ticker, direction) using the def if matched,
    # else derived from the trade row itself.
    groups = {}
    unmatched = 0
    for t in trades:
        tf = str(t.get("TimeFrame", "SWING_1d"))
        try:
            rank = int(float(t.get("Pattern_Rank", 0)))
        except (ValueError, TypeError):
            rank = 0
        ticker = str(t.get("Ticker", ""))
        direction = str(t.get("Direction", "LONG")).upper()
        factors = str(t.get("Pattern_Factors", "")).strip()
        defn = _match_def(defs, tf, rank, ticker, direction, factors)
        if defn is None:
            unmatched += 1
            defn = {
                "tf": tf, "rank": rank, "ticker": ticker, "region": str(t.get("Mode", "US")).upper(),
                "direction": direction, "factors": factors,
                "backtest_wr": _f(t.get("Expected_WinRate")), "backtest_pnl": 0.0,
                "trades": 0, "cost_rt": 0.0, "market": ticker,
            }
        key = (defn["tf"], defn["rank"], defn["ticker"], defn["direction"])
        groups.setdefault(key, {"defn": defn, "trades": []})["trades"].append(t)

    # ── Per-strategy aggregates ──
    summary_rows = []
    for key, grp in groups.items():
        d = grp["defn"]
        closed = [t for t in grp["trades"] if str(t.get("Status", "")).upper() == "CLOSED"]
        open_t = [t for t in grp["trades"] if str(t.get("Status", "")).upper() != "CLOSED"]
        gross_t = charges_t = net_t = 0.0
        wins = losses = 0
        for t in closed:
            g, c, n = _gross_net(t)
            gross_t += g
            charges_t += c
            net_t += n
            wins += 1 if n > 0 else 0
            losses += 1 if n < 0 else 0
        win_rate = round(wins / len(closed) * 100, 1) if closed else 0.0
        label = _label(d)
        summary_rows.append({
            "label": label, "defn": d,
            "total": len(grp["trades"]), "closed": len(closed), "open": len(open_t),
            "wins": wins, "losses": losses, "win_rate": win_rate,
            "gross": round(gross_t, 2), "charges": round(charges_t, 2),
            "net": round(net_t, 2),
            "avg_net": round(net_t / len(closed), 2) if closed else 0.0,
        })

    # ── Strategies never fired ──
    fired_keys = set()
    for d in defs:
        if (d["tf"], d["rank"], d["ticker"], d["direction"]) in groups:
            fired_keys.add((d["tf"], d["rank"], d["ticker"]))
    not_fired = []
    for d in defs:
        key = (d["tf"], d["rank"], d["ticker"], d["direction"])
        if key in groups:
            continue
        ticker = d["ticker"]
        rk = d["rank"]
        if ticker not in scanned:
            reason = "Ticker never scanned in any run"
        elif (rk, ticker, d["direction"]) in fired:
            sk = [s for s in skipped if s["ticker"] == ticker and s["rank"] == rk
                  and s["direction"] == d["direction"]]
            reason = sk[0]["reason"] if sk else "Fired in scan but never entered"
        else:
            reason = "Scanned but pattern never matched"
        not_fired.append({**d, "reason": reason})

    # ── Sort: fired by net desc, then never-fired ──
    fired_sum = [s for s in summary_rows if s["total"] > 0]
    fired_sum.sort(key=lambda s: (s["net"], s["total"]), reverse=True)
    unfired = [d for d in not_fired if d["rank"] not in (997, 998)]
    unfired.sort(key=lambda d: d["backtest_wr"], reverse=True)

    # ══════════════ BUILD WORKBOOK ══════════════
    wb = openpyxl.Workbook()
    wb.remove(wb.active)

    # ── Sheet 1: Summary ──
    header = ["#", "Strategy", "TF", "Market/Ticker", "Region", "Direction", "Factors",
              "Backtest WR%", "Backtest NetPnL%", "Trades", "Open", "Wins", "Losses",
              "WinRate%", "Gross P&L", "Charges", "Net P&L", "Avg Net/Trade"]
    rows = []
    rank_idx = 1
    for s in fired_sum:
        d = s["defn"]
        rows.append([rank_idx, s["label"], d["tf"], d["ticker"], d.get("region", ""),
                     d["direction"], d["factors"], d["backtest_wr"], d["backtest_pnl"],
                     s["total"], s["open"], s["wins"], s["losses"], s["win_rate"],
                     s["gross"], s["charges"], s["net"], s["avg_net"]])
        rank_idx += 1
    for d in unfired:
        rows.append([rank_idx, f"#{d['rank']}{'ID' if d['tf']=='INTRADAY_1h' else 'SW'} {d['ticker']} {d['direction']}",
                     d["tf"], d["ticker"], d.get("region", ""), d["direction"], d["factors"],
                     d["backtest_wr"], d["backtest_pnl"], 0, 0, 0, 0, 0.0, 0.0, 0.0, 0.0, 0.0])
        rank_idx += 1

    ws = _write_sheet(wb, "Summary", header, rows,
                      widths=[5, 26, 12, 13, 9, 9, 52, 10, 11, 8, 7, 7, 8, 9, 12, 11, 12, 12],
                      center_cols=(1, 3, 5, 6, 9, 10, 11, 12, 13, 14),
                      num_cols=(8, 15, 16, 17, 18))
    # Highlight fired profitable / losing
    for r in range(2, len(fired_sum) + 2):
        net_cell = ws.cell(row=r, column=17)
        net = net_cell.value or 0.0
        if net > 0:
            net_cell.fill = GRN
        elif net < 0:
            net_cell.fill = RED
    for r in range(len(fired_sum) + 2, len(rows) + 2):
        ws.cell(row=r, column=17).fill = GRY

    # ── Sheet 2: Per-strategy trade sheets ──
    for s in fired_sum:
        d = s["defn"]
        safe = f"#{d['rank']}{'ID' if d['tf']=='INTRADAY_1h' else 'SW'} {d['ticker']}"
        for ch in r'[]:*?/\\':
            safe = safe.replace(ch, "")
        safe = safe[:31]
        t_header = ["Date", "Time_IST", "Mode", "Ticker", "Direction", "Entry", "Qty",
                    "Exit", "Hold/Exit_Time", "Gross P&L", "Charges", "Net P&L",
                    "P&L%", "Status", "Reason"]
        t_rows = []
        gr_tot = ch_tot = net_tot = 0.0
        grp_trades = groups[(d["tf"], d["rank"], d["ticker"], d["direction"])]["trades"]
        for t in grp_trades:
            if str(t.get("Status", "")).upper() == "CLOSED":
                g, c, n = _gross_net(t)
                gr_tot += g
                ch_tot += c
                net_tot += n
                t_rows.append([t.get("Date"), t.get("Time_IST"), t.get("Mode"), t.get("Ticker"),
                               t.get("Direction"), _f(t.get("Entry_Price")), _f(t.get("Qty")),
                               _f(t.get("Exit_Price")), t.get("Exit_Time"), round(g, 2),
                               round(c, 2), round(n, 2), _f(t.get("P&L_%")), t.get("Status"),
                               t.get("Reason")])
            else:
                entry = _f(t.get("Entry_Price"))
                qty = _f(t.get("Qty"))
                rate = _charge_rate(t.get("Mode"))
                charges = round(entry * qty * rate, 2)
                cur = closes.get(t.get("Ticker"))
                if cur is not None:
                    gross = round((cur - entry) * qty, 2) if t.get("Direction") == "LONG" else round((entry - cur) * qty, 2)
                    net = round(gross - charges, 2)
                    pnl_pct = round(net / (entry * qty) * 100, 2) if entry * qty else 0.0
                    pnl_lbl = f"~{net:.2f} (unrealized)"
                else:
                    gross = net = pnl_pct = 0.0
                    pnl_lbl = "—"
                t_rows.append([t.get("Date"), t.get("Time_IST"), t.get("Mode"), t.get("Ticker"),
                               t.get("Direction"), entry, qty, None, t.get("Exit_Time"),
                               round(gross, 2), round(charges, 2), pnl_lbl,
                               round(pnl_pct, 2) if isinstance(pnl_pct, float) else pnl_pct,
                               t.get("Status"), t.get("Reason")])
        t_rows.append(["", "", "", "", "", "", "", "", "TOTAL", round(gr_tot, 2),
                       round(ch_tot, 2), round(net_tot, 2), "", "", ""])
        tws = _write_sheet(wb, safe, t_header, t_rows,
                           widths=[12, 12, 9, 11, 10, 10, 8, 10, 18, 11, 10, 12, 9, 9, 50],
                           center_cols=(3, 4, 5, 8, 14),
                           num_cols=(6, 7, 10, 11, 12))
        tot_row = len(t_rows) + 1
        for c in range(1, 16):
            tws.cell(row=tot_row, column=c).fill = TOT_FILL
            tws.cell(row=tot_row, column=c).font = Font(bold=True)
        # color net P&L column
        for r in range(2, tot_row):
            cell = tws.cell(row=r, column=12)
            v = cell.value
            if isinstance(v, (int, float)):
                cell.fill = GRN if v > 0 else (RED if v < 0 else NONE_FILL)
        if net_tot > 0:
            tws.cell(row=tot_row, column=12).fill = GRN
        elif net_tot < 0:
            tws.cell(row=tot_row, column=12).fill = RED

    # ── Sheet 3: Never fired + why ──
    if unfired:
        nf_header = ["Rank", "TF", "Market", "Ticker", "Region", "Direction", "Factors",
                     "Backtest WR%", "Backtest NetPnL%", "Backtest Trades", "Why Not Fired"]
        nf_rows = []
        for d in unfired:
            nf_rows.append([f"#{d['rank']}{'ID' if d['tf']=='INTRADAY_1h' else 'SW'}",
                            d["tf"], d["market"], d["ticker"], d.get("region", ""),
                            d["direction"], d["factors"], d["backtest_wr"],
                            d["backtest_pnl"], d["trades"], d["reason"]])
        _write_sheet(wb, "Never Fired", nf_header, nf_rows,
                     widths=[8, 12, 13, 11, 9, 9, 52, 10, 12, 10, 42],
                     center_cols=(1, 2, 5, 6, 8, 9, 10))
        # highlight gapdown ranks separately
        gd_rows = [d for d in not_fired if d["rank"] in (997, 998)]
        if gd_rows:
            gd_header = ["Rank", "TF", "Market", "Ticker", "Region", "Direction", "Factors",
                         "Backtest WR%", "Backtest NetPnL%", "Backtest Trades", "Why Not Fired"]
            gd_rows_out = []
            for d in gd_rows:
                gd_rows_out.append([f"#{d['rank']}", d["tf"], d["market"], d["ticker"],
                                    d.get("region", ""), d["direction"], d["factors"],
                                    d["backtest_wr"], d["backtest_pnl"], d["trades"], d["reason"]])
            _write_sheet(wb, "GapDown", gd_header, gd_rows_out,
                         widths=[8, 12, 13, 11, 9, 9, 52, 10, 12, 10, 42],
                         center_cols=(1, 2, 5, 6, 8, 9, 10))

    # ── Sheet 4: Portfolio meta ──
    if os.path.exists(PAPER_FILE):
        closed = [t for t in trades if str(t.get("Status", "")).upper() == "CLOSED"]
        gtot = ctot = ntot = 0.0
        for t in closed:
            g, c, n = _gross_net(t)
            gtot += g
            ctot += c
            ntot += n
        wins = sum(1 for t in closed if _f(t.get("P&L")) > 0)
        losses = sum(1 for t in closed if _f(t.get("P&L")) < 0)
        breakeven = len(closed) - wins - losses
        meta = wb.create_sheet("Portfolio")
        meta.cell(row=1, column=1, value="Metric").fill = HDR_FILL
        meta.cell(row=1, column=2, value="Value").fill = HDR_FILL
        meta.cell(row=1, column=1).font = HDR_FONT
        meta.cell(row=1, column=2).font = HDR_FONT
        meta_items = [
            ("Total Trades (incl. open)", len(trades)),
            ("Closed Trades", len(closed)),
            ("Open Positions", len(trades) - len(closed)),
            ("Wins", wins),
            ("Losses", losses),
            ("Breakeven (0 P&L)", breakeven),
            ("Win Rate %", round(wins / len(closed) * 100, 1) if closed else 0.0),
            ("Gross P&L (closed, pre-charges)", round(gtot, 2)),
            ("Total Charges", round(ctot, 2)),
            ("Net P&L (closed)", round(ntot, 2)),
            ("Strategies Fired", len(fired_sum)),
            # Summary sheet lists 43 fired + 247 never-fired (gap-down 997/998
            # live in their own GapDown sheet), so the meta count must use
            # `unfired` (247) - `not_fired` (249) double-counts the GapDown defs.
            ("Strategies Never Fired", len(unfired)),
            ("Unmatched Trades (no strategy row)", unmatched),
        ]
        for i, (k, v) in enumerate(meta_items, 2):
            meta.cell(row=i, column=1, value=k)
            meta.cell(row=i, column=2, value=v)
        meta.column_dimensions["A"].width = 34
        meta.column_dimensions["B"].width = 26

    # Deterministic metadata: pin created/modified to the latest trade date
    # so identical data yields byte-identical xlsx (no useless git commits).
    try:
        from datetime import datetime as _dt
        latest = "2026-01-01 00:00:00"
        for t in trades:
            dt = str(t.get("Date") or "").strip()
            if dt and dt > latest:
                latest = dt
        dt_obj = _dt.strptime(latest, "%Y-%m-%d")
        wb.properties.created = dt_obj
        wb.properties.modified = dt_obj
        _save_deterministic(wb, out, dt_obj)
    except (ValueError, TypeError):
        _save_deterministic(wb, out, None)
    print(f"[StrategyReport] Saved {out} ({len(fired_sum)} fired / {len(not_fired)} never-fired strategies)")
    return out


def _save_deterministic(wb, out, dt_obj):
    """Save workbook, then pin docProps/core.xml to a fixed timestamp.

    openpyxl stamps `modified` at save time, which makes byte output vary
    between runs. Rewrite core.xml with a deterministic timestamp instead.
    """
    import datetime as _dtm
    import re as _re

    wb.save(out)
    if dt_obj is None:
        return
    stamp = dt_obj.strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = out + ".det"
    dt_tuple = (dt_obj.year, dt_obj.month, dt_obj.day, dt_obj.hour, dt_obj.minute, dt_obj.second)
    with _zf.ZipFile(out, "r") as zin, _zf.ZipFile(tmp, "w", _zf.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            item.date_time = dt_tuple
            if item.filename == "docProps/core.xml":
                text = data.decode("utf-8")
                text = _re.sub(r"(<dcterms:created[^>]*>)[^<]*(</dcterms:created>)",
                               rf'\g<1>{stamp}\g<2>', text)
                text = _re.sub(r"(<dcterms:modified[^>]*>)[^<]*(</dcterms:modified>)",
                               rf'\g<1>{stamp}\g<2>', text)
                data = text.encode("utf-8")
            zout.writestr(item, data)
    os.replace(tmp, out)


if __name__ == "__main__":
    generate_strategy_report()

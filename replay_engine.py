"""
replay_engine.py - Data integrity backtester + outage catch-up replay
=======================================================================
Verifies every paper trade against REAL historical market data and
auto-corrects the log when reality disagrees with the record.

Commands:
  python replay_engine.py audit
      Daily integrity check. For every OPEN trade, fetches real historical
      bars and verifies whether SL / TP / MaxHold actually triggered. A trade
      that should have exited but is still marked OPEN is closed using the
      real historical price and timestamp. Rebuilds portfolio / stats /
      Excel report so the whole log is a pure function of real market data.

  python replay_engine.py catchup --from 2026-08-06T15:40:31Z --to 2026-08-07T00:09:23Z
      Replays a downtime window [from, to]:
        * Missed INTRAday entry scans are re-run at the scheduled scan times
          that fell inside the window (entries that never got recorded).
        * Missed exits / holds are closed using the real bar that stopped them
          out (SL / TP / MaxHold), with the real historical exit timestamp.
      Swing entries are NOT replayed: they are date-based, entered once per day
      by the morning scan, and no such scan fell inside typical gaps.

Every correction is written to logs/replay_report_<date>.json for auditability.

Author: Finance Manager
"""

import argparse
import json
import math
import os
import sys
from datetime import datetime, timedelta

import pandas as pd
import pytz
import yfinance as yf

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CHARGES_PER_MARKET, MAX_HOLD_DAYS, get_region
from paper_trader import (
    IST,
    _apply_slippage, _log_audit_exit, check_entry_allowed,
    enter_trade, load_portfolio, rebuild_portfolio_from_csv,
    round_price, update_strategy_stats, _session_live_until,
)
import paper_trader as pt
from scanner_intraday import (
    compute_indicators_1h, explain_signal_1h, get_best_intraday_entries,
    get_yf_ticker, load_intraday_strategies, unique_tickers,
)

# Bot schedule times (UTC) that must be replayed when they fall inside a gap.
# Matches .github/workflows/bot.yml. Weekday-only scans after the daily 01:00.
DAILY_SCAN_HOURS = ["01:00"]
WEEKDAY_SCAN_HOURS = ["09:45", "12:30", "13:30", "14:30", "15:30",
                      "16:30", "18:00", "19:30", "20:30"]

_TOLERANCE = 0.9999  # 0.01% noise guard (same as paper_trader update_trades)


# =====================================================================
# Data loading helpers
# =====================================================================

import market_data
def _load_trades() -> pd.DataFrame:
    """Load paper_trades.csv with string columns forced to object dtype."""
    if not os.path.exists(pt.PAPER_FILE):
        return pd.DataFrame()
    df = pd.read_csv(pt.PAPER_FILE, on_bad_lines="warn")
    str_cols = ["Exit_Price", "Exit_Time", "P&L", "P&L_%", "Status", "Reason",
                "Date", "Time_IST", "Mode", "Ticker", "Direction", "TimeFrame",
                "Pattern_Rank", "Expected_WinRate", "Pattern_Factors",
                "Signal_Indicators"]
    for col in str_cols:
        if col in df.columns:
            df[col] = df[col].astype(object)
    if "TimeFrame" not in df.columns:
        df["TimeFrame"] = "SWING_1d"
    return df


def _parse_entry_utc(row) -> pd.Timestamp:
    """Entry datetime (tz-aware UTC) from Date + Time_IST columns."""
    date_str = str(row.get("Date", ""))
    time_str = str(row.get("Time_IST", "")).replace("IST", "").strip()
    if date_str and len(time_str) >= 5:
        try:
            dt = datetime.strptime(f"{date_str} {time_str[:8]}", "%Y-%m-%d %H:%M:%S")
            return pd.Timestamp(dt, tz="Asia/Kolkata").tz_convert("UTC")
        except Exception:
            pass
    try:
        return pd.Timestamp(date_str, tz="Asia/Kolkata").tz_convert("UTC")
    except Exception:
        return pd.Timestamp(date_str)


def _fetch_bars(ticker: str, start_utc, end_utc, interval: str) -> list:
    """Historical bars [(utc_ts, high, low, close)] sorted ascending.

    yfinance bar times are BAR-START timestamps; we add the interval so the
    returned ts is the candle-END / completion time (matches _signal_candle_index
    and the bot's completed-candle semantics).
    """
    try:
        buf_start = (pd.Timestamp(start_utc) - pd.Timedelta(days=10)).strftime("%Y-%m-%d")
        buf_end = (pd.Timestamp(end_utc) + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        df = market_data.download(ticker, interval=interval,
                                 start=buf_start, end=buf_end)
        if df is None or len(df) == 0:
            return []
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)
        df = df.dropna(subset=["Close"])
        if len(df) == 0:
            return []
        idx = df.index
        if idx.tz is None:
            idx = idx.tz_localize("UTC")
        else:
            idx = idx.tz_convert("UTC")
        delta = pd.Timedelta(hours=1) if interval in ("1h", "60m") else pd.Timedelta(days=1)
        out = []
        for ts, hi, lo, cl in zip(idx, df["High"], df["Low"], df["Close"]):
            out.append((ts + delta, float(hi), float(lo), float(cl)))
        return sorted(out, key=lambda b: b[0])
    except Exception as e:
        print(f"  [replay] fetch error {ticker} {interval}: {e}")
        return []


def _bar_interval(tf: str) -> str:
    if tf in ("SWING_1d", "IPO_1d"):
        return "1d"
    return "1h"


# =====================================================================
# Exit integrity (bar-level first-touch, mirrors _bars_sl_tp + MaxHold)
# =====================================================================

def _find_exit(row, bars, cutoff_utc):
    """Return (exit_price, exit_reason, exit_utc) if an exit triggers by cutoff.

    Mirrors paper_trader semantics: SL/TP first-touch over post-entry bars,
    MaxHold checked first (time-based), session-minutes budget for US intraday.
    """
    tf = str(row.get("TimeFrame", "SWING_1d"))
    direction = str(row.get("Direction", "LONG"))
    entry = float(row["Entry_Price"])
    sl = float(row["SL"])
    target = float(row["Target"])
    mode = str(row.get("Mode", "US")).upper()
    if mode == "INDIA":
        mode = "INDIAN"
    try:
        mh = row.get("MaxHold")
        max_hold = int(mh) if pd.notna(mh) else MAX_HOLD_DAYS
    except (TypeError, ValueError):
        max_hold = MAX_HOLD_DAYS
    entry_utc = _parse_entry_utc(row)
    if entry_utc is None:
        return None

    cutoff = pd.Timestamp(cutoff_utc)
    if cutoff.tzinfo is None:
        cutoff = cutoff.tz_localize("UTC")
    # OHLC validity guard (mirrors update_trades): never exit on corrupt bars
    # (yfinance occasionally returns zero high/low on partial/empty days).
    bars = [b for b in bars if b[1] > 0 and b[2] > 0 and b[3] > 0]
    post = [b for b in bars if entry_utc <= b[0] <= cutoff]

    if tf in ("SWING_1d", "IPO_1d"):
        et = pytz.timezone("America/New_York")
        entry_session = entry_utc.astimezone(et).date()
        for ts, hi, lo, _cl in post:
            if pd.Timestamp(ts.date()) <= pd.Timestamp(entry_session):
                continue
            if direction == "LONG":
                if lo <= sl * _TOLERANCE:
                    return (sl, "SL Hit (intraday)", ts)
                if hi >= target / _TOLERANCE:
                    return (target, "Target Hit", ts)
            else:
                if hi >= sl / _TOLERANCE:
                    return (sl, "SL Hit (intraday)", ts)
                if lo <= target * _TOLERANCE:
                    return (target, "Target Hit", ts)
        if max_hold and max_hold > 0:
            exp = entry_utc + pd.Timedelta(days=max_hold)
            if exp <= cutoff:
                within = [b for b in post if b[0] <= exp]
                if within:
                    return (within[-1][3], f"Expiry {max_hold}d", within[-1][0])
        return None

    # INTRADAY_1h (and any other intraday tf): session-minutes budget for US
    if mode == "US":
        try:
            lu = _session_live_until(entry_utc, max_hold)
        except Exception:
            lu = entry_utc + pd.Timedelta(hours=max_hold)
    else:
        lu = entry_utc + pd.Timedelta(hours=max_hold)

    for ts, hi, lo, _cl in post:
        if ts >= lu:
            break
        if direction == "LONG":
            if lo <= sl * _TOLERANCE:
                return (sl, "SL Hit (intraday)", ts)
            if hi >= target / _TOLERANCE:
                return (target, "Target Hit", ts)
        else:
            if hi >= sl / _TOLERANCE:
                return (sl, "SL Hit (intraday)", ts)
            if lo <= target * _TOLERANCE:
                return (target, "Target Hit", ts)

    if lu <= cutoff:
        within = [b for b in post if b[0] <= lu]
        if within:
            return (within[-1][3], "Expiry", within[-1][0])
    return None


def _close_trade(df, idx, exit_price, exit_reason, exit_utc):
    """Close one OPEN row exactly like update_trades (minus Telegram)."""
    # Force str columns to object dtype so float fills are assignable (pandas 3)
    for col in ["Exit_Price", "Exit_Time", "P&L", "P&L_%", "Status", "Reason"]:
        if col in df.columns:
            df[col] = df[col].astype(object)
    row = df.loc[idx]
    direction = str(row["Direction"])
    entry = float(row["Entry_Price"])
    qty = row["Qty"]
    mode = str(row.get("Mode", "US"))
    tf = str(row.get("TimeFrame", "SWING_1d"))

    exit_price = _apply_slippage(exit_price, direction, "EXIT", mode, tf)

    if direction == "LONG":
        pnl = (exit_price - entry) * qty
        pnl_pct = ((exit_price - entry) / entry) * 100
    else:
        pnl = (entry - exit_price) * qty
        pnl_pct = ((entry - exit_price) / entry) * 100

    mode_norm = mode.upper()
    if mode_norm == "INDIA":
        mode_norm = "INDIAN"
    charge_rate = CHARGES_PER_MARKET.get(mode_norm, 0.001)
    notional = entry * qty
    pnl -= round(notional * charge_rate, 2)
    pnl_pct -= charge_rate * 100

    if not math.isfinite(pnl):
        pnl, pnl_pct = 0.0, 0.0

    exit_ist = exit_utc.tz_convert("Asia/Kolkata") if getattr(exit_utc, "tzinfo", None) else exit_utc
    exit_dt_str = exit_ist.strftime("%Y-%m-%d %H:%M:%S IST")

    df.at[idx, "Exit_Price"] = round_price(exit_price)
    df.at[idx, "Exit_Time"] = exit_dt_str
    df.at[idx, "P&L"] = round(pnl, 2)
    df.at[idx, "P&L_%"] = round(pnl_pct, 2)
    df.at[idx, "Status"] = "CLOSED"
    full_reason = f"{str(row['Reason'])} | {exit_reason}"
    df.at[idx, "Reason"] = full_reason

    _log_audit_exit({
        "Exit_Time": exit_dt_str,
        "Entry_Time": f"{row['Date']} {row.get('Time_IST', '')}",
        "Hold": f"{(exit_utc - _parse_entry_utc(row)).total_seconds() / 3600:.1f}h",
        "Mode": mode,
        "Ticker": row["Ticker"],
        "Direction": direction,
        "Entry_Price": entry,
        "Exit_Price": round_price(exit_price),
        "Qty": qty,
        "P&L": round(pnl, 2),
        "P&L_%": round(pnl_pct, 2),
        "Pattern_Rank": row.get("Pattern_Rank", ""),
        "Expected_WinRate": row.get("Expected_WinRate", ""),
        "Pattern_Factors": row.get("Pattern_Factors", ""),
        "Reason": full_reason,
        "Signal_Indicators": row.get("Signal_Indicators", ""),
    })
    update_strategy_stats(full_reason, round(pnl, 2))
    return {"ticker": str(row["Ticker"]), "direction": direction, "tf": tf,
            "entry": entry, "sl": float(row["SL"]), "target": float(row["Target"]),
            "exit_price": round_price(exit_price), "exit_reason": exit_reason,
            "exit_utc": str(exit_utc), "pnl": round(pnl, 2)}


def replay_exits(cutoff_utc, fetch=True, df=None) -> list:
    """Close every OPEN trade whose SL/TP/MaxHold triggered by cutoff_utc.

    Returns list of closed-trade summaries.
    """
    if df is None:
        df = _load_trades()
    cutoff = pd.Timestamp(cutoff_utc)
    closed = []
    for idx, row in df.iterrows():
        if str(row.get("Status", "")) != "OPEN":
            continue
        tf = str(row.get("TimeFrame", "SWING_1d"))
        entry_utc = _parse_entry_utc(row)
        if entry_utc is None:
            continue
        if fetch:
            bars = _fetch_bars(str(row["Ticker"]), entry_utc, cutoff, _bar_interval(tf))
        else:
            bars = []
        hit = _find_exit(row, bars, cutoff)
        if hit:
            ep, reason, ets = hit
            closed.append(_close_trade(df, idx, ep, reason, ets))
            print(f"  [replay] CLOSE {row['Direction']} {row['Ticker']} {reason} "
                  f"@{round_price(ep)} at {ets}")
    if closed:
        df.to_csv(pt.PAPER_FILE, index=False)
        rebuild_portfolio_from_csv()
    return closed


# =====================================================================
# Entry replay (intraday scan pinned to a historical scan time)
# =====================================================================

def _snapshot_indicators(df: pd.DataFrame) -> dict:
    """Capture indicator values at the signal candle (mirrors scanner)."""
    last = df.iloc[-1]
    inds = {"Close", "Open", "High", "Low", "Volume",
            "SMA20", "SMA50", "EMA9", "EMA20", "EMA50",
            "RSI14", "Ret", "2Red"}
    snap = {}
    for col in inds:
        if col in last.index and pd.notna(last[col]):
            v = last[col]
            if hasattr(v, "iloc"):
                try:
                    v = float(v.iloc[0])
                except Exception:
                    continue
            snap[col] = round(float(v), 6)
    return snap


def _scan_intraday_at(strategies, scan_utc, tickers=None, fetch=True):
    """Replay the intraday scan exactly as of scan_utc (completed candles only)."""
    if tickers is None:
        tickers = unique_tickers(strategies)
    scan = pd.Timestamp(scan_utc)
    ticker_data = {}
    errors = 0
    if fetch:
        start = (scan - pd.Timedelta(days=100)).strftime("%Y-%m-%d")
        end = (scan + pd.Timedelta(days=2)).strftime("%Y-%m-%d")
        for yf_ticker in tickers:
            try:
                df = market_data.download(yf_ticker, interval="1h",
                                         start=start, end=end)
                if df is None or len(df) == 0:
                    continue
                if isinstance(df.columns, pd.MultiIndex):
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna(subset=["Close"])
                if len(df) < 200:
                    continue
                df = compute_indicators_1h(df)
                if df is None or len(df) < 200:
                    continue
                idx = df.index
                if idx.tz is None:
                    idx = idx.tz_localize("UTC")
                else:
                    idx = idx.tz_convert("UTC")
                completed = idx + pd.Timedelta(hours=1) <= scan
                sliced = df[completed]
                if len(sliced) < 200:
                    continue
                ticker_data[yf_ticker] = sliced
            except Exception as e:
                errors += 1
                print(f"  [replay] scan fetch error {yf_ticker}: {e}")
    print(f"  [replay] scan@{scan}: data {len(ticker_data)}/{len(tickers)} "
          f"({errors} errors)")

    signals = []
    for _, strat in strategies.iterrows():
        rank = int(strat["Final_Rank"])
        market = str(strat["Market"])
        region = str(strat["Region"])
        factors = str(strat["Factors"])
        direction = str(strat["Direction"])
        win_rate = float(strat["AvgWin%"])
        trades_count = int(strat["Trades"])
        yf_ticker = get_yf_ticker(market)
        if not yf_ticker or yf_ticker not in ticker_data:
            signals.append({"rank": rank, "market": market, "ticker": yf_ticker or market,
                            "direction": direction, "factors": factors, "win_rate": win_rate,
                            "trades_count": trades_count, "region": region, "close": 0,
                            "fired": False, "reason": "No data"})
            continue
        df = ticker_data[yf_ticker]
        close_price = float(df.iloc[-1]["Close"])
        fired, reason = explain_signal_1h(df, factors, direction, row_idx=-1)
        signals.append({"rank": rank, "market": market, "ticker": yf_ticker,
                        "direction": direction, "factors": factors, "win_rate": win_rate,
                        "trades_count": trades_count, "region": region, "close": close_price,
                        "fired": fired, "reason": reason,
                        "signal_indicators": _snapshot_indicators(df) if fired else None})
    fired_n = sum(1 for s in signals if s["fired"])
    best = get_best_intraday_entries(signals)
    print(f"  [replay] scan@{scan}: strategies={len(signals)} fired={fired_n} "
          f"best={len(best)}")
    return best, ticker_data


def _already_entered(ticker: str, direction: str, df: pd.DataFrame,
                     scan_utc, window_start_utc, replayed_keys) -> bool:
    """Dedupe: skip ticker/direction already open, or already replay-entered."""
    key = (ticker, direction)
    if key in replayed_keys:
        return True
    if check_entry_allowed(ticker, direction):
        return True  # open position exists
    if df is not None and len(df) > 0:
        mask = (df["Ticker"].astype(str) == ticker) & \
               (df["Direction"].astype(str) == direction)
        for _, r in df[mask].iterrows():
            try:
                et = _parse_entry_utc(r)
            except Exception:
                continue
            if et is None:
                continue
            # any entry for this ticker/direction recorded inside the replay
            # window up to this scan already covers the missed signal
            if pd.Timestamp(window_start_utc) - pd.Timedelta(hours=1) <= et <= scan_utc:
                return True
    return False


# =====================================================================
# Entry-price integrity (audit mode)
# =====================================================================

def _verify_entry_prices(df, cutoff_utc, report) -> None:
    """Flag (and auto-fix) entries recorded at prices the market never traded."""
    for idx, row in df.iterrows():
        ticker = str(row["Ticker"])
        entry = float(row["Entry_Price"])
        direction = str(row.get("Direction", "LONG"))
        tf = str(row.get("TimeFrame", "SWING_1d"))
        entry_utc = _parse_entry_utc(row)
        if entry_utc is None:
            continue
        bars = _fetch_bars(ticker, entry_utc - pd.Timedelta(days=1),
                           entry_utc + pd.Timedelta(hours=6),
                           _bar_interval(tf))
        if not bars:
            continue
        bars = [b for b in bars if b[1] > 0 and b[2] > 0 and b[3] > 0]
        if not bars:
            continue
        # the entry candle is the first post-entry bar (bar-end >= entry_utc)
        entry_bars = [b for b in bars if b[0] >= entry_utc]
        if not entry_bars:
            entry_bars = bars[-1:]
        lo = min(b[2] for b in entry_bars)
        hi = max(b[1] for b in entry_bars)
        achievable_lo = lo * 0.98
        achievable_hi = hi * 1.02
        if direction == "LONG" and entry < achievable_lo:
            fix = round_price(lo)
            report["entry_anomalies"].append(
                {"ticker": ticker, "direction": direction, "entry": entry,
                 "entry_bar_low": lo, "entry_bar_high": hi, "fixed_to": fix,
                 "issue": "LONG entry below achievable low"})
            df.at[idx, "Entry_Price"] = fix
        elif direction == "SHORT" and entry > achievable_hi:
            fix = round_price(hi)
            report["entry_anomalies"].append(
                {"ticker": ticker, "direction": direction, "entry": entry,
                 "entry_bar_low": lo, "entry_bar_high": hi, "fixed_to": fix,
                 "issue": "SHORT entry above achievable high"})
            df.at[idx, "Entry_Price"] = fix


# =====================================================================
# Entry scans inside a gap (scheduled times)
# =====================================================================

def _missed_scan_times(start_utc, end_utc):
    times = []
    d = pd.Timestamp(start_utc).date()
    last = pd.Timestamp(end_utc).date()
    while d <= last:
        t0 = pd.Timestamp(d).tz_localize("UTC")
        hours = DAILY_SCAN_HOURS + (WEEKDAY_SCAN_HOURS if d.weekday() < 5 else [])
        for hhmm in hours:
            hh, mm = map(int, hhmm.split(":"))
            t = t0 + pd.Timedelta(hours=hh, minutes=mm)
            if pd.Timestamp(start_utc) <= t <= pd.Timestamp(end_utc):
                times.append(t)
        d += timedelta(days=1)
    return times


# =====================================================================
# Commands
# =====================================================================

def run_audit():
    """Daily integrity backtester: verify + auto-fix exits and entries."""
    now_utc = pd.Timestamp.now(tz="UTC")
    report = {
        "generated": str(now_utc),
        "mode": "audit",
        "window": {"from": "earliest", "to": str(now_utc)},
        "exits_closed": [],
        "entry_anomalies": [],
        "scans_replayed": [],
        "errors": [],
    }
    print(f"\n=== BACKTEST AUDIT - {now_utc} ===")
    df = _load_trades()
    if len(df) == 0:
        print("No paper_trades.csv - nothing to audit.")
        return report

    # 1. Entry-price integrity (auto-fix impossible fills)
    _verify_entry_prices(df, now_utc, report)
    if report["entry_anomalies"]:
        df.to_csv(pt.PAPER_FILE, index=False)
        rebuild_portfolio_from_csv()

    # 2. Exit integrity - close any OPEN trade that reality stopped out
    closed = replay_exits(now_utc, fetch=True, df=_load_trades())
    report["exits_closed"] = closed

    # 3. Rebuild report artifacts
    _finalize(report)
    return report


def run_catchup(start_utc, end_utc, fetch=True):
    """Replay a downtime window: missed entries + missed exits."""
    start = pd.Timestamp(start_utc)
    end = pd.Timestamp(end_utc)
    if end <= start:
        raise ValueError("--to must be after --from")
    report = {
        "generated": str(pd.Timestamp.now(tz="UTC")),
        "mode": "catchup",
        "window": {"from": str(start), "to": str(end)},
        "exits_closed": [],
        "entry_anomalies": [],
        "scans_replayed": [],
        "errors": [],
    }
    print(f"\n=== CATCH-UP REPLAY [{start} -> {end}] ===")

    # 1. Missed intraday entry scans inside the gap
    strategies = load_intraday_strategies()
    df = _load_trades()
    scan_times = _missed_scan_times(start, end)
    print(f"Missed scan times: {[str(t) for t in scan_times]}")
    replayed_keys = set()
    for scan_t in scan_times:
        best, _ = _scan_intraday_at(strategies, scan_t, fetch=fetch)
        report["scans_replayed"].append({"time": str(scan_t),
                                         "fired": len(best)})
        for e in best:
            ticker, direction = e["ticker"], e["direction"]
            if _already_entered(ticker, direction, df, scan_t, start, replayed_keys):
                print(f"  [replay] SKIP {direction} {ticker} @{scan_t} (dup/open)")
                continue
            region = get_region(ticker, e.get("region"))
            trade = enter_trade(
                mode=region, ticker=ticker, direction=direction,
                entry_price=e["close"], reason=str(e.get("factors", ""))[:60],
                pattern_rank=e.get("rank"), expected_win_rate=e.get("win_rate"),
                pattern_factors=e.get("factors", ""), tf="INTRADAY_1h",
                signal_indicators=e.get("signal_indicators"),
                entry_dt=scan_t,
            )
            if trade:
                replayed_keys.add((ticker, direction))
                report["scans_replayed"][-1].setdefault("entered", []).append(
                    {"ticker": ticker, "direction": direction,
                     "entry": trade["Entry_Price"], "sl": trade["SL"],
                     "target": trade["Target"], "rank": e.get("rank"),
                     "win_rate": e.get("win_rate")})
                print(f"  [replay] ENTER {direction} {ticker} @ {trade['Entry_Price']}")
        # exit sweep up to this scan time (catches exits of just-entered trades)
        replay_exits(scan_t, fetch=fetch)
        df = _load_trades()

    # 2. Final exit sweep across the whole gap
    final_closed = replay_exits(end, fetch=fetch)
    report["exits_closed"] = final_closed

    _finalize(report)
    return report


def _finalize(report) -> None:
    """Rebuild strategy report / portfolio snapshots and write the report JSON."""
    try:
        from strategy_report import generate_strategy_report
        generate_strategy_report()
        print("  [replay] strategy_report.xlsx regenerated")
    except Exception as e:
        print(f"  [replay] report regen failed (non-fatal): {e}")
        report["errors"].append(f"report regen: {e}")
    os.makedirs(pt.LOG_DIR, exist_ok=True)
    report_file = os.path.join(pt.LOG_DIR,
        f"replay_report_{datetime.now(IST).strftime('%Y-%m-%d_%H%M%S')}.json")
    with open(report_file, "w") as f:
        json.dump(report, f, indent=2, default=str)
    print(f"\n[replay] report written: {report_file}")
    print(f"  exits closed: {len(report['exits_closed'])}")
    print(f"  entry anomalies: {len(report['entry_anomalies'])}")
    print(f"  scans replayed: {len(report['scans_replayed'])}")


def main():
    ap = argparse.ArgumentParser(description="Paper-trade data integrity backtester")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("audit", help="daily integrity check + auto-fix")
    p_c = sub.add_parser("catchup", help="replay a downtime window")
    p_c.add_argument("--from", dest="from_utc", required=True,
                     help="window start (ISO UTC, e.g. 2026-08-06T15:40:31Z)")
    p_c.add_argument("--to", dest="to_utc", required=True,
                     help="window end (ISO UTC)")
    p_c.add_argument("--no-fetch", action="store_true",
                     help="skip yfinance downloads (dry-run over recorded data)")
    args = ap.parse_args()

    if args.cmd == "audit":
        run_audit()
    else:
        run_catchup(args.from_utc, args.to_utc, fetch=not args.no_fetch)


if __name__ == "__main__":
    main()

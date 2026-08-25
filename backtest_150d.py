"""Deployed-strategy backtest (last N days) vs LIVE paper-trade results.

Uses the repo's own bot-identical execution engine:
  - data/indicators/factors: strategy_miner.load_data + scanner.compute_indicators(_1h) + strategy_miner.factor_series
  - fills/exits:             strategy_miner.backtest_one (SL/TP intra-hold, max-hold close, same as paper_trader)

Scope: combo strategies (strategies.csv SWING_1d + intraday_strategies.csv INTRADAY_1h).
Rule-based variants (FADE #90x, IPO #93x, GAP_DOWN #99x, LONG_BOUNCE) are listed
live-only: yfinance cannot serve 15m/5m bars beyond 60 days, so a >60d replay of
those rules is impossible without bar archives.
"""
import os, sys, io, warnings
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
from datetime import datetime, timedelta, timezone
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import CHARGES_PER_MARKET, get_region
from scanner import compute_indicators, get_yf_ticker
from scanner_intraday import compute_indicators_1h
import strategy_miner as sm

DAYS = int(sys.argv[1]) if len(sys.argv) > 1 else 150   # backtest window
RECENT = 30                                             # live-window mirror
HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_CSV = os.path.join(HERE, "logs", "paper_trades.csv")
OUT_CSV = os.path.join(HERE, "logs", "backtest150_vs_live.csv")

REGION_NORM = {"India": "INDIAN", "INDIAN": "INDIAN", "US": "US", "Crypto": "CRYPTO", "CRYPTO": "CRYPTO"}


def load_strategies():
    rows = []
    for path, tf, label in (("data/strategies.csv", "SWING_1d", "SW"),
                            ("data/intraday_strategies.csv", "INTRADAY_1h", "ID")):
        df = pd.read_csv(os.path.join(HERE, path), on_bad_lines="warn")
        for _, r in df.iterrows():
            rows.append({
                "rank": int(r["Final_Rank"]), "market": str(r["Market"]),
                "region": REGION_NORM.get(str(r["Region"]), get_region(get_yf_ticker(str(r["Market"])))),
                "tf": tf, "label": label,
                "direction": str(r["Direction"]).upper(),
                "factors": [f.strip() for f in str(r["Factors"]).split("+")],
            })
    return rows


_prepared = {}


def get_prepared(yft, tf):
    key = (yft, tf)
    if key in _prepared:
        return _prepared[key]
    interval = "1h" if tf == "INTRADAY_1h" else "1d"
    ind_fn = compute_indicators_1h if tf == "INTRADAY_1h" else compute_indicators
    min_rows = 200 if tf == "INTRADAY_1h" else 60
    try:
        df = sm.load_data(yft, interval)
        if df is None or len(df) < min_rows:
            import market_data
            period = "730d" if interval == "1h" else "1y"
            df = market_data.download(yft, interval=interval, period=period)
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.get_level_values(0)
        if df is None or len(df) < min_rows:
            _prepared[key] = None
            return None
        df = ind_fn(df)
        if df is None or len(df) < min_rows:
            _prepared[key] = None
            return None
        _prepared[key] = df
        return df
    except Exception as e:
        print(f"[BT] prep fail {yft}: {e}")
        _prepared[key] = None
        return None


def facs_for(df, factors):
    out = {}
    for f in set(factors):
        s = sm.factor_series(df, f)
        if s is None:
            return None
        out[f] = s
    return out


def main():
    t0 = datetime.now(timezone.utc)
    strats = load_strategies()
    print(f"[BT] deployed combo strategies: {len(strats)} "
          f"(swing={sum(1 for s in strats if s['tf']=='SWING_1d')}, "
          f"intraday={sum(1 for s in strats if s['tf']=='INTRADAY_1h')})")

    # ── precompute per (yft,tf) once, parallel downloads ──
    need = sorted({(get_yf_ticker(s["market"]), s["tf"]) for s in strats})
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {ex.submit(get_prepared, y, t): (y, t) for y, t in need}
        done = 0
        for f in as_completed(futs):
            f.result(); done += 1
            if done % 10 == 0:
                print(f"[BT] data {done}/{len(need)}")

    cutoff150 = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=DAYS)
    cutoff30 = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=RECENT)

    bt_rows = []
    for i, s in enumerate(strats):
        yft = get_yf_ticker(s["market"])
        df = get_prepared(yft, s["tf"])
        region = s["region"]
        cost_rt = CHARGES_PER_MARKET.get(region, 0.001)
        rec = {"rank": s["rank"], "market": s["market"], "region": region,
               "tf": s["tf"], "direction": s["direction"],
               "factors": "+".join(s["factors"])}
        if df is None:
            rec.update(bt_n=0, note="no-data")
            bt_rows.append(rec); continue
        facs = facs_for(df, s["factors"])
        if facs is None:
            rec.update(bt_n=0, note="bad-factor")
            bt_rows.append(rec); continue
        trades = sm.backtest_one(df, s["factors"], facs, s["direction"], s["tf"], region) or []

        def _utc(ts):
            return ts.tz_localize("UTC") if getattr(ts, "tzinfo", None) is None else ts.tz_convert("UTC")
        w150 = [t for t in trades if _utc(t[0]) >= cutoff150]
        w30 = [t for t in trades if _utc(t[0]) >= cutoff30]

        def stat(sub):
            n = len(sub)
            if n == 0:
                return 0, 0.0, 0.0, 0.0
            gross = sum(p for _, p in sub)
            net = gross - cost_rt * n
            wr = sum(1 for _, p in sub if p > 0) / n * 100
            return n, round(wr, 1), round(net * 100, 2), round(net / n * 100, 3)

        n, wr, net, apt = stat(w150)
        n3, wr3, net3, apt3 = stat(w30)
        rec.update(bt_n=n, bt_wr=wr, bt_net_pct=net, bt_avg_per_trade_pct=apt,
                   bt_recent_n=n3, bt_recent_wr=wr3, bt_recent_net_pct=net3,
                   bt_last_trade=str(w150[-1][0].date()) if w150 else "")
        bt_rows.append(rec)
        if (i + 1) % 25 == 0:
            print(f"[BT] {i+1}/{len(strats)} strategies backtested")

    bt = pd.DataFrame(bt_rows)

    # ── LIVE side ──
    live = pd.read_csv(LIVE_CSV, on_bad_lines="warn")
    lv = live[live["Pattern_Rank"].notna()].copy()
    lv["rank"] = pd.to_numeric(lv["Pattern_Rank"], errors="coerce").astype("Int64")
    closed = lv[lv["Status"].astype(str) == "CLOSED"].copy()
    closed["pnl"] = pd.to_numeric(closed["P&L"], errors="coerce").fillna(0)
    grp = closed.groupby(["Ticker", "rank", "TimeFrame"]).agg(
        live_n=("pnl", "size"), live_wins=("pnl", lambda x: int((x > 0).sum())),
        live_losses=("pnl", lambda x: int((x < 0).sum())), live_pnl=("pnl", "sum")).reset_index()

    # mode ↔ region mapping for join
    bt["mode_key"] = bt["region"].map({"INDIAN": "INDIAN", "US": "US", "CRYPTO": "CRYPTO"})
    bt["yft"] = bt["market"].map(get_yf_ticker)
    m = bt.merge(grp, left_on=["yft", "rank", "tf"],
                 right_on=["Ticker", "rank", "TimeFrame"], how="outer",
                 suffixes=("", "_live"), indicator=True)
    opk = lv[lv["Status"].astype(str) == "OPEN"].groupby(["Ticker", "rank", "TimeFrame"]).size().rename("live_open").reset_index()
    m = m.merge(opk, left_on=["yft", "rank", "tf"], right_on=["Ticker", "rank", "TimeFrame"],
                how="left", suffixes=("", "_op"))
    m = m[m["_merge"] != "right_only"].copy()  # keep strategies even if never traded live
    for c in ("live_open_x", "live_open_y"):
        if c in m.columns and "live_open" not in m.columns:
            m = m.rename(columns={c: "live_open"})
        elif c in m.columns:
            m = m.drop(columns=[c])
    if "live_open" not in m.columns:
        m["live_open"] = 0
    m["live_open"] = pd.to_numeric(m["live_open"], errors="coerce").fillna(0).astype(int)

    def verdict(r):
        lp = float(r.get("live_pnl", 0) or 0)
        ln = int(pd.notna(r.get("live_n")) and r["live_n"] or 0)
        bn = int(pd.notna(r.get("bt_n")) and r["bt_n"] or 0)
        bnet = float(r.get("bt_net_pct", 0) or 0)
        if ln == 0:
            return "NOT-TRADED-LIVE"
        if bn == 0:
            return "LIVE-ONLY(no-bt-data)"
        if lp > 0 and bnet > 0:
            return "CONSISTENT-PROFIT ✔"
        if lp <= 0 and bnet <= 0:
            return "CONSISTENT-LOSS ✘"
        if lp > 0 and bnet <= 0:
            return "LIVE-PROFIT-BT-LOSS ⚠"
        return "LIVE-LOSS-BT-PROFIT ~"

    m["verdict"] = m.apply(verdict, axis=1)
    cols = ["rank", "market", "region", "tf", "direction", "factors",
            "bt_n", "bt_wr", "bt_net_pct", "bt_avg_per_trade_pct",
            "bt_recent_n", "bt_recent_wr", "bt_recent_net_pct",
            "live_n", "live_wins", "live_losses", "live_pnl", "live_open",
            "verdict"]
    for c in cols:
        if c not in m.columns:
            m[c] = ""
    out = m[cols].sort_values(["verdict", "live_pnl"], ascending=[True, False])
    out.to_csv(OUT_CSV, index=False)

    # ── headline ──
    traded = out[out["live_n"].fillna(0) > 0]
    print(f"\n{'='*74}\n  BACKTEST({DAYS}d) vs LIVE({RECENT}d) — deployed combo strategies\n{'='*74}")
    print(f"strategies backtested : {len(out)} | traded live: {len(traded)}")
    for v, g in traded.groupby("verdict"):
        tot = g['live_pnl'].sum()
        print(f"{v:28s} {len(g):>3} strats | live P&L ₹{tot:+,.0f}")
    prof_live = traded[traded["live_pnl"] > 0]
    if len(prof_live):
        consistent = prof_live[prof_live["verdict"] == "CONSISTENT-PROFIT ✔"]
        print(f"\nLive-profitable strategies : {len(prof_live)}")
        print(f"  └ bhi {DAYS}d backtest me profit : {len(consistent)} "
              f"({len(consistent)/len(prof_live)*100:.0f}%)")
        warn = traded[traded["verdict"] == "LIVE-PROFIT-BT-LOSS ⚠"]
        if len(warn):
            print(f"  ⚠ live-profit par backtest-loss: {len(warn)}")
            print(warn[["rank","market","tf","live_pnl","bt_net_pct","bt_recent_net_pct"]]
                  .to_string(index=False))
    print(f"\nsaved -> {OUT_CSV}")
    print(f"[BT] done in {(datetime.now(timezone.utc)-t0).total_seconds():.0f}s")


if __name__ == "__main__":
    main()

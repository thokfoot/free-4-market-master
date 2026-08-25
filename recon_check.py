"""Recon v2 — point-in-time, evidence-based timestamp reconciliation.

For every live trade of a VERIFIED strategy group we ask, using FRESH
yfinance data downloaded independently:
  1. Which exact bar did the bot act on?  -> matched against its own
     Signal_Indicators.Close snapshot (saved in the ledger).
  2. Was that bar COMPLETE (session over) or PARTIAL (scan ran mid-session,
     so 'Close' was just the price at scan minute)?
  3. Would our backtest engine have fired on that SAME bar, evaluated
     point-in-time (history truncated at that bar), on both price bases
     (raw auto_adjust=False vs adjusted auto_adjust=True)?
And conversely every BACKTEST signal bar: did any live trade act on it?
"""
import os, sys, io, warnings, json, glob
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import numpy as np
import pandas as pd
import yfinance as yf
from concurrent.futures import ThreadPoolExecutor, as_completed

from config import SL_PCT, TP_PCT, MAX_HOLD_DAYS, get_region
from scanner import get_yf_ticker, compute_indicators
from scanner_intraday import compute_indicators_1h
import strategy_miner as sm

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_CSV = os.path.join(HERE, "logs", "recon_timestamps.csv")
IST = pd.Timedelta(hours=5, minutes=30)
CUTOFF = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=36)
REGION_NORM = {"India": "INDIAN", "INDIAN": "INDIAN", "US": "US", "Crypto": "CRYPTO", "CRYPTO": "CRYPTO"}

# Regular sessions in UTC (approx, Aug = EDT)
SESSION_UTC = {"US": (13.5, 20.0), "INDIAN": (3.75, 10.0), "CRYPTO": (0.0, 24.0)}
PERIODS = {"SWING_1d": ["5y", "2y"], "INTRADAY_1h": ["730d", "3mo"]}


def fresh(yft, interval, period, adjust):
    for attempt in range(3):
        try:
            d = yf.download(yft, period=period, interval=interval, progress=False,
                            auto_adjust=adjust, threads=False)
            if d is not None and not d.empty:
                break
        except Exception:
            d = None
        import time as _t
        _t.sleep(2 * (attempt + 1))
    if d is None or d.empty:
        return None
    if isinstance(d.columns, pd.MultiIndex):
        d.columns = d.columns.get_level_values(0)
    d = sm.normalize_columns(d)
    d = d[~d.index.duplicated(keep="last")].sort_index()
    return d


def load_verified():
    out = pd.read_csv(os.path.join(HERE, "logs", "backtest150_vs_live.csv"))
    ver = out[(out["verdict"] == "CONSISTENT-PROFIT ✔") &
              (pd.to_numeric(out["live_n"], errors="coerce").fillna(0) > 0)].copy()
    return {(get_yf_ticker(str(r["market"]).split(",")[0]), int(r["rank"]), str(r["tf"]))
            for _, r in ver.iterrows()}


def load_defs():
    rows = []
    for path, tf in (("data/strategies.csv", "SWING_1d"),
                     ("data/intraday_strategies.csv", "INTRADAY_1h")):
        df = pd.read_csv(os.path.join(HERE, path), on_bad_lines="warn")
        for _, r in df.iterrows():
            rows.append({"rank": int(r["Final_Rank"]), "market": str(r["Market"]),
                         "region": REGION_NORM.get(str(r["Region"]), "US"), "tf": tf,
                         "direction": str(r["Direction"]).upper(),
                         "factors": [f.strip() for f in str(r["Factors"]).split("+")]})
    return rows


def fires_pit(df_full, factors, bar_ts, ind_fn, min_rows):
    """Point-in-time factor evaluation: history truncated AT bar_ts."""
    try:
        ix = df_full.index
        if getattr(ix, "tz", None) is None:
            bar = bar_ts.tz_localize(None)
            if getattr(bar_ts, "tzinfo", None) is None:
                bar = bar_ts
            d = df_full[ix <= bar]
        else:
            b = bar_ts.tz_localize("UTC") if bar_ts.tzinfo is None else bar_ts.tz_convert("UTC")
            d = df_full[ix <= b]
        if len(d) < max(min_rows, 60):
            return False, "insufficient-history"
        d = ind_fn(d.copy())
        if d is None or len(d) < 60:
            return False, "ind-fail"
        last = d.iloc[-1]
        for f in factors:
            s = sm.factor_series(d, f)
            if s is None:
                return False, f"bad-factor:{f}"
            if not bool(s[-1]):
                return False, "factor-false:" + f
        return True, ""
    except Exception as e:
        return False, f"err:{e}"


def bt_signals(df_full, factors, direction, tf, region, ind_fn, min_rows):
    d = ind_fn(df_full.copy())
    facs = {}
    for f in set(factors):
        s = sm.factor_series(d, f)
        if s is None:
            return None
        facs[f] = s
    tr = sm.backtest_one(d, factors, facs, direction, tf, region) or []
    t = pd.DataFrame(tr, columns=["ts", "pnl"])
    t["ts"] = pd.to_datetime(t["ts"], utc=True)
    return t[t["ts"] >= CUTOFF]


def main():
    vkeys = load_verified()
    defs = [r for r in load_defs()
            if (get_yf_ticker(r["market"]), r["rank"], r["tf"]) in vkeys]
    print(f"[recon2] verified groups: {len(vkeys)}, defs matched: {len(defs)}")

    # ── fresh independent data: both price bases ──
    need = sorted({(get_yf_ticker(r["market"]),
                    "1h" if r["tf"] == "INTRADAY_1h" else "1d",
                    p, adj)
                   for r in defs for p in PERIODS[r["tf"]] for adj in (False, True)})
    DATA = {}
    with ThreadPoolExecutor(max_workers=6) as ex:
        futs = {ex.submit(fresh, y, iv, pe, ad): (y, iv, pe, ad) for y, iv, pe, ad in need}
        done = 0
        for fu in as_completed(futs):
            DATA[futs[fu]] = fu.result()
            done += 1
            if done % 10 == 0:
                print(f"[recon2] fresh data {done}/{len(need)}")

    # ── live ledger ──
    lv = pd.read_csv(os.path.join(HERE, "logs", "paper_trades.csv"), on_bad_lines="warn")
    lv = lv[lv["Pattern_Rank"].notna()].copy()
    lv["rank"] = pd.to_numeric(lv["Pattern_Rank"], errors="coerce").astype("Int64")
    lv = lv[lv["TimeFrame"].isin(["SWING_1d", "INTRADAY_1h"]) & (lv["rank"] < 900)]
    dts = []
    for _, r in lv.iterrows():
        try:
            dt = pd.Timestamp(str(r["Date"])[:10] + " " +
                              str(r["Time_IST"]).replace(" IST", ""))
            dts.append(dt.tz_localize("UTC") - IST)  # naive IST -> UTC
        except Exception:
            dts.append(pd.NaT)
    lv["dt_utc"] = dts

    # scan-log coverage + skips (for miss reasons)
    skips, covered = {}, {}
    for f in sorted(glob.glob(os.path.join(HERE, "logs", "daily_scan_*.json"))):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        date = str(d.get("date", ""))
        for r in d.get("runs", []):
            covered.setdefault(date, []).append((r.get("mode"), len(r.get("fired_patterns") or [])))
            for s in (r.get("skipped_entries") or []):
                skips.setdefault((date, str(s.get("ticker", "")).upper()), []).append(
                    str(s.get("reason", ""))[:60])

    out_rows = []
    for r in defs:
        yft = get_yf_ticker(r["market"])
        region = get_region(yft) or r["region"]
        tf, factors, direction = r["tf"], r["factors"], r["direction"]
        iv = "1h" if tf == "INTRADAY_1h" else "1d"
        ind_fn = compute_indicators_1h if iv == "1h" else compute_indicators
        min_rows = 200 if iv == "1h" else 60
        prim_p = PERIODS[tf][0]
        base_raw = DATA.get((yft, iv, prim_p, False))
        base_adj = DATA.get((yft, iv, prim_p, True))
        grp = lv[(lv["Ticker"].astype(str) == str(yft)) & (lv["rank"] == r["rank"])
                 & (lv["TimeFrame"] == tf)]

        # ── backtest signal bars (harness parity: raw, primary period) ──
        sigs = None
        if base_raw is not None:
            try:
                sigs = bt_signals(base_raw, factors, direction, tf, region, ind_fn, min_rows)
            except Exception as e:
                print(f"[recon2] bt fail {yft}#{r['rank']}: {e}")
        sig_dates = set(sigs["ts"].dt.floor("D")) if sigs is not None and len(sigs) else set()

        # ── live trades of this group ──
        for _, lt in grp.iterrows():
            scan_dt = lt["dt_utc"]
            si_close = np.nan
            try:
                j = json.loads(lt.get("Signal_Indicators")) \
                    if isinstance(lt.get("Signal_Indicators"), str) else {}
                si_close = float(j.get("Close"))
            except Exception:
                si_close = np.nan
            # locate acted-on bar via SI.Close (or entry price fallback)
            src = base_adj if base_adj is not None else base_raw
            acted_ts, price_note = pd.NaT, ""

            def _utc_idx(df_):
                ix = df_.index
                if getattr(ix, "tz", None) is None:
                    return ix.tz_localize("UTC")
                return ix.tz_convert("UTC")

            if src is not None and len(src) and not pd.isna(scan_dt):
                ixc = _utc_idx(src)
                # bot can NEVER see a bar stamped after its scan moment
                mask = ixc <= scan_dt
                cand = src[mask].tail(6)
                ref = si_close if not np.isnan(si_close) else float(lt["Entry_Price"])
                best, bestd = None, 9e9
                for ts, row in cand.iterrows():
                    c = float(row["Close"])
                    dd = abs(c / ref - 1)
                    if dd < bestd:
                        best, bestd = ts, dd
                if best is not None and bestd <= 0.015:
                    acted_ts = best
                    price_note = f"si-match({bestd*100:.2f}%)"
                else:
                    acted_ts = cand.index[-1] if len(cand) else pd.NaT
                    price_note = "fallback-last-bar"

            partial = False
            if not pd.isna(acted_ts):
                a = acted_ts.tz_localize("UTC") if acted_ts.tzinfo is None else acted_ts.tz_convert("UTC")
                if iv == "1h":
                    # hourly bar closes 1h after its stamp
                    partial = scan_dt < a + pd.Timedelta(hours=1)
                elif region == "CRYPTO":
                    bdate = a.date()
                    partial = scan_dt < pd.Timestamp(bdate, tz="UTC") + pd.Timedelta(days=1)
                else:
                    bdate = a.date()
                    lo, hi = SESSION_UTC.get(region, (0, 24))
                    hm = scan_dt.hour + scan_dt.minute / 60
                    nxt_open = pd.Timestamp(bdate, tz="UTC") + pd.Timedelta(days=1, hours=lo)
                    partial = (nxt_open - scan_dt).total_seconds() > 0 and (lo <= hm < hi)

            pit_raw = pit_adj = False
            why_raw = why_adj = ""
            if base_raw is not None and not pd.isna(acted_ts):
                pit_raw, why_raw = fires_pit(base_raw, factors, acted_ts, ind_fn, min_rows)
            if base_adj is not None and not pd.isna(acted_ts):
                pit_adj, why_adj = fires_pit(base_adj, factors, acted_ts, ind_fn, min_rows)

            acted_d = pd.NaT
            if not pd.isna(acted_ts):
                a = acted_ts.tz_localize("UTC") if acted_ts.tzinfo is None else acted_ts.tz_convert("UTC")
                acted_d = a.floor("D")
            in_sigs = acted_d in sig_dates if acted_d is not pd.NaT else False

            if in_sigs:
                kind = "MATCH"
                note = price_note
            elif pit_raw or pit_adj:
                kind = "MATCH-data-revision"
                note = f"PIT fires on acted bar | raw={pit_raw} adj={pit_adj}"
            else:
                gap_days = (scan_dt - acted_ts.tz_localize("UTC")
                            ).total_seconds() / 86400 \
                    if getattr(acted_ts, "tzinfo", None) is None \
                    else (scan_dt - acted_ts.tz_convert("UTC")).total_seconds() / 86400
                if partial:
                    kind = "PARTIAL-BAR-ENTRY"
                    note = "fired mid-session; completed bar never signals"
                elif gap_days >= 2:
                    kind = "STALE-DATA"
                    note = f"bot evaluated {gap_days:.0f}d-old series (cache fallback <=7d)"
                else:
                    d0 = (scan_dt + IST).date().isoformat()
                    hits = skips.get((d0, str(yft).upper())) or []
                    runs = covered.get(d0, [])
                    if hits:
                        kind, note = "GATED", "gated:" + hits[0][:50]
                    elif not any(m in ("BOTH", "SWING") for m, _ in runs):
                        kind, note = "NO-SCAN", "no-scan-run-that-day"
                    else:
                        kind, note = "UNEXPLAINED", "no-signal-that-bar(unexplained)"
            out_rows.append(dict(
                side="LIVE", ticker=yft, rank=r["rank"], tf=tf, direction=direction,
                ts=str(lt["dt_utc"]), price=float(lt["Entry_Price"]),
                acted_bar=str(acted_ts), partial_bar=int(partial),
                pit_raw_fire=int(pit_raw), pit_adj_fire=int(pit_adj),
                kind=kind, note=note))

        # ── every backtest signal bar: claimed by live? ──
        if sigs is not None:
            claimed_dates = set()
            for o in out_rows:
                if o["side"] == "LIVE" and o["ticker"] == yft and o["rank"] == r["rank"] \
                        and o["tf"] == tf and o["kind"] in ("MATCH", "MATCH-data-revision"):
                    claimed_dates.add(str(o["acted_bar"])[:10])
            # context for misses: other-rank entries / open positions / scan coverage
            grp_all = lv[(lv["Ticker"].astype(str) == str(yft))
                         & (lv["TimeFrame"] == tf) & (lv["Direction"] == direction)]
            exits = []
            for _, o in grp_all.iterrows():
                try:
                    ex = pd.Timestamp(str(o.get("Exit_Time", "")).replace(" IST", ""))
                    if pd.notna(ex):
                        exits.append((o["dt_utc"], (ex.tz_localize("UTC") - IST)
                                      if ex.tzinfo is None else ex.tz_convert("UTC"),
                                      int(o["rank"])))
                except Exception:
                    pass
            for _, s in sigs.iterrows():
                d = s["ts"].floor("D")
                ds = str(d.date() if d.tz is None else d.tz_convert("UTC").date())
                if ds in claimed_dates:
                    kind, note = "BT-SIGNAL-TAKEN", ""
                else:
                    same_day_other = any(
                        pd.notna(o.get("dt_utc")) and str(o["dt_utc"].date()) == ds
                        for _, o in grp_all.iterrows())
                    pos_open = any(e0 <= s["ts"] <= e1 for e0, e1, _ in exits)
                    runs = covered.get(ds, [])
                    if pos_open and not same_day_other:
                        kind, note = "BT-SIGNAL-MISSED", "position-already-open"
                    elif same_day_other:
                        kind, note = "BT-SIGNAL-MISSED", "best-entry:other-rank-same-day"
                    elif not any(m in ("BOTH", "SWING") for m, _ in runs):
                        kind, note = "BT-SIGNAL-MISSED", "no-scan-run-that-day"
                    else:
                        kind, note = "BT-SIGNAL-MISSED", "scan-ran-signal-not-taken"
                out_rows.append(dict(
                    side="BT", ticker=yft, rank=r["rank"], tf=tf, direction=direction,
                    ts=str(s["ts"]), price="", acted_bar="", partial_bar="",
                    pit_raw_fire="", pit_adj_fire="",
                    kind=kind, note=note))

    out = pd.DataFrame(out_rows)
    out.to_csv(OUT_CSV, index=False)

    print(f"\n{'='*74}\n  RECON v2 — verified strategies, point-in-time (fresh data)\n{'='*74}")
    lt_rows = out[out["side"] == "LIVE"]
    print(f"LIVE trades analysed : {len(lt_rows)}")
    for k, c in lt_rows["kind"].value_counts().items():
        print(f"  {k:30s} {c:>3}")
    pb = int(lt_rows["partial_bar"].sum())
    print(f"  -> entered on PARTIAL (mid-session) bars: {pb}")
    bt_rows = out[out["side"] == "BT"]
    print(f"BT signal bars ({CUTOFF.date()} ->): {len(bt_rows)} "
          f"| taken: {(bt_rows['kind']=='BT-SIGNAL-TAKEN').sum()} "
          f"| missed: {(bt_rows['kind']=='BT-SIGNAL-MISSED').sum()}")
    print("\n-- BT-SIGNAL-MISSED by group --")
    ms = bt_rows[bt_rows["kind"] == "BT-SIGNAL-MISSED"]
    if len(ms):
        print(ms.groupby(["ticker", "rank", "tf"]).size().to_string())
    print(f"\nsaved -> {OUT_CSV}")


if __name__ == "__main__":
    main()

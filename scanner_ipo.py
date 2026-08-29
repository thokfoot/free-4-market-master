"""
FREE 3-Market v5.21 - IPO EDGE SCANNER (daily 1d, verified listing-edge strategies)
====================================================================================
Two verified real-data edges on IPO listings (mainboard only):

  DIP (rank 936, LONG):  mainboard IPO that opened at a premium. Anchor =
      max(High of first 3 sessions after listing). When the close first
      crosses -10% below the anchor -> LONG. Exit +5%, SL -8%, max hold 20
      trading days. Verified on 11 high-opening mainboard IPOs (2023-24,
      avg listing gain +67%): every one dropped >= -8% from the listing
      anchor (11/11), and BUY -10% -> EXIT +5% (20d) = 100% win, +4.8% avg.

  SHORT (rank 937, SHORT): negative-opening mainboard IPO. On the day-2
      close (3rd session) -> SHORT. Cover -8%, stop +5%, max hold 30 days.
  BREAK (rank 938, LONG):  flat-opening mainboard IPO (listing gain 0-30%).
      Close crossing ABOVE the listing-day high (within 30 sessions) -> LONG.
      Exit +20%, stop -12%, max hold 45 days. Verified 61.4% win, +6.3%/trade
      on 83 trades 2023-26, OOS (2025-26) +6.35% — consistent every year.
      Verified 70-80% win on negative/low-opening IPOs.

No lookahead / no backfill:
  * DIP fires ONLY on the bar where close crosses the trigger (close[t]<=T
    and close[t-1]>T). IPOs that already crossed long ago never fire.
  * SHORT fires ONLY when the day-2 bar is the LATEST completed bar
    (fresh listing). Old IPOs never fire.
  * Once-per-IPO: data/ipo_state.json records emitted tickers, so a signal
    is emitted at most once per IPO per strategy.

Usage (called from bot.py --mode=both, or standalone):
    python scanner_ipo.py [--dry]
"""
import os
import json
import numpy as np
import pandas as pd
import pytz
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from config import (
    IPO_UNIVERSE_FILE, IPO_STATE_FILE, IPO_VARIANTS,
    IPO_DIP_RANK, IPO_SHORT_RANK, IPO_BREAK_RANK,
)
import market_data

IST = pytz.timezone("Asia/Kolkata")


def _download(ticker: str, interval: str, period: str, force: bool = False) -> pd.DataFrame:
    import time as _time
    for attempt in range(3):
        try:
            df = market_data.download(ticker, interval=interval, period=period,
                                       force_refresh=force, allow_stale=False)
            if df is not None and len(df) > 0:
                return df
        except Exception:
            pass
        _time.sleep(1.5 * (attempt + 1))
    return None


def _norm(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or len(df) == 0:
        return df
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    df = df[~df.index.duplicated(keep="first")].sort_index()
    return df


def load_ipo_universe() -> list:
    """List of {strategy, ticker, name, listing_date} from data/ipo_watchlist.csv."""
    out = []
    if not os.path.exists(IPO_UNIVERSE_FILE):
        return out
    try:
        df = pd.read_csv(IPO_UNIVERSE_FILE)
        for _, r in df.iterrows():
            strat = str(r.get("strategy", "")).strip().upper()
            tk = str(r.get("ticker", "")).strip()
            if strat not in ("DIP", "SHORT", "BREAK") or not tk:
                continue
            if not tk.endswith(".NS"):
                tk += ".NS"
            out.append({
                "strategy": strat,
                "ticker": tk,
                "name": str(r.get("name", "")).strip(),
                "listing_date": str(r.get("listing_date", "")).strip(),
            })
    except Exception as e:
        print(f"[IPO] watchlist load error: {e}")
    return out


def _load_state() -> dict:
    try:
        if os.path.exists(IPO_STATE_FILE):
            with open(IPO_STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return {"DIP": {}, "SHORT": {}, "BREAK": {}}


def _save_state(state: dict):
    try:
        tmp = IPO_STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, IPO_STATE_FILE)
    except Exception as e:
        print(f"[IPO] state save error: {e}")


def _dip_anchor(df: pd.DataFrame) -> float:
    """Anchor = max High of the first 3 sessions after listing (data start)."""
    head = df.head(3)
    if len(head) == 0:
        return np.nan
    return float(head["High"].max())


def _break_anchor(df: pd.DataFrame) -> float:
    """Anchor = listing-day high (first session). BREAK fires when close
    crosses above this within break_max_days sessions."""
    if len(df) == 0:
        return np.nan
    return float(df["High"].iloc[0])


def _variant(v_key: str):
    return next((v for v in IPO_VARIANTS if v["key"] == v_key), None)


def scan_ipo(limit: int = None, dry: bool = False) -> dict:
    """Scan IPO watchlist for DIP / SHORT signals (daily 1d candles)."""
    start = datetime.now(IST)
    date_str = start.strftime("%Y-%m-%d")

    universe = load_ipo_universe()
    if limit:
        universe = universe[:limit]
    print(f"[IPO] Watchlist: {len(universe)} IPOs | {date_str}")

    state = _load_state()
    intervals_needed = sorted({v["interval"] for v in IPO_VARIANTS})
    period_map = {v["interval"]: v["period"] for v in IPO_VARIANTS}
    ticker_data = {}
    scan_errors = 0
    for interval in intervals_needed:
        period = period_map.get(interval, "730d")
        with ThreadPoolExecutor(max_workers=8) as pool:
            futs = {pool.submit(_download, u["ticker"], interval, period, True): u for u in universe}
            for fut in as_completed(futs):
                u = futs[fut]
                try:
                    df = _norm(fut.result())
                    if df is not None and len(df) >= 4:
                        ticker_data[(interval, u["ticker"])] = df
                    else:
                        scan_errors += 1
                except Exception:
                    scan_errors += 1
        ok = len([1 for (i, _) in ticker_data if i == interval])
        print(f"[IPO] {interval} data OK: {ok}/{len(universe)}, errors {scan_errors}")

    all_signals = []
    fired = []
    for u in universe:
        tk = u["ticker"]
        strat = u["strategy"]
        v = _variant(strat)
        if v is None:
            continue
        df = ticker_data.get((v["interval"], tk))
        if df is None:
            continue
        rank = v["rank"]
        close = float(df.iloc[-1]["Close"])
        signal = {
            "rank": rank, "market": "NSE", "region": "INDIAN",
            "ticker": tk, "direction": v["direction"],
            "factors": v["factors"], "win_rate": v["win_rate"],
            "trades_count": v.get("trades_count", 0), "close": close,
            "entry_price": close, "interval": v["interval"],
            "sl_pct": v["sl_pct"], "tp_pct": v["tp_pct"],
            "risk_pct": v.get("risk_pct", 0.01),
            "max_hold_days": v["max_hold_days"],
            "fired": False, "reason": "No signal",
            "signal_indicators": {"Close": round(close, 2)},
        }
        already = tk in state.get(strat, {})
        if strat == "DIP":
            anchor = _dip_anchor(df)
            trigger = anchor * (1 - v["dip_pct"] / 100.0) if np.isfinite(anchor) else np.nan
            signal["signal_indicators"]["AnchorHigh"] = round(anchor, 2) if np.isfinite(anchor) else None
            signal["signal_indicators"]["Trigger"] = round(trigger, 2) if np.isfinite(trigger) else None
            if np.isfinite(trigger) and len(df) >= 4:
                prev_close = float(df.iloc[-2]["Close"])
                crossed = (close <= trigger) and (prev_close > trigger)
                if crossed:
                    if already:
                        signal["reason"] = "Already entered (once-per-IPO)"
                    else:
                        signal["fired"] = True
                        signal["reason"] = f"Crossed -10% of listing anchor ({trigger:.2f})"
                elif close <= trigger:
                    signal["reason"] = "Below trigger but cross was earlier (no backfill)"
                else:
                    signal["reason"] = f"Above trigger ({trigger:.2f}) - watching"
        elif strat == "BREAK":
            # Anchor = listing-day high (first session). Fire ONLY on the bar
            # where close crosses ABOVE the anchor within break_max_days of
            # listing (no backfill for late crosses).
            anchor = _break_anchor(df)
            signal["signal_indicators"]["ListingHigh"] = round(anchor, 2) if np.isfinite(anchor) else None
            n_sessions = len(df)  # data starts at listing day
            if not np.isfinite(anchor):
                signal["reason"] = "No listing high available"
            elif n_sessions > v.get("break_max_days", 30) + 1:
                signal["reason"] = "Breakout window passed (no backfill)"
            else:
                prev_close = float(df.iloc[-2]["Close"]) if len(df) >= 2 else None
                crossed = (close > anchor) and (prev_close is not None and prev_close <= anchor)
                if crossed:
                    if already:
                        signal["reason"] = "Already entered (once-per-IPO)"
                    else:
                        signal["fired"] = True
                        signal["reason"] = f"Close > listing-day high ({anchor:.2f}) breakout"
                elif close > anchor:
                    signal["reason"] = "Above listing high but cross was earlier (no backfill)"
                else:
                    signal["reason"] = f"Below listing high ({anchor:.2f}) - watching"
        else:  # SHORT
            # day-2 bar = 3rd session; must be the LATEST completed bar (fresh listing)
            if len(df) >= 3:
                day2_bar = df.iloc[2]
                is_last = (df.index[-1] == day2_bar.name)
                signal["signal_indicators"]["Day2Date"] = str(pd.Timestamp(day2_bar.name).date())
                signal["signal_indicators"]["Day2Close"] = round(float(day2_bar["Close"]), 2)
                if is_last:
                    if already:
                        signal["reason"] = "Already entered (once-per-IPO)"
                    else:
                        signal["fired"] = True
                        signal["reason"] = "Day-2 close SHORT (fresh listing)"
                        signal["close"] = float(day2_bar["Close"])
                        signal["entry_price"] = float(day2_bar["Close"])
                else:
                    signal["reason"] = "Day-2 bar is historical (no backfill)"
            else:
                signal["reason"] = "Listing < 3 sessions old"
        all_signals.append(signal)
        if signal["fired"]:
            fired.append(signal)

    # Persist state for fired signals (once-per-IPO, recorded at emission time so
    # a rejected/duplicate entry can never re-fire).
    if fired and not dry:
        for s in fired:
            if s["rank"] == IPO_DIP_RANK:
                strat = "DIP"
            elif s["rank"] == IPO_SHORT_RANK:
                strat = "SHORT"
            elif s["rank"] == IPO_BREAK_RANK:
                strat = "BREAK"
            else:
                strat = "DIP"
            state.setdefault(strat, {})[s["ticker"]] = start.strftime("%Y-%m-%d")
        _save_state(state)

    print(f"[IPO] Signals: {len(all_signals)}, fired: {len(fired)}")
    return {
        "all_signals": all_signals,
        "fired_signals": fired,
        "ticker_data": ticker_data,
        "scan_errors": scan_errors,
        "duration": (datetime.now(IST) - start).total_seconds(),
    }


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--limit", type=int, default=None)
    a = ap.parse_args()
    res = scan_ipo(limit=a.limit, dry=a.dry)
    for s in res["all_signals"]:
        print(f"  [{'FIRE' if s['fired'] else 'wait'}] {s['ticker']:20s} {s['direction']:5s} "
              f"#{s['rank']} close={s['close']:.2f} - {s['reason']}")


# ── AUTO-DISCOVERY of new IPO listings (v5.21) ─────────────────────────────
# Every weekday the bot fetches Chittorgarh's mainboard performance tracker
# (server-rendered HTML, current year). Companies NOT already in the watchlist
# are classified by their Listing Day Gain:
#     >= +30%  -> DIP  (premium listing; -10% dip-buy edge, 11/11 verified)
#     <  0%    -> SHORT (negative opening; day-2 short edge, 70-80% verified)
# The Yahoo symbol is resolved and validated against fresh 1d data (must have
# listed within the last 60 sessions). Newly listed IPOs are appended to
# data/ipo_watchlist.csv automatically; the scanner's no-backfill + once-per-IPO
# logic then handles entry timing (DIP waits for the -10% cross, SHORT fires on
# the day-2 bar).
IPO_TRACKER_URL = "https://www.chittorgarh.com/ipo/ipo_perf_tracker.asp?year={}"
IPO_DISCOVERY_MAX_AGE_DAYS = 60
_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}


def _fetch_listed_this_year(year: int) -> list:
    """[(company_name, listing_day_gain_pct), ...] from the mainboard tracker."""
    import requests as _req
    import re as _re
    import html as _html
    try:
        r = _req.get(IPO_TRACKER_URL.format(year), headers=_UA, timeout=25)
        if r.status_code != 200:
            print(f"[IPO] Tracker HTTP {r.status_code}")
            return []
        tables = _re.findall(r"<table[^>]*>.*?</table>", r.text, _re.S)
        for t in tables:
            rows = _re.findall(r"<tr[^>]*>(.*?)</tr>", t, _re.S)
            if len(rows) <= 3:
                continue
            out = []
            for row in rows[1:]:
                cells = _re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", row, _re.S)
                cells = [_html.unescape(_re.sub(r"<[^>]+>", "", c)).strip() for c in cells]
                if len(cells) < 2 or not cells[0]:
                    continue
                name = cells[0]
                gain = None
                try:
                    gain = float(cells[1].replace("%", "").replace(",", "").strip())
                except (TypeError, ValueError):
                    gain = None
                out.append((name, gain))
            if out:
                return out
    except Exception as e:
        print(f"[IPO] Tracker fetch error: {e}")
    return []


def _chart_probe(sym: str) -> bool:
    """True if Yahoo chart API has real bars for this symbol."""
    import requests as _req
    try:
        r = _req.get(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}",
                     params={"range": "3mo", "interval": "1d"},
                     headers=_UA, timeout=8)
        res = r.json().get("chart", {}).get("result")
        return bool(res and res[0].get("timestamp"))
    except Exception:
        return False


def _name_candidates(name: str) -> list:
    """Derive plausible NSE ticker symbols from a company name."""
    import re as _re
    n = name.upper()
    for w in (" LTD.", " LIMITED", " LTD", " PVT.", " PVT", " PRIVATE LIMITED",
              " INDIA", " INDIAN", " INDUSTRIES", " INDUSTRY", " VENTURES",
              " TECHNOLOGIES", " TECHNOLOGY", " SYSTEMS", " SYSTEM",
              " CORPORATION", " ENTERPRISES", " ENTERPRISE", " HOLDINGS",
              " HOLDING", " GLOBAL", " GROUP", " &", " AND", " THE",
              " COMPANY", " CO", " LIMITED", " INTERNATIONAL"):
        n = n.replace(w, " ")
    tokens = [t for t in _re.sub(r"[^A-Z0-9 ]+", "", n).split() if t]
    if not tokens:
        return []
    clean = "".join(tokens)
    cands = [clean, tokens[0]]
    if len(clean) > 10:
        cands.append(clean[:10])
    if len(tokens) >= 2:
        cands.append(tokens[0] + tokens[1][:2])
    if len(tokens) >= 2:
        cands.append("".join(t[:1] for t in tokens[:4]))
    out = []
    for c in cands:
        if c and c not in out:
            out.append(c)
    return out


def _ysearch_symbol(name: str) -> str:
    """Resolve a company name to a Yahoo .NS symbol (best-effort).

    Tries, in order:
      1. cached resolution (data/ipo_state.json _sym_cache)
      2. Yahoo finance search (matches most listed names)
      3. BSE (.BO) twin of the searched name -> same ticker on .NS
      4. Name-derived ticker candidates probed against the chart API

    Successful resolutions are cached so the daily discovery does not
    re-search the same names (35+ dead candidates) every day, which
    caused Yahoo rate-limits and transient misses.
    """
    import requests as _req
    state = _load_state()
    sym_cache = state.setdefault("_sym_cache", {})
    cache_key = _norm_name(name)
    if cache_key in sym_cache:
        return sym_cache[cache_key] or None
    queries = [name]
    for suf in (" Ltd.", " Limited", " Ltd", " Private Limited"):
        if name.endswith(suf):
            queries.append(name[: -len(suf)])
            break
    queries.append(name.split(" Ltd.")[0].split(" Limited")[0])
    seen = set()
    bo_sym = None
    found = None
    for q in queries:
        q = q.strip()
        if not q or q in seen:
            continue
        seen.add(q)
        try:
            r = _req.get(
                "https://query1.finance.yahoo.com/v1/finance/search",
                params={"q": q, "quotesCount": 5},
                headers=_UA, timeout=12)
            if r.status_code != 200:
                continue
            for qq in r.json().get("quotes", []):
                sym = qq.get("symbol", "")
                if sym.endswith(".NS") and qq.get("quoteType") == "EQUITY":
                    found = sym
                    break
                if sym.endswith(".BO") and qq.get("quoteType") == "EQUITY" \
                        and bo_sym is None:
                    bo_sym = sym
            if found:
                break
        except Exception:
            continue
    # BSE twin -> NSE (mainboard IPOs list on both, ticker usually same)
    if not found and bo_sym:
        ns = bo_sym[:-3] + ".NS"
        if _chart_probe(ns):
            found = ns
    # Name-derived candidates (new listings search has not indexed)
    if not found:
        for cand in _name_candidates(name):
            ns = cand + ".NS"
            if _chart_probe(ns):
                found = ns
                break
    if found:
        sym_cache[cache_key] = found
        _save_state(state)
    return found


def _norm_name(name: str) -> str:
    import re as _re
    n = name.lower()
    for suf in (" ltd.", " ltd", " limited", " private", " &", " and "):
        n = n.replace(suf, " ")
    return _re.sub(r"[^a-z0-9]+", "", n)


def discover_new_ipos() -> list:
    """Detect newly listed mainboard IPOs and append them to the watchlist.

    Runs at most once per calendar day (state stored in data/ipo_state.json).
    Returns a list of dicts {ticker, strategy, name, listing_date, gain}.
    """
    import csv as _csv
    from datetime import datetime as _dt, timedelta as _td

    state = _load_state()
    disc = state.get("_discovery", {})
    today = _dt.now(IST).strftime("%Y-%m-%d")
    if disc.get("last") == today:
        return []  # already ran today

    year = _dt.now(IST).year
    listed = _fetch_listed_this_year(year)
    if not listed:
        # record anyway so we don't hammer the tracker every run
        state["_discovery"] = {"last": today, "added": state.get("_discovery", {}).get("added", 0)}
        _save_state(state)
        return []

    # existing watchlist (name-normalized + tickers)
    existing_names = set()
    existing_tickers = set()
    for u in load_ipo_universe():
        existing_names.add(_norm_name(u["name"]))
        existing_tickers.add(u["ticker"])

    added = []
    for name, gain in listed:
        if gain is None:
            continue
        nname = _norm_name(name)
        if nname in existing_names:
            continue
        if gain >= 30.0:
            strat = "DIP"
        elif gain < 0.0:
            strat = "SHORT"
        elif 0.0 <= gain < 30.0:
            strat = "BREAK"  # flat listing: listing-high breakout edge (verified 61% win)
        else:
            continue
        sym = _ysearch_symbol(name)
        if not sym:
            print(f"[IPO] Discovery: no Yahoo symbol for '{name}' (gain {gain:+.1f}%) — skipped")
            continue
        if sym in existing_tickers:
            existing_names.add(nname)
            continue
        # validate freshness with real data (first-bar date cached once)
        first_bar_cache = state.setdefault("_sym_firstbar", {})
        if sym in first_bar_cache:
            first_date = first_bar_cache[sym]
        else:
            try:
                df = _download(sym, "1d", "730d")
                df = _norm(df)
                if df is None or len(df) == 0:
                    print(f"[IPO] Discovery: no data for {sym} — skipped")
                    continue
                first_date = str(pd.Timestamp(df.index[0]).date())
                first_bar_cache[sym] = first_date
                _save_state(state)
            except Exception as e:
                print(f"[IPO] Discovery: {sym} data error {e} — skipped")
                continue
        age = (pd.Timestamp(today).date() - pd.Timestamp(first_date).date()).days
        if age > IPO_DISCOVERY_MAX_AGE_DAYS:
            print(f"[IPO] Discovery: {sym} listed {first_date} ({age}d ago) — too old, skipped")
            continue
        listing_date = first_date
        added.append({"ticker": sym, "strategy": strat, "name": name,
                      "listing_date": listing_date, "gain": gain})

    if added:
        rows = []
        if os.path.exists(IPO_UNIVERSE_FILE):
            with open(IPO_UNIVERSE_FILE, "r", encoding="utf-8") as f:
                rows = list(_csv.DictReader(f))
        seen = set()
        for r in rows:
            seen.add((str(r.get("strategy", "")).upper(), str(r.get("ticker", "")).strip()))
        with open(IPO_UNIVERSE_FILE, "a", newline="", encoding="utf-8") as f:
            w = _csv.writer(f)
            if not rows:
                w.writerow(["strategy", "ticker", "name", "listing_date", "notes"])
            for a in added:
                key = (a["strategy"], a["ticker"])
                if key in seen:
                    continue
                w.writerow([a["strategy"], a["ticker"], a["name"], a["listing_date"],
                            f"auto {a['gain']:+.1f}%"])
                seen.add(key)
                print(f"[IPO] Discovery ADDED: {a['strategy']} {a['ticker']} "
                      f"({a['name']}) listed {a['listing_date']} gain {a['gain']:+.1f}%")

    state["_discovery"] = {"last": today, "added": len(added)}
    _save_state(state)
    return added

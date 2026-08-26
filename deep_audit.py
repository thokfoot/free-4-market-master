"""DEEP AUDIT - every trade, every parameter, every possible inconsistency.
Run: python deep_audit.py
"""
import os, sys, warnings
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd

from config import (FADE_VARIANTS, US_FADE_VARIANTS, LONG_BOUNCE_VARIANTS,
                    IPO_VARIANTS, CAPITAL_BY_MARKET, INTRADAY_CAPITAL,
                    FADE_CAPITAL, US_FADE_CAPITAL, LONG_BOUNCE_CAPITAL,
                    IPO_CAPITAL, FADE_MAX_HOLD_HOURS, LONG_BOUNCE_MAX_HOLD_HOURS,
                    US_FADE_MAX_HOLD_HOURS)
from paper_trader import load_portfolio

C_FADE_HOURS = FADE_MAX_HOLD_HOURS
C_LB_HOURS = LONG_BOUNCE_MAX_HOLD_HOURS
C_USFADE_HOURS = US_FADE_MAX_HOLD_HOURS

HERE = os.path.dirname(os.path.abspath(__file__))
issues = []


def flag(check, ident, detail):
    issues.append({"check": check, "item": str(ident), "detail": detail})


def close_enough(a, b):
    try:
        return abs(float(a) - float(b)) <= max(0.02, abs(float(b)) * 0.005)
    except Exception:
        return False


def main():
    t = pd.read_csv(os.path.join(HERE, "logs", "paper_trades.csv"),
                    on_bad_lines="warn")
    t["rk"] = pd.to_numeric(t["Pattern_Rank"], errors="coerce")
    print(f"[audit] ledger rows: {len(t)}")

    # 1-3: P&L math / % / chronology  (stored P&L is NET of charges)
    bp = bpct = bt = 0
    rates = {"INDIAN": 0.0012, "US": 0.0002, "CRYPTO": 0.003}
    for _, r in t.iterrows():
        if str(r.Status) != "CLOSED" or pd.isna(r.get("Entry_Price")) \
                or pd.isna(r.get("Exit_Price")):
            continue
        e, x, q = float(r.Entry_Price), float(r.Exit_Price), float(r.Qty or 0)
        d = str(r.Direction).upper()
        rate = rates.get(str(r.get("Mode", "")).upper(), 0.0)
        exp = (x - e) * q if d == "LONG" else (e - x) * q
        exp -= abs(e * q) * rate          # charges deducted by design
        got = float(r["P&L"] or 0)
        if abs(exp - got) > 1.0:
            bp += 1
            if bp <= 5:
                flag("P&L-MATH", f"{r.Ticker}#{r.rk}@{r.Date}",
                     f"stored {got:.2f} vs net-recomputed {exp:.2f}")
        try:
            pe = float(str(r["P&L_%"]).replace("%", ""))
            exp_pct = (x / e - 1) * (1 if d == "LONG" else -1) * 100
            if abs(pe - exp_pct) > 0.25:
                bpct += 1
                if bpct <= 5:
                    flag("P&L-%", f"{r.Ticker}#{r.rk}@{r.Date}",
                         f"stored {pe:.2f}% vs {exp_pct:.2f}%")
        except Exception:
            pass
        try:
            et = str(r.get("Exit_Time", ""))[:19]
            ex_ts = pd.to_datetime(et[:10] + " " + (et[11:19] if len(et) > 16 else "00:00:00"))
            hh, mm, ss = (str(r.get("Time_IST", "00:00:00 IST"))[:8]).split(":")
            in_ts = pd.to_datetime(f"{r.Date} {hh}:{mm}:{ss}")
            if ex_ts < in_ts:
                bt += 1
                flag("CHRONOLOGY", f"{r.Ticker}#{r.rk}", f"exit {et} < entry")
        except Exception:
            pass
    print(f"  P&L math mismatches : {bp}")
    print(f"  P&L-% mismatches    : {bpct}")
    print(f"  chronology violations: {bt}")

    # 4: duplicates
    dup = t[t.duplicated(subset=["Ticker", "Pattern_Rank", "Date", "Direction"],
                         keep=False)]
    print(f"  duplicate keys      : {len(dup)}")
    for _, r in dup.head(5).iterrows():
        flag("DUPLICATE", f"{r.Ticker}#{r.rk}@{r.Date}", "same key twice")

    # 5: variant SL/TP/Hold vs config
    var = {}
    for fam in (FADE_VARIANTS, US_FADE_VARIANTS, LONG_BOUNCE_VARIANTS,
                IPO_VARIANTS):
        for v in fam:
            var[int(v["rank"])] = v
    vrows = t[t.TimeFrame.astype(str).isin(
        ["FADE_1h", "US_FADE_5m", "LONG_BOUNCE_5m", "IPO_1d"])]
    bsl = btp = bh = n = 0
    for _, r in vrows.iterrows():
        v = var.get(int(r.rk)) if pd.notna(r.rk) else None
        if not v or pd.isna(r.Entry_Price):
            continue
        n += 1
        e = float(r.Entry_Price)
        d = str(r.Direction).upper()
        sl_e = e * (1 + v["sl_pct"]) if d == "SHORT" else e * (1 - v["sl_pct"])
        tp_e = e * (1 - v["tp_pct"]) if d == "SHORT" else e * (1 + v["tp_pct"])
        if not close_enough(r.SL, sl_e):
            bsl += 1
            if bsl <= 5:
                flag("VAR-SL", f"{r.Ticker}#{int(r.rk)}@{r.Date}",
                     f"SL {r.SL} vs cfg {sl_e:.2f}")
        if not close_enough(r.Target, tp_e):
            btp += 1
            if btp <= 5:
                flag("VAR-TP", f"{r.Ticker}#{int(r.rk)}@{r.Date}",
                     f"TP {r.Target} vs cfg {tp_e:.2f}")
        if pd.notna(r.get("MaxHold")):
            mhv = int(r.MaxHold)
            tfname = str(r.TimeFrame)
            if tfname == "FADE_1h":
                ok = (mhv == C_FADE_HOURS)
            elif tfname == "LONG_BOUNCE_5m":
                ok = (mhv == C_LB_HOURS)
            elif tfname == "US_FADE_5m":
                ok = (mhv == C_USFADE_HOURS)
            else:
                ok = (int(v.get("max_hold_days") or -9) == mhv)
            if not ok:
                bh += 1
                if bh <= 5:
                    flag("VAR-HOLD", f"{r.Ticker}#{int(r.rk)}@{r.Date}",
                         f"MaxHold {mhv} unexpected for {tfname}")
    print(f"  variant SL drift    : {bsl}/{n}")
    print(f"  variant TP drift    : {btp}/{n}")
    print(f"  variant HOLD drift  : {bh}/{n}")

    # 6: combo Expected_WinRate vs def WR column
    sw = pd.read_csv(os.path.join(HERE, "data", "strategies.csv"))
    wr_col = next((c for c in sw.columns
                   if c.strip().lower() in ("winrate%", "wr%")), None)
    if wr_col and "Expected_WinRate" in t.columns:
        from scanner import get_yf_ticker
        cmap = {}
        for _, r in sw.iterrows():
            y = get_yf_ticker(str(r.Market)) or str(r.Market)
            cmap[(y, int(r.Final_Rank))] = float(r[wr_col])
        crows = t[t.TimeFrame.astype(str) == "SWING_1d"]
        bw = 0
        for _, r in crows.iterrows():
            k = (str(r.Ticker), int(r.rk)) if pd.notna(r.rk) else None
            ew = pd.to_numeric(pd.Series([r.get("Expected_WinRate")]),
                               errors="coerce").iloc[0]
            if k in cmap and pd.notna(ew) and \
                    not close_enough(ew, cmap[k]):
                bw += 1
                if bw <= 5:
                    flag("COMBO-WR", f"{r.Ticker}#{int(r.rk)}",
                         f"ledger {ew} vs def {cmap[k]}")
        print(f"  combo WR drift      : {bw}/{len(crows)}")

    # 7: capital buckets
    port = load_portfolio()
    cbm = port.get("capital_by_market", {})
    expected = dict(CAPITAL_BY_MARKET)
    expected.update({"INTRADAY": INTRADAY_CAPITAL, "FADE": FADE_CAPITAL,
                     "US_FADE": US_FADE_CAPITAL,
                     "LONG_BOUNCE": LONG_BOUNCE_CAPITAL, "IPO": IPO_CAPITAL})
    for k, ev in expected.items():
        av = cbm.get(k)
        if av is not None and abs(float(av) - float(ev)) > float(ev) * 0.30:
            flag("CAPITAL", k, f"bucket {av} far from configured {ev}")
    print(f"  capital buckets     : total Rs{sum(cbm.values()):,.0f} "
          f"| portfolio Rs{port.get('total_capital', '?')}")

    # 8: config guards
    import config as C
    if C.FADE_ALLOW_SHORT:
        flag("CONFIG", "FADE_ALLOW_SHORT", "should be False (paused)")
    if C.GAP_DOWN_B_ENABLED:
        flag("CONFIG", "GAP_DOWN_B_ENABLED", "should be False")
    print(f"  config guards       : FADE paused={not C.FADE_ALLOW_SHORT}, "
          f"GAP_DOWN_B off={not C.GAP_DOWN_B_ENABLED}")

    # summary
    print(f"\n{'='*70}\n  DEEP AUDIT: {len(issues)} issue(s)\n{'='*70}")
    if issues:
        dfi = pd.DataFrame(issues)
        dfi.to_csv(os.path.join(HERE, "logs", "deep_audit_issues.csv"),
                   index=False)
        print(dfi.to_string(index=False))
        print("saved -> logs/deep_audit_issues.csv")
    else:
        print("CLEAN - no inconsistencies found")


if __name__ == "__main__":
    main()

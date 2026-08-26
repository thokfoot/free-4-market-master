"""v5.27b cleanup - remove leftover DATA of REMOVED strategies.

Scope (removed != paused):
  * Ledger rows whose combo rank no longer exists in any def file
    (QQQ#46, IWM#30 x3, DIA#76 x3 - casualties of earlier removals)
  * strategy_stats.json entries for ranks that are not deployed anywhere
  FADE_1h rows are NOT touched: the family is PAUSED, not removed.
Run: python cleanup_removed_strategies.py
"""
import os, sys, json, warnings
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import pandas as pd
from datetime import datetime

from paper_trader import load_portfolio, save_portfolio, rebuild_portfolio_from_csv
from config import FADE_VARIANTS, US_FADE_VARIANTS, LONG_BOUNCE_VARIANTS, IPO_VARIANTS

HERE = os.path.dirname(os.path.abspath(__file__))
LEDGER = os.path.join(HERE, "logs", "paper_trades.csv")
STATS = os.path.join(HERE, "logs", "strategy_stats.json")
ARCH = os.path.join(HERE, "logs", "archive")


def active_ranks():
    sw = pd.read_csv(os.path.join(HERE, "data", "strategies.csv"))
    it = pd.read_csv(os.path.join(HERE, "data", "intraday_strategies.csv"))
    from scanner import get_yf_ticker
    ranks = set(sw.Final_Rank.astype(int)) | set(it.Final_Rank.astype(int))
    for fam in (FADE_VARIANTS, US_FADE_VARIANTS, LONG_BOUNCE_VARIANTS,
                IPO_VARIANTS):
        ranks |= {int(v["rank"]) for v in fam}
    # (ticker, rank) pairs - a rank alone is NOT identity (ranks repeat
    # across markets); a ledger row is valid only if its ticker+rank
    # matches a deployed def.
    pairs = set()
    for d0 in (sw, it):
        for _, r in d0.iterrows():
            y = get_yf_ticker(str(r["Market"])) or str(r["Market"])
            pairs.add((str(y), int(r["Final_Rank"])))
    return ranks, pairs


def main():
    os.makedirs(ARCH, exist_ok=True)
    stamp = datetime.utcnow().strftime("%Y%m%d_%H%M")

    # ---- 1. ledger: drop combo rows with dead ranks -------------------
    df = pd.read_csv(LEDGER, on_bad_lines="warn")
    n0 = len(df)
    df.to_csv(os.path.join(ARCH, f"paper_trades_full_backup_{stamp}.csv"),
              index=False)

    tf = df["TimeFrame"].astype(str)
    ranks, pairs = active_ranks()
    rk_num = pd.to_numeric(df["Pattern_Rank"], errors="coerce")
    is_combo = tf.isin(["SWING_1d", "INTRADAY_1h"])
    pair_ok = [(str(a), int(b)) in pairs
               if pd.notna(b) else False
               for a, b in zip(df["Ticker"].astype(str), rk_num)]
    dead_rank = (~rk_num.isin(ranks)) | rk_num.isna() | \
                ~pd.Series(pair_ok, index=df.index)
    mask = is_combo & dead_rank
    retired = df[mask].copy()
    kept = df[~mask].copy()
    assert (retired["Status"].astype(str) == "OPEN").sum() == 0, \
        "refusing: open position among retired rows!"

    arch_csv = os.path.join(ARCH, f"paper_trades_removed_defs_{stamp}.csv")
    retired.to_csv(arch_csv, index=False)
    kept.to_csv(LEDGER, index=False)
    print(f"[clean] ledger {n0} -> {len(kept)} rows "
          f"(retired {len(retired)} -> {os.path.basename(arch_csv)})")
    print("[clean] retired tickers:",
          retired.groupby(["Ticker", "Pattern_Rank"]).size().to_dict())

    tot_before = pd.to_numeric(df["P&L"], errors="coerce").fillna(0).sum()
    tot_kept = pd.to_numeric(kept["P&L"], errors="coerce").fillna(0).sum()
    tot_ret = pd.to_numeric(retired["P&L"], errors="coerce").fillna(0).sum()
    ok = abs((tot_kept + tot_ret) - tot_before) < 1.0
    print(f"[clean] RECON Rs {tot_before:,.2f} = kept Rs {tot_kept:,.2f} "
          f"+ retired Rs {tot_ret:,.2f} -> {'MATCH' if ok else 'MISMATCH'}")
    if not ok:
        raise SystemExit("RECON FAILED - restoring backup")
    # backup already saved above; ledger rewrite is safe past this point

    # ---- 2. strategy_stats.json prune ---------------------------------
    try:
        stats = json.load(open(STATS, encoding="utf-8"))
    except FileNotFoundError:
        stats = None
    if stats:
        s0 = len(stats)
        pruned = {}
        for k, v in stats.items():
            try:
                r = int(str(k).split("_")[-1])
            except ValueError:
                pruned[k] = v          # non-rank key, keep
                continue
            if r in ranks:
                pruned[k] = v
        bpath = os.path.join(ARCH, f"strategy_stats_backup_{stamp}.json")
        json.dump(stats, open(bpath, "w", encoding="utf-8"), indent=2)
        json.dump(pruned, open(STATS, "w", encoding="utf-8"), indent=2)
        print(f"[clean] strategy_stats {s0} -> {len(pruned)} entries "
              f"(backup -> {os.path.basename(bpath)})")

    # ---- 3. portfolio rebuild ------------------------------------------
    def _valid_pos(p):
        if p.get("TimeFrame") not in ("SWING_1d", "INTRADAY_1h"):
            return True
        pr = str(p.get("Pattern_Rank", ""))
        if not pr.isdigit():
            return False
        return (str(p.get("Ticker")), int(pr)) in pairs

    port = load_portfolio()
    port["open_positions"] = [p for p in port.get("open_positions", [])
                              if _valid_pos(p)]
    save_portfolio(port)
    rebuilt = rebuild_portfolio_from_csv()
    print(f"[clean] portfolio rebuilt: P&L {rebuilt.get('total_pnl'):+.2f} | "
          f"closed {rebuilt.get('closed_count')} | W/L "
          f"{rebuilt.get('total_wins')}/{rebuilt.get('total_losses')} | "
          f"open {len(rebuilt.get('open_positions', []))}")


if __name__ == "__main__":
    main()

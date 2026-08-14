"""Remove duplicate trade rows from paper_trades.csv (and other log CSVs).

Union-merge (git merge=union) can duplicate rows when two workflows update
the same trade row concurrently (e.g. bot + live_pnl both exit the same
trade, each writing its own reason string "Target Hit" vs "🎯 Target Hit (live)").

Dedupe key = trade identity (Date, Time_IST, Ticker, Direction, Entry_Price,
Qty, Exit_Price, Status). Rows that match on all of these are the SAME
physical trade logged twice (only the Reason column differs) -> keep first.
Rows with different qty/entry/exit are legitimately distinct trades (e.g.
same ticker traded 3x with qty 116/119/121) -> untouched.

SAFETY: data/ strategy files (strategies.csv, intraday_strategies.csv,
*_fade_universe.csv) are NEVER deduped. They are read-only config for the
bot, and some of their columns (e.g. Direction) overlap with the trade
identity key, which would collapse the whole strategy list to 2 rows
(2026-08-14: dedupe ran on all *.csv after a fallback merge and truncated
strategies.csv 228 -> 2, cutting the live scanner from 228 to 2 strategies).

Usage: python .ai/dedupe_csv.py [file...]
"""
import sys

import pandas as pd

KEY_COLS = ["Date", "Time_IST", "Ticker", "Direction", "Entry_Price",
            "Qty", "Exit_Price", "Status"]

# Strategy/universe definition files — read-only config, never trade logs.
# Never dedupe them (see SAFETY note above).
PROTECTED_PREFIXES = ("data/",)


def dedupe(path: str) -> int:
    norm = path.replace("\\", "/")
    if norm.startswith(PROTECTED_PREFIXES):
        print(f"[dedupe] skip (protected) {path}")
        return 0
    try:
        df = pd.read_csv(path, on_bad_lines="warn")
    except Exception as e:
        print(f"[dedupe] skip {path}: {e}")
        return 0
    if len(df) == 0:
        return 0
    before = len(df)
    cols = [c for c in KEY_COLS if c in df.columns]
    if not cols:
        return 0
    df = df.copy()
    for c in cols:
        if c in ("Entry_Price", "Exit_Price", "Qty"):
            try:
                df[c] = pd.to_numeric(df[c], errors="coerce")
            except Exception:
                pass
        else:
            df[c] = df[c].astype(str).str.strip()
    df = df.drop_duplicates(subset=cols, keep="first")
    after = len(df)
    if after < before:
        df.to_csv(path, index=False)
        print(f"[dedupe] {path}: removed {before - after} duplicate trade rows ({before} -> {after})")
    return before - after


if __name__ == "__main__":
    total = 0
    for p in sys.argv[1:]:
        total += dedupe(p)
    print(f"[dedupe] done, {total} rows removed")

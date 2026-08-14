#!/usr/bin/env bash
# Robust log commit for concurrent paper-trade workflows.
# - *.csv files merge with union (both concurrent appends survive)
# - retries push; on failure pulls --rebase; falls back to a merge
# This prevents the "could not apply <commit>... failed to push" failures
# that happen when bot/fade/gapdown/live_pnl workflows commit logs at the same time.
#
# PROTECTED FILES (read-only for the bot):
#   data/strategies.csv, data/intraday_strategies.csv,
#   data/nse_fade_universe.csv, data/us_fade_universe.csv
# These are strategy definitions/universe lists — only ever changed by a
# deliberate strategy update, NEVER by a bot run. A stale/truncated runner
# copy must never win a merge conflict, or the live scanner silently loses
# its strategy set (2026-08-14: dd386ad truncated strategies.csv 1039 -> 23
# lines and cut the scanner from 228 to 2 strategies). We therefore force
# these files back to origin/main after ANY conflict resolution so the
# remote's version always wins.
set -u
MSG="${1:-scan $(date -u +'%Y-%m-%d %H:%M UTC')}"

git config --local user.email "bot@paper-trade.com" 2>/dev/null || true
git config --local user.name "paper-trade-bot" 2>/dev/null || true

# Strategy/universe definition files — source of truth is origin/main.
STRATEGY_FILES="data/strategies.csv data/intraday_strategies.csv data/nse_fade_universe.csv data/us_fade_universe.csv"

# Restore protected files from origin/main (or HEAD if origin ref missing).
# The runner only ever READS these; any local divergence is corruption from
# a bad merge, so the remote version always wins.
protect_strategy_files() {
  if git rev-parse --verify origin/main >/dev/null 2>&1; then
    git checkout origin/main -- $STRATEGY_FILES 2>/dev/null || true
  else
    git checkout HEAD -- $STRATEGY_FILES 2>/dev/null || true
  fi
}

for attempt in 1 2 3 4 5; do
  protect_strategy_files
  git add -A 2>/dev/null || true
  if git diff --staged --quiet; then
    echo "[commit-log] no changes to commit"
    exit 0
  fi
  git commit -qm "$MSG" || true
  if git push origin HEAD 2>/dev/null; then
    echo "[commit-log] pushed OK (attempt $attempt)"
    exit 0
  fi
  echo "[commit-log] push failed (attempt $attempt), syncing with origin..."
  sleep $((attempt * 3))
  # Try rebase (union handles *.csv; JSON files are now mode-specific so no clash)
  if git pull --rebase 2>/dev/null; then
    echo "[commit-log] rebase OK, retrying push"
    continue
  fi
  echo "[commit-log] rebase conflicted, falling back to merge (keep-ours for logs)"
  git rebase --abort 2>/dev/null || true
  git merge --no-edit -X ours origin/main 2>/dev/null || {
    git checkout --ours . 2>/dev/null || true
    git add -A 2>/dev/null || true
  }
  # CRITICAL: -X ours / checkout --ours resolves conflicts toward the runner's
  # local copy. Strategy files are read-only config — force the remote's version
  # so a stale/truncated copy can never be committed (see header note).
  protect_strategy_files
  git add -A 2>/dev/null || true
  # Union-merge can leave exact duplicate rows in CSVs that get updated
  # in-place (paper_trades.csv exits). Remove them before committing so
  # P&L is never double-counted. Note: dedupe_csv.py itself skips data/
  # (strategy files) — it must never touch strategy definitions.
  git ls-files -z '*.csv' 2>/dev/null | xargs -0 -r python .ai/dedupe_csv.py 2>/dev/null || true
  protect_strategy_files
  git add -A 2>/dev/null || true
  git commit -qm "$MSG (merged)" || true
  git push origin HEAD 2>/dev/null && { echo "[commit-log] pushed OK after merge"; exit 0; }
  sleep 3
done

echo "[commit-log] PUSH STILL FAILING after 5 attempts - logs may be stale on remote"
exit 1

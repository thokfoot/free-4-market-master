#!/usr/bin/env bash
# Robust log commit for concurrent paper-trade workflows.
# - *.csv files merge with union (both concurrent appends survive)
# - retries push; on failure pulls --rebase; falls back to a merge
# This prevents the "could not apply <commit>... failed to push" failures
# that happen when bot/fade/gapdown/live_pnl workflows commit logs at the same time.
set -u
MSG="${1:-scan $(date -u +'%Y-%m-%d %H:%M UTC')}"

git config --local user.email "bot@paper-trade.com" 2>/dev/null || true
git config --local user.name "paper-trade-bot" 2>/dev/null || true

for attempt in 1 2 3 4 5; do
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
  echo "[commit-log] rebase conflicted, falling back to merge (keep-ours for non-csv)"
  git rebase --abort 2>/dev/null || true
  git merge --no-edit -X ours origin/main 2>/dev/null || {
    git checkout --ours . 2>/dev/null || true
    git add -A 2>/dev/null || true
  }
  git commit -qm "$MSG (merged)" || true
  git push origin HEAD 2>/dev/null && { echo "[commit-log] pushed OK after merge"; exit 0; }
  sleep 3
done

echo "[commit-log] PUSH STILL FAILING after 5 attempts - logs may be stale on remote"
exit 1

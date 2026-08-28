"""Keep-alive guard for the LIVE P&L workflow.

GitHub Actions cron is unreliable (2026-08-11: 75 min with zero scheduled
runs while 7 gap-down scalps were open — all rode through SL to expiry).
The live_pnl.yml keep-alive step calls this module on every run:

    if python keepalive_guard.py; then
        sleep 120
        gh workflow run "LIVE P&L v5.10 (Market Hours)"
    else
        echo "[KeepAlive] chain stopped"
    fi

Exit codes:
    0  → RESCHEDULE (time-sensitive positions open and their market active)
    9  → STOP the chain (nothing time-sensitive, or markets closed)
"""

import json
import sys
from datetime import datetime
import zoneinfo

from config import market_active_for_mode

LOG_DIR = "logs"
PORTFOLIO_FILE = f"{LOG_DIR}/portfolio.json"

# Only these intraday frames need a fast self-healing loop. Swing positions
# (multi-day holds, wider SL) are covered by the regular cron + health self-heal.
# v5.23 fix: include IPO_1d so a missed daily scan that leaves an IPO position
# without an exit check still self-heals via live_pnl.
TIME_SENSITIVE_TFS = ("GAP_DOWN_1m", "INTRADAY_1h", "FADE_1h", "US_FADE_5m", "LONG_BOUNCE_5m", "IPO_1d")


def should_reschedule(opens, now) -> tuple:
    """Return (go: bool, reason: str).

    Market windows come from config.market_active_for_mode — the same single
    source of truth the live updater itself uses, so the keep-alive chain can
    never reschedule for a market the updater would refuse to process.
    (Fixes the old hand-rolled windows that treated Sat/Sun 00:00-03:00 IST
    as a US session and Sat/Sun 15:00-15:35 IST as an India session.)
    """
    ts = [o for o in opens if str(o.get("TimeFrame", "")) in TIME_SENSITIVE_TFS]
    if not ts:
        return False, "no time-sensitive positions"
    modes = sorted({str(o.get("Mode", "")).upper() for o in ts})
    active_modes = []
    for m in modes:
        active, why = market_active_for_mode(m, now)
        if active:
            active_modes.append(f"{m} ({why})")
    if not active_modes:
        return False, f"markets closed for {modes}"
    return True, f"{len(ts)} time-sensitive positions {active_modes}"


def main():
    try:
        with open(PORTFOLIO_FILE, encoding="utf-8") as f:
            opens = json.load(f).get("open_positions", [])
    except Exception:
        opens = []
    go, why = should_reschedule(
        opens, datetime.now(zoneinfo.ZoneInfo("Asia/Kolkata"))
    )
    if go:
        print(f"[KeepAlive] {why} — reschedule in 120s")
        sys.exit(0)
    print(f"[KeepAlive] {why} — stop chain")
    sys.exit(9)


if __name__ == "__main__":
    main()

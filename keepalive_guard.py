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

LOG_DIR = "logs"
PORTFOLIO_FILE = f"{LOG_DIR}/portfolio.json"

# Only these intraday frames need a fast self-healing loop. Swing positions
# (multi-day holds, wider SL) are covered by the regular cron scans.
TIME_SENSITIVE_TFS = ("GAP_DOWN_1m", "INTRADAY_1h", "FADE_1h", "US_FADE_5m", "LONG_BOUNCE_5m")


def should_reschedule(opens, now) -> tuple:
    """Return (go: bool, reason: str)."""
    ts = [o for o in opens if str(o.get("TimeFrame", "")) in TIME_SENSITIVE_TFS]
    if not ts:
        return False, "no time-sensitive positions"
    modes = {str(o.get("Mode", "")).upper() for o in ts}
    wd, hh, mm = now.weekday(), now.hour, now.minute
    # India market window (IST): Mon-Fri 08:45-15:35
    in_india = (wd < 5 and 8 <= hh < 15) or (hh == 15 and mm <= 35)
    # US market window (IST): Mon 18:00 - Sat 02:30
    in_us = (wd < 5 and hh >= 18) or hh < 3
    active = (("INDIAN" in modes and in_india) or ("US" in modes and in_us)
              or bool(modes & {"CRYPTO", "INTRADAY"}))
    if not active:
        return False, f"markets closed for {sorted(modes)}"
    return True, f"{len(ts)} time-sensitive positions {sorted(modes)}"


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

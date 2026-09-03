#!/usr/bin/env python3
"""
KILL SWITCH — Emergency halt for FREE 4-Market trading system.
================================================================
File-system based (data/kill.flag). No config reload / restart needed.
When data/kill.flag exists, paper_trader.check_entry_allowed() blocks
ALL new entries instantly (checked per-entry, not per-process-start).

Usage:
    python kill_switch.py --status            # Check if kill switch is active
    python kill_switch.py --activate          # Halt ALL new entries now
    python kill_switch.py --activate --reason "flash crash"
    python kill_switch.py --deactivate        # Allow new entries again

Note:
    - Existing open positions are NOT auto-flattened (paper trading).
      The flag only blocks NEW entries.
    - Safe to re-run (idempotent).
"""

import argparse
import sys
from pathlib import Path
from datetime import datetime, timezone

FLAG = Path(__file__).resolve().parent / "data" / "kill.flag"


def is_killed() -> bool:
    """Return True if the kill switch is active (flag file exists)."""
    return FLAG.exists()


def activate(reason: str = "manual") -> None:
    """Create the kill flag to halt all new entries."""
    FLAG.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    content = f"killed_at={timestamp}\nreason={reason}\n"
    FLAG.write_text(content, encoding="utf-8")
    print(f"[KILL] ACTIVATED — all new entries blocked")
    print(f"       {FLAG}")
    print(f"       {content.strip()}")
    print(f"To resume: python kill_switch.py --deactivate")


def deactivate() -> None:
    """Remove the kill flag to allow new entries again."""
    if FLAG.exists():
        FLAG.unlink()
        print("[KILL] DEACTIVATED — new entries allowed again")
    else:
        print("[KILL] Already inactive (no flag present)")


def status() -> int:
    """Print status and return exit code (1 if active, 0 if not)."""
    if is_killed():
        content = FLAG.read_text(encoding="utf-8").strip()
        print("[KILL] ACTIVE")
        print(f"       {FLAG}")
        if content:
            print(f"       {content}")
        return 1
    print("[KILL] INACTIVE — trading allowed")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Emergency kill switch for FREE 4-Market trading system")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--activate", action="store_true",
                       help="Halt ALL new entries (create data/kill.flag)")
    group.add_argument("--deactivate", action="store_true",
                       help="Allow new entries again (remove data/kill.flag)")
    group.add_argument("--status", action="store_true",
                       help="Check if kill switch is active")
    parser.add_argument("--reason", default="manual",
                        help="Reason, recorded in the flag file (for --activate)")
    args = parser.parse_args()

    if args.status:
        return status()
    if args.activate:
        activate(args.reason)
    elif args.deactivate:
        deactivate()
    return 0


if __name__ == "__main__":
    sys.exit(main())

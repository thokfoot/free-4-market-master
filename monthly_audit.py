"""Create a Git-tracked monthly paper-trading verification summary."""
import json
import os
from datetime import datetime, timezone

import pandas as pd

from audit_events import EVENT_FILE, backfill_legacy_events, load_event_ids
from integrity_check import PAPER_FILE, validate_all


def run(output=None):
    output = output or os.path.join(
        os.path.dirname(PAPER_FILE),
        f"monthly_audit_{datetime.now(timezone.utc):%Y-%m}.json",
    )
    legacy_added = backfill_legacy_events(PAPER_FILE)
    ledger = pd.read_csv(PAPER_FILE, on_bad_lines="error")
    failures = validate_all()
    closed = ledger[ledger["Status"].astype(str).str.upper() == "CLOSED"]
    pnl = pd.to_numeric(closed["P&L"], errors="coerce").fillna(0)
    result = {
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "period": {"from": str(ledger["Date"].min()), "to": str(ledger["Date"].max())},
        "ledger_rows": len(ledger),
        "closed": len(closed),
        "open": int((ledger["Status"].astype(str).str.upper() == "OPEN").sum()),
        "wins": int((pnl > 0).sum()),
        "losses": int((pnl < 0).sum()),
        "net_pnl": round(float(pnl.sum()), 2),
        "signal_event_rows": len(load_event_ids()),
        "legacy_events_backfilled": legacy_added,
        "candle_evidence_dir": os.path.join(os.path.dirname(PAPER_FILE), "candle_evidence"),
        "integrity": "OK" if not failures else "FAIL",
        "integrity_failures": failures,
        "event_file": EVENT_FILE,
    }
    with open(output, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2))
    if failures:
        raise SystemExit(1)
    return output


if __name__ == "__main__":
    run()
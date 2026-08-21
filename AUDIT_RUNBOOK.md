# Paper-trade audit contract

Every new paper entry must have:

- a row in `logs/paper_trades.csv`
- an immutable entry record in `logs/signal_events.jsonl`
- signal indicators with provider provenance and a snapshot hash
- fetched bars archived under `logs/candle_evidence/` when workflows run
- a matching row in `logs/strategy_report.xlsx`

Every state-writing workflow runs `integrity_check.py --report` before commit.
If the ledger, Excel, portfolio reconciliation, manifest, or event evidence is inconsistent, the workflow fails and does not publish a success state.

## Monthly verification

From the repository root:

```text
python monthly_audit.py
```

The command writes `logs/monthly_audit_YYYY-MM.json`. Legacy trades are marked as legacy and are not treated as freshly verified. New trades must have a matching event record and archived market evidence.

## Reproducibility

The report source manifest records the ledger SHA-256, ledger row count, latest trade date, and Git commit. The repository history plus the event log and candle evidence are the audit trail. Do not delete or rewrite these files.
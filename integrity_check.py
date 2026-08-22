"""Fail-closed consistency checks for the paper-trade ledger and Excel report."""
import argparse
import hashlib
import json
import math
import os
from collections import Counter

import pandas as pd
from openpyxl import load_workbook

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_DIR = os.path.join(BASE_DIR, "logs")
PAPER_FILE = os.path.join(LOG_DIR, "paper_trades.csv")
PORTFOLIO_FILE = os.path.join(LOG_DIR, "portfolio.json")
REPORT_FILE = os.path.join(LOG_DIR, "strategy_report.xlsx")
MANIFEST_FILE = os.path.join(LOG_DIR, "strategy_report_source.json")
EVENT_FILE = os.path.join(LOG_DIR, "signal_events.jsonl")
REQUIRED = [
    "Date", "Time_IST", "Mode", "Ticker", "Direction", "TimeFrame",
    "Entry_Price", "Qty", "SL", "Target", "MaxHold", "Exit_Price",
    "Exit_Time", "P&L", "P&L_%", "Status", "Pattern_Rank",
    "Expected_WinRate", "Pattern_Factors", "Reason", "Signal_Indicators",
]


def _ledger_sha256():
    """Hash normalized ledger bytes so Windows and CI agree on provenance."""
    with open(PAPER_FILE, "rb") as handle:
        return hashlib.sha256(handle.read().replace(b"\r\n", b"\n")).hexdigest()


def _norm(value):
    text = "" if pd.isna(value) else str(value).strip()
    try:
        number = float(text)
        return str(int(number)) if number.is_integer() else text
    except (TypeError, ValueError):
        return text


def trade_key(row):
    entry = row.get("Entry_Price", row.get("Entry", ""))
    qty = row.get("Qty", "")
    return tuple(_norm(value) for value in
                 (row.get("Date", ""), row.get("Time_IST", ""),
                  row.get("Ticker", ""), row.get("Direction", ""),
                  row.get("TimeFrame", ""), row.get("Pattern_Rank", ""),
                  entry, qty))


def _number(value, field, row_number, errors):
    try:
        result = float(value)
        if not math.isfinite(result):
            raise ValueError("not finite")
        return result
    except (TypeError, ValueError):
        errors.append(f"row {row_number}: {field} is not finite: {value!r}")
        return 0.0


def validate_ledger():
    errors = []
    if not os.path.exists(PAPER_FILE):
        return [f"missing ledger: {PAPER_FILE}"], None
    df = pd.read_csv(PAPER_FILE, on_bad_lines="error")
    missing = [column for column in REQUIRED if column not in df.columns]
    errors.extend(f"missing ledger column: {column}" for column in missing)
    if missing:
        return errors, None
    keys = [trade_key(row) for _, row in df.iterrows()]
    duplicates = [key for key, count in Counter(keys).items() if count > 1]
    errors.extend(f"duplicate trade row: {key!r}" for key in duplicates)
    for index, row in df.iterrows():
        row_number = index + 2
        status = str(row["Status"]).strip().upper()
        if status not in ("OPEN", "CLOSED"):
            errors.append(f"row {row_number}: invalid Status={status!r}")
        entry = _number(row["Entry_Price"], "Entry_Price", row_number, errors)
        qty = _number(row["Qty"], "Qty", row_number, errors)
        sl = _number(row["SL"], "SL", row_number, errors)
        target = _number(row["Target"], "Target", row_number, errors)
        if entry <= 0 or qty <= 0 or sl <= 0 or target <= 0:
            errors.append(f"row {row_number}: non-positive trade price/quantity")
        if status == "CLOSED":
            _number(row["Exit_Price"], "Exit_Price", row_number, errors)
            _number(row["P&L"], "P&L", row_number, errors)
            _number(row["P&L_%"], "P&L_%", row_number, errors)
            if not str(row["Exit_Time"]).strip():
                errors.append(f"row {row_number}: CLOSED trade has no Exit_Time")
    return errors, df


def validate_report(ledger):
    errors = []
    if not os.path.exists(REPORT_FILE):
        return [f"missing report: {REPORT_FILE}"]
    try:
        report = pd.read_excel(REPORT_FILE, sheet_name="All Trades")
    except Exception as exc:
        return [f"cannot read report: {exc}"]
    report = report[report["Status"].astype(str).str.upper().isin(("OPEN", "CLOSED"))]
    ledger_keys = Counter(trade_key(row) for _, row in ledger.iterrows())
    report_keys = Counter(trade_key(row) for _, row in report.iterrows())
    errors.extend(f"report row mismatch: {key!r}" for key in (ledger_keys - report_keys))
    errors.extend(f"ledger row missing from report: {key!r}" for key in (report_keys - ledger_keys))
    ledger_closed = ledger[ledger["Status"].astype(str).str.upper() == "CLOSED"]
    report_closed = report[report["Status"].astype(str).str.upper() == "CLOSED"]
    ledger_pnl = pd.to_numeric(ledger_closed["P&L"], errors="coerce").sum()
    report_pnl = pd.to_numeric(report_closed["Net P&L"], errors="coerce").sum()
    if abs(float(ledger_pnl) - float(report_pnl)) > 0.05:
        errors.append(f"report P&L mismatch: ledger={ledger_pnl:.2f} report={report_pnl:.2f}")
    if os.path.exists(MANIFEST_FILE):
        try:
            manifest = json.load(open(MANIFEST_FILE, encoding="utf-8"))
            digest = _ledger_sha256()
            if manifest.get("ledger_sha256") != digest:
                errors.append("report manifest ledger_sha256 does not match current ledger")
            if int(manifest.get("ledger_rows", -1)) != len(ledger):
                errors.append("report manifest ledger_rows does not match current ledger")
        except Exception as exc:
            errors.append(f"invalid report manifest: {exc}")
    else:
        errors.append("missing report source manifest")
    return errors


def validate_all():
    errors, ledger = validate_ledger()
    if ledger is not None:
        if os.path.exists(EVENT_FILE):
            try:
                from audit_events import _event_id, load_event_ids
                event_ids = load_event_ids()
                legacy = 0
                for _, row in ledger.iterrows():
                    if _event_id(row.to_dict()) not in event_ids:
                        legacy += 1
                if legacy:
                    print(f"[Integrity] {legacy} legacy trades have no event record")
            except Exception as exc:
                errors.append(f"invalid signal event log: {exc}")
        else:
            errors.append("missing signal event log")
        errors.extend(validate_report(ledger))
        if os.path.exists(PORTFOLIO_FILE):
            try:
                portfolio = json.load(open(PORTFOLIO_FILE, encoding="utf-8"))
                closed = ledger[ledger["Status"].astype(str).str.upper() == "CLOSED"]
                pnl = pd.to_numeric(closed["P&L"], errors="coerce").sum()
                wins = int((pd.to_numeric(closed["P&L"], errors="coerce") > 0).sum())
                losses = int((pd.to_numeric(closed["P&L"], errors="coerce") < 0).sum())
                checks = {"total_pnl": float(pnl), "total_wins": wins,
                          "total_losses": losses, "closed_count": len(closed)}
                for field, expected in checks.items():
                    actual = float(portfolio.get(field, 0))
                    if abs(actual - expected) > 0.05:
                        errors.append(f"portfolio {field} mismatch: {actual} != {expected}")
                if len(portfolio.get("open_positions", [])) != int((ledger["Status"].astype(str).str.upper() == "OPEN").sum()):
                    errors.append("portfolio open_positions count does not match ledger")
            except Exception as exc:
                errors.append(f"invalid portfolio: {exc}")
        else:
            errors.append("missing portfolio reconciliation artifact")
    return errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", action="store_true", help="validate ledger, portfolio, and Excel")
    args = parser.parse_args()
    failures = validate_all()
    if failures:
        print("INTEGRITY_FAIL")
        for failure in failures:
            print(f"- {failure}")
        raise SystemExit(1)
    print("INTEGRITY_OK")

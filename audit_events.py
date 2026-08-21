"""Durable event-level evidence for every newly created paper trade."""
import hashlib
import json
import os
from datetime import datetime

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "logs")
EVENT_FILE = os.path.join(LOG_DIR, "signal_events.jsonl")


def _event_id(trade):
    def clean(value):
        try:
            if value != value:
                return ""
        except Exception:
            pass
        return "" if value is None else str(value).strip()

    identity = {
        "Date": clean(trade.get("Date", "")),
        "Time_IST": clean(trade.get("Time_IST", "")),
        "Ticker": clean(trade.get("Ticker", "")),
        "Direction": clean(trade.get("Direction", "")),
        "TimeFrame": clean(trade.get("TimeFrame", "")),
        "Pattern_Rank": clean(trade.get("Pattern_Rank", "")),
        "Entry_Price": clean(trade.get("Entry_Price", "")),
        "Qty": clean(trade.get("Qty", "")),
    }
    return hashlib.sha256(json.dumps(identity, sort_keys=True, default=str).encode()).hexdigest()


def append_signal_event(trade):
    """Append one immutable signal/entry evidence record."""
    os.makedirs(LOG_DIR, exist_ok=True)
    event = {
        "event": "ENTRY",
        "event_id": _event_id(trade),
        "recorded_at_utc": datetime.utcnow().isoformat(timespec="seconds") + "Z",
        "trade": {
            key: trade.get(key, "") for key in (
                "Date", "Time_IST", "Mode", "Ticker", "Direction", "TimeFrame",
                "Entry_Price", "Qty", "SL", "Target", "MaxHold", "Pattern_Rank",
                "Expected_WinRate", "Pattern_Factors", "Signal_Indicators", "Reason",
            )
        },
        "evidence_contract": {
            "provider": "market_data.download",
            "provider_chain": ["yfinance", "yahoo_chart", "nasdaq_or_binance", "ohlc_cache"],
            "signal_is_persisted": True,
            "raw_candle_archive_required_for_new_runs": True,
        },
    }
    with open(EVENT_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(event, sort_keys=True, default=str) + "\n")
    return event["event_id"]


def load_event_ids():
    if not os.path.exists(EVENT_FILE):
        return set()
    ids = set()
    with open(EVENT_FILE, encoding="utf-8") as f:
        for line in f:
            try:
                item = json.loads(line)
                if item.get("event_id"):
                    ids.add(item["event_id"])
            except json.JSONDecodeError:
                continue
    return ids


def backfill_legacy_events(csv_path):
    """Record existing trades as legacy evidence, never as newly verified signals."""
    import csv
    existing = load_event_ids()
    count = 0
    with open(csv_path, encoding="utf-8-sig", newline="") as f:
        for trade in csv.DictReader(f):
            event_id = _event_id(trade)
            if event_id in existing:
                continue
            os.makedirs(LOG_DIR, exist_ok=True)
            event = {
                "event": "LEGACY_ENTRY",
                "event_id": event_id,
                "recorded_at_utc": None,
                "trade": {key: trade.get(key, "") for key in trade},
                "evidence_contract": {
                    "provider": "unknown_legacy_source",
                    "signal_verified": False,
                    "legacy_backfill": True,
                },
            }
            with open(EVENT_FILE, "a", encoding="utf-8") as out:
                out.write(json.dumps(event, sort_keys=True, default=str) + "\n")
            existing.add(event_id)
            count += 1
    return count

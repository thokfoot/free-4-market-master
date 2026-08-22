import hashlib

import integrity_check
import strategy_report


def test_ledger_provenance_hash_is_line_ending_independent(tmp_path, monkeypatch):
    ledger = tmp_path / "paper_trades.csv"
    ledger.write_bytes(b"Date,Status\r\n2026-08-22,OPEN\r\n")
    monkeypatch.setattr(integrity_check, "PAPER_FILE", str(ledger))
    monkeypatch.setattr(strategy_report, "PAPER_FILE", str(ledger))
    expected = hashlib.sha256(b"Date,Status\n2026-08-22,OPEN\n").hexdigest()
    assert integrity_check._ledger_sha256() == expected

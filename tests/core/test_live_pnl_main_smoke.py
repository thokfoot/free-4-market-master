"""Smoke test: live_pnl_updater.main() must run end-to-end without NameError.

Catches the 2026-08-25 incident: main() printed a counter that lived inside
process_open_trades() scope -> NameError AFTER processing -> job failed ->
zero Telegram messages + stale Excel. Any new variable used in main() must
exist in main()'s own scope."""
import live_pnl_updater as lp


def test_live_pnl_main_runs_clean(monkeypatch):
    sent = []
    monkeypatch.setattr(lp, "initialize_system", lambda: None)
    monkeypatch.setattr(lp, "_load_live_state", lambda: {"last_tg": {}, "last_pnl": {}})
    monkeypatch.setattr(lp, "_save_live_state", lambda s: None)
    monkeypatch.setattr(lp, "process_open_trades", lambda: ([], []))
    monkeypatch.setattr(lp, "_commit_state_now", lambda: None)
    monkeypatch.setattr(lp, "send_telegram", lambda msg: sent.append(msg) or "Sent")
    monkeypatch.setattr(lp, "generate_strategy_report", lambda: None)
    monkeypatch.setattr(lp, "validate_all", lambda: [])

    lp.main()  # must not raise

    assert sent == []  # nothing happened -> nothing messaged

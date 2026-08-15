"""
BOT HEARTBEAT — Missed-Run Detection (v5.22)
==============================================
Detects when the paper-trade bot has gone silent and alerts via Telegram.

How it works:
  - The bot writes a heartbeat line into logs/portfolio_snapshots.csv on
    every successful run (bot.py / live_pnl_updater.py both call log_portfolio).
  - This script (run by .github/workflows/health.yml on a schedule) reads the
    LAST snapshot's Date+Time and compares against now.
  - If the gap exceeds a threshold while any market should be producing runs,
    it sends a Telegram alert ("BOT DEAD"). The alert state is stored in
    logs/health_state.json so we only alert once per incident (no spam).

Thresholds (minutes since last heartbeat):
  - ANY market open (India/US/crypto): 150 min  (bot runs every ~5-30 min)
  - All markets closed (weekend/after hours): 12 hours (still expect crypto runs)
"""
import os, json, sys, time
from datetime import datetime, timedelta

LOG_DIR = "logs"
SNAPSHOT_FILE = os.path.join(LOG_DIR, "portfolio_snapshots.csv")
LIVE_PNL_FILE = os.path.join(LOG_DIR, "live_pnl_snapshots.csv")
STATE_FILE = os.path.join(LOG_DIR, "health_state.json")

OPEN_THRESHOLD_MIN = 150        # any market open
CLOSED_THRESHOLD_MIN = 12 * 60  # all markets closed (crypto still runs)

IST_OFFSET = timedelta(hours=5, minutes=30)  # UTC -> IST


def now_ist() -> datetime:
    return datetime.utcnow() + IST_OFFSET


def parse_snapshot_time(line: str) -> datetime:
    """Parse '2026-08-15,12:01:01 IST,...' -> IST datetime (date-only tolerant)."""
    try:
        parts = line.split(",")
        date_s = parts[0].strip()
        time_s = parts[1].strip().replace(" IST", "")
        return datetime.strptime(f"{date_s} {time_s}", "%Y-%m-%d %H:%M:%S")
    except Exception:
        return None


def _scan_file(filepath: str) -> datetime:
    """Parse the newest timestamp from a snapshot CSV (Date,Time IST prefix)."""
    if not os.path.exists(filepath):
        return None
    last = None
    with open(filepath, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("Date,"):
                continue
            ts = parse_snapshot_time(line)
            if ts is not None:
                last = ts
    return last


def last_heartbeat() -> datetime:
    """Return the newest heartbeat from bot (portfolio_snapshots.csv) OR
    live-P&L (live_pnl_snapshots.csv, runs every 5-30 min during market
    hours). Whichever is freshest = the bot is alive."""
    a = _scan_file(SNAPSHOT_FILE)
    b = _scan_file(LIVE_PNL_FILE)
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def market_open_now(now: datetime) -> bool:
    """Crude market-open check. Crypto is 24/7 so return True unless weekend
    AND all equity markets closed. Conservative: weekends still have crypto."""
    return True  # crypto 24/7 -> heartbeat expected around the clock


def send_telegram(msg: str) -> None:
    token = os.getenv("TELEGRAM_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        print(f"[Health] Missing TG creds — would alert: {msg[:120]}")
        return
    try:
        import requests
        r = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=15)
        print(f"[Health] TG alert rc={r.status_code}")
    except Exception as e:
        print(f"[Health] TG alert failed: {e}")


def main() -> int:
    now = now_ist()
    hb = last_heartbeat()

    if hb is None:
        print(f"[Health] No heartbeat file found ({SNAPSHOT_FILE}) — CRITICAL")
        send_telegram("🚨 *BOT HEARTBEAT* — no heartbeat file found! "
                      "Bot may never have run.")
        return 1

    gap_min = (now - hb).total_seconds() / 60.0
    threshold = OPEN_THRESHOLD_MIN if market_open_now(now) else CLOSED_THRESHOLD_MIN
    stale = gap_min > threshold

    # Load alert state (avoid re-alerting every run while down)
    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                state = json.load(f)
        except Exception:
            state = {}

    alerted_ts = state.get("last_alert_ts")
    alert_age_min = (now - datetime.fromisoformat(alerted_ts)).total_seconds() / 60.0 \
        if alerted_ts else 999999

    if stale and alert_age_min > 180:  # re-alert at most every 3h while down
        msg = (f"🚨 *BOT DEAD?* — no run in {gap_min:.0f} min "
               f"(threshold {threshold:.0f} min)\n"
               f"Last heartbeat: {hb:%Y-%m-%d %H:%M} IST\n"
               f"Check GitHub Actions — scheduled runs may be failing.")
        send_telegram(msg)
        state["last_alert_ts"] = now.isoformat()
        state["last_gap_min"] = round(gap_min, 1)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2)
        print(f"[Health] ALERT sent (gap {gap_min:.0f} min)")
        return 1
    elif stale:
        print(f"[Health] Still stale ({gap_min:.0f} min) — already alerted, "
              f"next re-alert in {180 - alert_age_min:.0f} min")
        return 1
    else:
        # Healthy — clear the alert state so the next outage re-alerts
        if state:
            state.pop("last_alert_ts", None)
            state.pop("last_gap_min", None)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        print(f"[Health] OK — heartbeat {gap_min:.0f} min ago (threshold {threshold:.0f})")
        return 0


if __name__ == "__main__":
    sys.exit(main())

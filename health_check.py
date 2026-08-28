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
PORTFOLIO_FILE = os.path.join(LOG_DIR, "portfolio.json")
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
    """True when heartbeats are expected soon:
    - any equity market literally OPEN right now, or
    - open CRYPTO positions exist (crypto trades 24/7 so its live-P&L
      loop keeps writing snapshots around the clock).
    With no exposure and both equity markets shut (nights/weekends), the
    12-hour closed threshold applies instead of paging at night."""
    from config import get_market_status
    st = get_market_status(now)
    if st.get("INDIAN") == "OPEN" or st.get("US") == "OPEN":
        return True
    try:
        with open(PORTFOLIO_FILE, encoding="utf-8") as f:
            opens = json.load(f).get("open_positions", [])
        return any(str(o.get("Mode", "")).upper() in ("CRYPTO", "INTRADAY")
                   for o in opens)
    except Exception:
        return True  # can't read portfolio — stay conservative (alert-ready)


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


def trigger_workflow(workflow_file: str, ref: str = "main") -> bool:
    """Self-healing: trigger a workflow via GitHub API when heartbeat is stale."""
    gh_token = os.getenv("GH_TOKEN") or os.getenv("GITHUB_TOKEN")
    repo = os.getenv("GITHUB_REPOSITORY", "thokfoot/free-4-market-master")
    if not gh_token:
        try:
            import subprocess
            name_map = {
                "bot.yml": "FREE 3-Market v5.12 PAPER TRADE (SWING + INTRADAY + FADE)",
                "live_pnl.yml": "LIVE P&L v5.10 (Market Hours)",
                "fade_scan.yml": "FADE SCAN (5-min)",
                "gap_down.yml": "Gap-Down 1m Scan",
            }
            wf_name = name_map.get(workflow_file, workflow_file)
            r = subprocess.run(["gh", "workflow", "run", wf_name, "--ref", ref],
                               capture_output=True, text=True, timeout=15)
            if r.returncode == 0:
                print(f"[Health] gh trigger OK: {wf_name}")
                return True
            print(f"[Health] gh trigger failed {wf_name}: {r.stderr[:200]}")
        except Exception as e:
            print(f"[Health] gh trigger exception {workflow_file}: {e}")
        return False
    try:
        import requests
        url = f"https://api.github.com/repos/{repo}/actions/workflows/{workflow_file}/dispatches"
        r = requests.post(url,
                          headers={"Authorization": f"Bearer {gh_token}",
                                   "Accept": "application/vnd.github+json",
                                   "X-GitHub-Api-Version": "2022-11-28"},
                          json={"ref": ref},
                          timeout=15)
        if r.status_code in (204, 201):
            print(f"[Health] API trigger OK: {workflow_file} -> {r.status_code}")
            return True
        print(f"[Health] API trigger failed {workflow_file}: {r.status_code} {r.text[:200]}")
        try:
            import subprocess
            name_map = {
                "bot.yml": "FREE 3-Market v5.12 PAPER TRADE (SWING + INTRADAY + FADE)",
                "live_pnl.yml": "LIVE P&L v5.10 (Market Hours)",
                "fade_scan.yml": "FADE SCAN (5-min)",
                "gap_down.yml": "Gap-Down 1m Scan",
            }
            wf_name = name_map.get(workflow_file, workflow_file)
            r2 = subprocess.run(["gh", "workflow", "run", wf_name, "--ref", ref],
                                capture_output=True, text=True, timeout=15)
            if r2.returncode == 0:
                print(f"[Health] gh fallback OK: {wf_name}")
                return True
        except Exception:
            pass
        return False
    except Exception as e:
        print(f"[Health] trigger exception {workflow_file}: {e}")
        return False


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
        for wf in ["bot.yml", "live_pnl.yml", "fade_scan.yml", "gap_down.yml"]:
            ok = trigger_workflow(wf)
            try:
                time.sleep(1)
            except Exception:
                pass
            if ok:
                print(f"[Health] Self-heal triggered {wf} (gap {gap_min:.0f}m)")
        return 1
    elif stale:
        print(f"[Health] Still stale ({gap_min:.0f} min) — already alerted, "
              f"next re-alert in {180 - alert_age_min:.0f} min")
        last_heal = state.get("last_heal_ts")
        heal_age = (now - datetime.fromisoformat(last_heal)).total_seconds() / 60.0 if last_heal else 999999
        if heal_age > 35:
            print(f"[Health] Self-heal retry (heal_age {heal_age:.0f}m)")
            for wf in ["bot.yml", "live_pnl.yml"]:
                trigger_workflow(wf)
                try:
                    time.sleep(1)
                except Exception:
                    pass
            state["last_heal_ts"] = now.isoformat()
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        return 1
    else:
        # Healthy — clear the alert state so the next outage re-alerts
        if state:
            state.pop("last_alert_ts", None)
            state.pop("last_gap_min", None)
            state.pop("last_heal_ts", None)
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f, indent=2)
        print(f"[Health] OK — heartbeat {gap_min:.0f} min ago (threshold {threshold:.0f})")
        return 0


if __name__ == "__main__":
    sys.exit(main())

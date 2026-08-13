"""
Chat context auto-updater.
Usage:  python .ai/update_chat_log.py "heading" "bullet1" "bullet2" ...
Appends a dated session entry to logs/chat_context.md.
If no args, just refreshes the portfolio snapshot section (auto mode).
"""
import sys, os, json, datetime, subprocess

LOG = "logs/chat_context.md"

def refresh_snapshot():
    """Update CURRENT SNAPSHOT portfolio numbers from live files."""
    try:
        p = json.load(open("logs/portfolio.json"))
        cbm = p.get("capital_by_market", {})
        total = round(sum(cbm.values()))
        pnl = round(p.get("total_pnl", 0))
        snap = []
        snap.append(f"## 📌 CURRENT SNAPSHOT (last updated: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M IST')})")
        snap.append("")
        snap.append("### Portfolio (from logs/portfolio.json)")
        snap.append("| Bucket | Capital |")
        snap.append("|---|---|")
        for k, v in cbm.items():
            snap.append(f"| {k} | ₹{round(v):,} |")
        snap.append(f"| **TOTAL** | **₹{total:,}** |")
        snap.append("")
        snap.append(f"- Total P&L: **₹{pnl:,}** | Open: {len(p.get('open_positions',[]))} | Closed: {p.get('closed_count',0)} | Wins {p.get('total_wins',0)} / Losses {p.get('total_losses',0)}")
        snap.append("")
        txt = open(LOG, encoding="utf-8").read()
        # Replace everything between snapshot header and next "---" section
        start = txt.find("## 📌 CURRENT SNAPSHOT")
        nxt = txt.find("\n---\n", start)
        if start >= 0 and nxt > start:
            txt = txt[:start] + "\n".join(snap) + txt[nxt:]
            open(LOG, "w", encoding="utf-8").write(txt)
            print("[chat-log] snapshot refreshed")
        return True
    except Exception as e:
        print(f"[chat-log] snapshot refresh failed: {e}")
        return False

def append_entry(heading, bullets):
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M IST")
    entry = [f"### {ts} — {heading}", ""]
    for b in bullets:
        entry.append(f"- {b}")
    entry.append("")
    entry.append("---")
    entry.append("")
    txt = open(LOG, encoding="utf-8").read()
    # insert after the SESSION HISTORY header
    anchor = "## 📜 SESSION HISTORY (most recent first)"
    if anchor in txt:
        txt = txt.replace(anchor, anchor + "\n" + "\n".join(entry), 1)
    else:
        txt += "\n".join(entry)
    open(LOG, "w", encoding="utf-8").write(txt)
    print(f"[chat-log] entry appended: {heading}")

if __name__ == "__main__":
    if len(sys.argv) < 2:
        refresh_snapshot()
    else:
        heading = sys.argv[1]
        bullets = sys.argv[2:]
        refresh_snapshot()
        append_entry(heading, bullets)

"""Model-free jobs run by Hermes cron scripts (no LLM call, so they cost nothing).

    python -m perpetual_core.jobs snapshot-metrics [--backfill DAYS]
    python -m perpetual_core.jobs snapshot-viral
    python -m perpetual_core.jobs requests-pending <client>     # cron monitor script
    python -m perpetual_core.jobs radar-scan                    # early-reply radar, all clients
    python -m perpetual_core.jobs postmortem-pending <client>   # cron monitor: posts that need a verdict
    python -m perpetual_core.jobs calendar-due <client>         # reminders for approved calendar slots
    python -m perpetual_core.jobs console-replies <client>      # import the console reply agent's drafts
    python -m perpetual_core.jobs apply-changes <user_id> <summary>   # background setup + DM receipts

Cron scripts run as plain subprocesses, outside any profile's secret scope, so shared
settings are read from the file the boot reconciler writes (PERPETUAL_SHARED_ENV).
"""

import os
import sys
from pathlib import Path

SHARED_ENV = Path(os.environ.get("PERPETUAL_SHARED_ENV", "/opt/data/perpetual-shared.env"))


def _load_shared_env():
    if not SHARED_ENV.exists():
        return
    for line in SHARED_ENV.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())


def main(argv):
    _load_shared_env()
    from . import agency, ledger
    if not argv:
        print(__doc__)
        return 2
    cmd = argv[0]
    if cmd == "apply-changes":
        return apply_changes(argv[1] if len(argv) > 1 else "", argv[2] if len(argv) > 2 else "your change")
    conn = ledger.connect()
    try:
        if cmd == "snapshot-metrics":
            days = int(argv[2]) if len(argv) > 2 and argv[1] == "--backfill" else 1
            n = agency.snapshot_metrics(conn, days_back=days)
            # --no-agent cron delivers stdout verbatim; stay silent on success.
            if n == 0:
                print("⚠️ metrics snapshot stored nothing — is the scraping console reachable?")
        elif cmd == "snapshot-viral":
            agency.snapshot_viral(conn)
        elif cmd == "console-replies":
            # Silent unless there's something new (a --no-agent cron delivers stdout verbatim).
            codes = agency.import_console_replies(conn, argv[1].lower())
            if codes:
                print(f"{len(codes)} reply draft(s) from the console's reply agent: {', '.join(codes)}")
        elif cmd == "radar-scan":
            # One model-free scan for every client; each hit becomes a request for that client's agent.
            from . import radar
            radar.scan(conn)
        elif cmd == "postmortem-pending":
            # Monitor script: prints the posts that need a verdict; unchanged output = no agent run.
            from . import postmortem
            for r in postmortem.prepare(conn, argv[1].lower()):
                vs = ", ".join(f"{k} {v}x" for k, v in (r["vs_median"] or {}).items())
                print(f"post-mortem due: {r['format']} {r['url']} (code {r['code'] or '-'}) vs median: {vs or 'no baseline'}")
        elif cmd == "calendar-due":
            # --no-agent cron: stdout goes to the channel verbatim, so print only real reminders.
            from . import ledger, planner
            for r in planner.due(conn, argv[1].lower()):
                s = ledger.get(conn, argv[1].lower(), r["code"]) or {}
                first = (r["text"] or "").strip().splitlines()[0][:140] if r["text"] else ""
                link = ledger.intent_url(s["kind"], s.get("final_text") or s.get("text") or "", s.get("target_id"), s.get("target_url")) if s else None
                print(f"⏰ Time to post *{r['code']}* ({r['kind']}): {first}\n"
                      + (f"<{link}|Open in X> or " if link else "") + f"`/post {r['code']}` · `posted {r['code']}` once it's live")
        elif cmd == "requests-pending":
            # Monitor script: output only changes when there is new work, so an unchanged
            # (empty) result suppresses the agent run entirely.
            for r in agency.pending_requests(conn, argv[1].lower()):
                print(f"request {r['id']} from {r['by_name'] or 'dashboard'}: {r['request']}")
        else:
            print(f"unknown command {cmd}")
            return 2
    finally:
        conn.close()
    return 0



# ---------------------------------------------------------------- background setup with DM receipts

def _gateway_pid():
    import re
    import subprocess
    out = subprocess.run(["hermes", "cron", "status"], capture_output=True, text=True).stdout
    m = re.search(r"PID: (\d+)", out)
    return m.group(1) if m and "Gateway is running" in out else None


def apply_changes(user_id, summary):
    """Run the reconciler, DM the result, restart the gateway (new channel routes are read at start),
    then DM again once Perpetual is back. Started detached by Perpetual's tools so chat never blocks."""
    import json
    import subprocess
    import time
    from . import notify
    who = [user_id] if user_id else notify.owners()
    r = subprocess.run(["/opt/hermes/.venv/bin/python", "/opt/perpetual/reconcile.py", "--from-runtime"],
                       capture_output=True, text=True, timeout=3600)
    if r.returncode != 0:
        tail = (r.stderr or r.stdout).strip().splitlines()[-1:] or ["unknown error"]
        notify.dm(who, f"⚠️ Setting up {summary} failed: {tail[0][:300]}. Nothing else changed; ask me to try again.")
        return 1
    try:
        info = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        info = {}
    notify.dm(who, f"✅ {summary}: set up ({len(info.get('clients', []))} active clients, "
                   f"{info.get('routes', '?')} with channels). Restarting for about 20 seconds to switch it on.")
    before = _gateway_pid()
    subprocess.run(["hermes", "gateway", "restart"], capture_output=True, text=True, timeout=300)
    for _ in range(36):                       # up to 3 minutes for the new gateway to come up
        time.sleep(5)
        now = _gateway_pid()
        if now and now != before:
            time.sleep(15)                    # let Slack reconnect
            notify.dm(who, f"🟢 I'm back. {summary} is live: client agents in new channels say hello "
                           "within a few minutes, and their schedules are running.")
            return 0
    notify.dm(who, f"⚠️ {summary} is set up, but I couldn't confirm the restart. If client channels stay "
                   "quiet, ask me to restart.")
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

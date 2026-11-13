"""Model-free jobs run by Hermes cron scripts (no LLM call, so they cost nothing).

    python -m perpetual_core.jobs snapshot-metrics [--backfill DAYS]
    python -m perpetual_core.jobs snapshot-viral
    python -m perpetual_core.jobs requests-pending <client>     # cron monitor script
    python -m perpetual_core.jobs radar-scan                    # early-reply radar, all clients
    python -m perpetual_core.jobs postmortem-pending <client>   # cron monitor: posts that need a verdict
    python -m perpetual_core.jobs calendar-due <client>         # reminders for approved calendar slots
    python -m perpetual_core.jobs console-replies <client>      # import the console reply agent's drafts
    python -m perpetual_core.jobs apply-changes <user_id> <summary>   # background setup + DM receipts
    python -m perpetual_core.jobs meetings-pending              # cron monitor: finished meetings to follow up
    python -m perpetual_core.jobs tasks-remind                  # DM owners what's due today / overdue
    python -m perpetual_core.jobs standup-tick                  # standup questions + summary at the right time

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
    from . import agency, console, ledger
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
        elif cmd == "trend-scan":
            # One model-free scan of every client's niche; hits become trend requests for that agent.
            from . import trends
            trends.scan(conn)
        elif cmd == "meetings-pending":
            # Monitor script: pulls finished meetings from the agency's Read AI, prints the ones waiting
            # for follow-up. Not connected / Read AI down → print nothing (no agent run, no noise).
            from . import connections, meetings, readai
            try:
                meetings.sync(conn)
            except connections.NotConnected:
                return 0
            except readai.ReadAIError as e:
                print(f"skipped {cmd}: {e}", file=sys.stderr)
            for r in meetings.pending(conn):
                n = len(r["action_items"] or [])
                print(f"meeting {r['id']} '{r['title']}' at {r['started_at']:%Y-%m-%d %H:%M}: {n} action item(s)")
        elif cmd == "notion-sync":
            # Agency OS in Notion: human edits pulled, new/changed rows pushed. Silent unless broken.
            from . import connections, notion_os
            try:
                notion_os.sync(conn)
            except connections.NotConnected:
                return 0
            except RuntimeError as e:
                print(f"skipped {cmd}: {e}", file=sys.stderr)
        elif cmd == "scorecard-tick":
            # Team scorecard once a week, Friday >= 18:00 local (scorecard_runs guard). stdout stays empty.
            from . import scorecard
            scorecard.post(conn) if "--now" in argv else scorecard.tick(conn)
        elif cmd == "proactive-pending":
            # Monitor script: model-free triggers → pending candidates; quiet hours / none → no agent run.
            from . import proactive
            for line in proactive.pending_lines(conn):
                print(line)
        elif cmd == "semantic-index":
            # Embed new memories / conversations / document chunks (no-op until VOYAGE_API_KEY is set).
            from . import semantic
            try:
                semantic.index_pending(conn)
            except Exception as e:  # noqa: BLE001 — Voyage down: try again next run
                print(f"skipped {cmd}: {e}", file=sys.stderr)
        elif cmd == "graph-ingest":
            # Nightly: new saved facts -> the Graphiti knowledge graph (separate process; off without keys).
            from . import graph_memory
            try:
                msg = graph_memory.ingest(conn)
                if msg:
                    print(msg, file=sys.stderr)
            except graph_memory.GraphError as e:
                print(f"skipped {cmd}: {e}", file=sys.stderr)
        elif cmd in ("sleep", "memory-digest"):
            # Monitor script (nightly "sleep"): forgetting curve, then the episodes to replay, most surprising
            # first -> the agent saves lessons + facts. Nothing to replay = no output = no agent run.
            from . import brain
            for line in brain.sleep_lines(conn):
                print(line)
        elif cmd == "audience-sync":
            # Audience Seeding (GitHub): pull the newest snapshot, then the once-a-day Slack post (model-free).
            from . import audience
            try:
                print(audience.sync(conn), file=sys.stderr)
                for line in audience.post_daily(conn):
                    print(f"posted {line}", file=sys.stderr)
            except audience.AudienceError as e:
                print(f"skipped {cmd}: {e}", file=sys.stderr)
        elif cmd == "heal-chats":
            # Every 2 min: chats frozen by a restart mid-answer are un-stuck and the person is told.
            from . import heal
            for line in heal.heal():
                print(line, file=sys.stderr)
        elif cmd == "brain-bootstrap":
            # One-time "childhood": score the whole conversation history so sleep can learn from it.
            from . import brain
            print(brain.bootstrap(conn), file=sys.stderr)
        elif cmd == "team-intro":
            from . import standup
            print(f"intro sent to {standup.send_intro(conn, force='--force' in argv)} member(s)")
        elif cmd == "standup-tick":
            from . import standup
            standup.tick(conn)                         # DMs + one channel summary; stdout stays empty
        elif cmd == "tasks-remind":
            from . import tasks
            tasks.send_reminders(conn)                 # DMs only; stdout stays empty
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
    except console.ConsoleError as e:
        if not e.temporary:
            raise
        # The scraping console is briefly down: skip this round quietly (stdout would be posted to
        # Slack, and a non-zero exit posts a failure); the next scheduled run tries again.
        print(f"skipped {cmd}: {e}", file=sys.stderr)
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
    then DM again once Agency Bot is back. Started detached by Agency Bot's tools so chat never blocks."""
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

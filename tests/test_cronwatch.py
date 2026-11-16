"""Offline tests: failed scheduled jobs are retried once, then reported to owners once.

    DATABASE_URL=postgresql://... uv run --no-project --with "psycopg[binary]" tests/test_cronwatch.py
"""

import json
import os
import sqlite3
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
os.environ.update(OWNER_SLACK_IDS="UOWNER")
from perpetual_core import console, cronwatch, ledger  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def home(root, profile, jobs, runs):
    h = root / "profiles" / profile if profile else root
    (h / "cron").mkdir(parents=True, exist_ok=True)
    (h / "cron" / "jobs.json").write_text(json.dumps({"jobs": [{"id": i, "name": n} for i, n in jobs]}))
    c = sqlite3.connect(h / "cron" / "executions.db")
    c.execute("CREATE TABLE IF NOT EXISTS executions (id TEXT PRIMARY KEY, job_id TEXT, status TEXT, claimed_at TEXT, error TEXT)")
    for rid, job, status, err in runs:
        c.execute("INSERT INTO executions VALUES (?,?,?,datetime('now'),?)", (rid, job, status, err))
    c.commit()
    c.close()


def main():
    conn = ledger.connect()
    tag = uuid.uuid4().hex[:6]
    root = Path(tempfile.mkdtemp())
    j1, j2 = f"pm{tag}", f"dg{tag}"
    home(root, "kennygtm", [(j1, "px-postmortem")], [(f"r1{tag}", j1, "failed", "the AI model service returned an internal error")])
    home(root, None, [(j2, "px-digest")], [(f"r2{tag}", j2, "completed", None)])
    ran, dms = [], []

    def run(cmd, **_):
        ran.append(cmd)
        return type("R", (), {"returncode": 0, "stdout": "", "stderr": ""})()

    def dm(ids, text):
        dms.append((ids, text))
        return len(ids)

    out = cronwatch.watch(conn, root, run, dm)
    check(ran == [["hermes", "-p", "kennygtm", "cron", "run", j1]] and not dms, "first failure: retried, nobody pinged")
    check(any("internal error" in x for x in out), "reason summarised")
    cronwatch.watch(conn, root, run, dm)
    check(len(ran) == 1, "the same failed run is never retried twice")

    home(root, "kennygtm", [(j1, "px-postmortem")], [(f"r3{tag}", j1, "failed", "HTTP 530 Cloudflare Tunnel error")])
    cronwatch.watch(conn, root, run, dm)
    check(len(ran) == 1 and dms and "failed twice" in dms[0][1] and "X scraping service" in dms[0][1]
          and dms[0][0] == ["UOWNER"], "fails again after the retry: owners DMed once, in plain words")
    home(root, "kennygtm", [(j1, "px-postmortem")], [(f"r4{tag}", j1, "failed", "internal error")])
    cronwatch.watch(conn, root, run, dm)
    check(len(dms) == 1, "no repeat DM the same day")

    check(console.ConsoleError("x", 530).temporary and console.ConsoleError("x", 520).temporary
          and not console.ConsoleError("x", 404).temporary, "Cloudflare 52x counts as a temporary console outage")
    conn.execute("DELETE FROM cron_watch WHERE job_id IN (%s, %s)", (j1, j2))
    conn.commit()
    conn.close()
    print("all good")


if __name__ == "__main__":
    main()

"""Scheduled-job watchdog: transient failures fix themselves; real ones reach the owners once.

Hermes only re-runs a job that never reached the model. Anything else (an Anthropic 500, a scraping
outage, a timeout) used to post a "Cron failed" notice into the channel and wait for the next schedule.
Now failure notices are not posted to channels (jobs use --failure-deliver local) and this job
(px-watch, every 10 minutes, model-free) looks at every profile's run history:

    a job failed and wasn't retried in the last 3 hours  -> run it again now (once)
    it failed again after that retry                     -> DM the owners, at most once a day per job
"""

import glob
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from . import ledger, notify

SCHEMA = """CREATE TABLE IF NOT EXISTS cron_watch (
    run_id   TEXT PRIMARY KEY,
    profile  TEXT,
    job_id   TEXT NOT NULL,
    job_name TEXT,
    action   TEXT NOT NULL,               -- retried | reported | seen
    at       TIMESTAMPTZ NOT NULL DEFAULT now()
)"""


def _root():
    return Path(os.environ.get("PX_DATA_ROOT") or "/opt/data")


def _homes(root):
    yield None, root
    for p in sorted(glob.glob(str(root / "profiles" / "*"))):
        yield Path(p).name, Path(p)


def _job_names(home):
    try:
        data = json.loads((home / "cron" / "jobs.json").read_text(encoding="utf-8"))
        jobs = data.get("jobs", data) if isinstance(data, dict) else data
        jobs = jobs.values() if isinstance(jobs, dict) else jobs
        return {j.get("id"): j.get("name") or j.get("id") for j in jobs if isinstance(j, dict)}
    except (OSError, ValueError):
        return {}


def failures(root=None, minutes=90):
    """[{profile, run_id, job_id, job_name, error}] — failed runs that finished in the last `minutes`."""
    root = Path(root or _root())
    out = []
    for profile, home in _homes(root):
        db = home / "cron" / "executions.db"
        if not db.exists():
            continue
        names = _job_names(home)
        try:
            c = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            rows = c.execute("""SELECT id, job_id, error FROM executions WHERE status = 'failed'
                                AND claimed_at > datetime('now', ?) ORDER BY claimed_at""",
                             (f"-{int(minutes)} minutes",)).fetchall()
            c.close()
        except sqlite3.Error:
            continue
        for run_id, job_id, error in rows:
            out.append({"profile": profile, "run_id": run_id, "job_id": job_id,
                        "job_name": names.get(job_id, job_id), "error": (error or "").strip()})
    return out


def _short(error):
    e = " ".join((error or "").split())
    for key, says in (("internal error", "the AI service had an internal error"), ("overloaded", "the AI service was overloaded"),
                      ("HTTP 530", "the X scraping service was unreachable"), ("HTTP 502", "the X scraping service was down"),
                      ("rate limit", "a rate limit was hit"), ("timed out", "it timed out")):
        if key.lower() in e.lower():
            return says
    return e[-200:] or "unknown error"


def watch(conn=None, root=None, run=None, dm=None):
    own = conn is None
    conn = conn or ledger.connect()
    run = run or subprocess.run
    dm = dm or notify.dm
    done = []
    try:
        conn.execute(SCHEMA)
        conn.commit()
        for f in failures(root):
            if conn.execute("SELECT 1 FROM cron_watch WHERE run_id = %s", (f["run_id"],)).fetchone():
                continue
            retried = conn.execute("""SELECT 1 FROM cron_watch WHERE job_id = %s AND action = 'retried'
                                      AND at > now() - interval '3 hours'""", (f["job_id"],)).fetchone()
            where = f"@{f['profile']}" if f["profile"] else "HQ"
            if not retried:
                cmd = ["hermes"] + (["-p", f["profile"]] if f["profile"] else []) + ["cron", "run", f["job_id"]]
                r = run(cmd, capture_output=True, text=True, timeout=120)
                action = "retried" if r.returncode == 0 else "seen"
                done.append(f"{action} {where} {f['job_name']} ({_short(f['error'])})")
            else:
                reported = conn.execute("""SELECT 1 FROM cron_watch WHERE job_id = %s AND action = 'reported'
                                           AND at > now() - interval '24 hours'""", (f["job_id"],)).fetchone()
                action = "seen" if reported else "reported"
                if not reported:
                    dm(notify.owners(), f"⚠️ Scheduled job *{f['job_name']}* ({where}) failed twice: "
                                        f"{_short(f['error'])}. I retried it once already; it will run again at its "
                                        "next scheduled time. Nothing to do unless this keeps happening.")
                done.append(f"{action} {where} {f['job_name']}")
            conn.execute("""INSERT INTO cron_watch(run_id, profile, job_id, job_name, action) VALUES (%s,%s,%s,%s,%s)
                            ON CONFLICT (run_id) DO NOTHING""",
                         (f["run_id"], f["profile"], f["job_id"], f["job_name"], action))
            conn.commit()
        conn.execute("DELETE FROM cron_watch WHERE at < now() - interval '14 days'")
        conn.commit()
    finally:
        if own:
            conn.close()
    return done

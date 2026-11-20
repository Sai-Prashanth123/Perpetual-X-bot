"""Offline tests: frequent jobs only wake the (metered) database when they have something to do.

    uv run --no-project tests/test_db_wakeups.py
"""

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from perpetual_core import agency, cronwatch, standup  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def main():
    tmp = Path(tempfile.mkdtemp())
    gate = tmp / "gate.json"
    t = datetime(2026, 11, 20, 5, 7, tzinfo=timezone.utc)          # 05:07 UTC, off the grid
    check(standup.needs_db(t, gate), "no gate yet: open the database once")

    ask = t + timedelta(minutes=50)
    gate.write_text(json.dumps({"at": t.isoformat(), "moments": [ask.isoformat()]}))
    check(not standup.needs_db(t + timedelta(minutes=5), gate), "between moments: database left asleep")
    check(standup.needs_db(ask, gate) and standup.needs_db(ask + timedelta(minutes=10), gate),
          "at a moment (and for 15 minutes after): database opened")
    check(not standup.needs_db(ask + timedelta(minutes=16), gate), "after the window: asleep again")
    hour = t.replace(hour=6, minute=20)
    check(standup.needs_db(hour, gate) and not standup.needs_db(hour.replace(minute=25), gate),
          "hourly refresh only on the shared :00/:20/:40 grid")

    class Conn:                                                      # save_gate reads only the config
        def execute(self, *_a, **_k):
            return self

        def fetchone(self):
            return {"id": 1, "time": "11:30", "timezone": "Asia/Kolkata", "days": "1,2,3,4,5", "lead_minutes": 45,
                    "summary_minutes": 5, "enabled": True, "checkin_time": "15:00", "eod_time": "18:30",
                    "channel": None, "members": None, "excluded": None}

        def commit(self):
            pass
    standup.save_gate(Conn(), t, gate)
    g = json.loads(gate.read_text())
    first = datetime.fromisoformat(g["moments"][0])
    check(len(g["moments"]) == 4 and first.astimezone(timezone.utc).strftime("%H:%M") == "05:15",
          "gate holds Friday's moments (questions 10:45 IST = 05:15 UTC); Saturday is not a standup day")

    root = tmp / "data"
    root.mkdir()
    check(cronwatch.watch(conn=None, root=root) == [], "watchdog with no failures never connects")
    check(agency.kick_requests("nobody") is False, "kick without a profile is a quiet no-op")
    print("all good")


if __name__ == "__main__":
    main()

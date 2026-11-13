"""Offline tests: chats frozen by a restart are found, un-stuck and the person is told.

    uv run --no-project tests/test_heal.py
"""

import json
import os
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "lib"))
from perpetual_core import heal, notify  # noqa: E402


def check(cond, msg):
    if not cond:
        raise SystemExit(f"FAIL: {msg}")
    print(f"ok   {msg}")


def db(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(path)
    c.execute("""CREATE TABLE session_admissions (seq INTEGER PRIMARY KEY, admission_id TEXT, principal_id TEXT,
                 target_session_id TEXT, payload_json TEXT, status TEXT)""")
    for r in rows:
        c.execute("INSERT INTO session_admissions(admission_id, principal_id, target_session_id, payload_json, status) "
                  "VALUES (?,?,?,?,?)", r)
    c.commit()
    c.close()


def main():
    root = Path(tempfile.mkdtemp())
    db(root / "state.db", [
        ("a1", 'messaging:[null,"slack","D0DM1",null,"U0SAI"]', "s-dm", json.dumps({"text": "what is going on with alex"}), "unknown"),
        ("a2", 'messaging:[null,"slack","D0DM1",null,"U0SAI"]', "s-dm", json.dumps({"text": "fine"}), "terminal"),
    ])
    db(root / "profiles" / "michlieben" / "state.db", [
        ("b1", 'messaging:[null,"slack","C0MICH","1731.5","U0NAGA"]', "s-ch", json.dumps({"text": "draft 3 hooks"}), "unknown"),
    ])
    turns = heal.stuck_turns(root)
    check(len(turns) == 2 and {t["profile"] for t in turns} == {None, "michlieben"},
          "finds lost turns in HQ and in client profiles (not finished ones)")

    ran, posts = [], []

    def run(cmd, **_):
        ran.append(cmd)
        return type("R", (), {"returncode": 0, "stderr": "", "stdout": "discarded"})()

    def slack(method, **kw):
        posts.append(kw)
        return {"ok": True}

    out = heal.heal(run=run, slack=slack, root=root)
    check(ran[0][:5] == ["hermes", "sessions", "discard", "s-dm", "--admission"] and "--yes" in ran[0],
          "HQ chat discarded non-interactively")
    check(ran[1][:3] == ["hermes", "-p", "michlieben"], "client profile chat discarded in its own profile")
    dm = next(p for p in posts if p["channel"] == "D0DM1")
    check("what is going on with alex" in dm["text"] and "send it once more" in dm["text"], "person told what was lost")
    ch = next(p for p in posts if p["channel"] == "C0MICH")
    check(ch["thread_ts"] == "1731.5" and "<@U0NAGA>" in ch["text"], "in a channel: same thread, mentions the person")
    check(all(x.startswith("healed") for x in out), "results logged")

    def failing(cmd, **_):
        return type("R", (), {"returncode": 1, "stderr": "locked", "stdout": ""})()
    posts.clear()
    out = heal.heal(run=failing, slack=slack, root=root)
    check(all(x.startswith("FAILED") for x in out) and not posts, "a failed discard isn't announced")
    notify.dm = lambda ids, text: len(ids)
    check(heal.stuck_turns(Path(tempfile.mkdtemp())) == [], "nothing stuck → nothing to do")
    print("all good")


if __name__ == "__main__":
    os.environ.pop("PX_DATA_ROOT", None)
    main()

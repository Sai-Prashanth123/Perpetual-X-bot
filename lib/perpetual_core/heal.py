"""Self-healing chats: un-stick conversations that a restart froze.

When the bot restarts (a deploy, a crash, low memory) while it's answering someone, Hermes fences that
turn as 'unknown' and pauses the WHOLE chat until someone acknowledges it: new messages get a ✅ but no
reply. This job (px-heal, every 2 minutes, model-free) finds every chat in every profile with such a
lost turn, acknowledges it (`hermes sessions discard`), and tells the person in that same chat what
was lost so they can send it again. Nobody stays silently stuck.
"""

import glob
import json
import os
import sqlite3
import subprocess
from pathlib import Path

from . import notify


def _data_root():
    return Path(os.environ.get("PX_DATA_ROOT") or "/opt/data")


def stuck_turns(root=None):
    """[{profile, session, admission, chat, thread, user, text}] — every lost turn, read-only."""
    root = Path(root or _data_root())
    dbs = [(None, root / "state.db")] + [(Path(p).parent.name, Path(p)) for p in
                                         sorted(glob.glob(str(root / "profiles" / "*" / "state.db")))]
    out = []
    for profile, db in dbs:
        if not db.exists():
            continue
        try:
            conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
            rows = conn.execute("""SELECT target_session_id, admission_id, principal_id, payload_json
                                   FROM session_admissions WHERE status = 'unknown' ORDER BY seq""").fetchall()
            conn.close()
        except sqlite3.Error:
            continue
        for session, admission, principal, payload in rows:
            chat = thread = user = None
            if (principal or "").startswith("messaging:"):
                try:
                    parts = json.loads(principal.split(":", 1)[1])     # [?, platform, chat, thread, user]
                    if len(parts) >= 5 and parts[1] == "slack":
                        chat, thread, user = parts[2], parts[3], parts[4]
                except (ValueError, IndexError):
                    pass
            try:
                text = (json.loads(payload or "{}").get("text") or "").strip()
            except ValueError:
                text = ""
            out.append({"profile": profile, "session": session, "admission": admission, "chat": chat,
                        "thread": thread, "user": user, "text": text})
    return out


def _discard(turn, run=None):
    run = run or subprocess.run
    cmd = ["hermes"] + (["-p", turn["profile"]] if turn["profile"] else []) + \
        ["sessions", "discard", turn["session"], "--admission", turn["admission"], "--yes"]
    r = run(cmd, capture_output=True, text=True, timeout=120)
    return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-300:]


def _tell(turn, slack=None):
    """Say it in the same chat (thread if there was one): the chat stays readable for everyone in it."""
    from . import slackui
    slack = slack or slackui.slack
    lost = " ".join(turn["text"].split())
    quote = f": “{lost[:280]}{'…' if len(lost) > 280 else ''}”" if lost else ""
    who = f"<@{turn['user']}> " if turn["user"] and (turn["chat"] or "").startswith(("C", "G")) else ""
    msg = (f"{who}Sorry, I restarted while I was answering your message{quote}. "
           "I've cleared it so I can reply again: please send it once more.")
    if turn["chat"]:
        try:
            slack("chat.postMessage", channel=turn["chat"], thread_ts=turn["thread"] or None, text=msg,
                  unfurl_links=False)
            return True
        except Exception:  # noqa: BLE001 — fall back to a DM
            pass
    return bool(turn["user"] and notify.dm([turn["user"]], msg))


def heal(run=None, slack=None, root=None):
    """Un-stick every frozen chat. Returns a list of short results (for the job log)."""
    done = []
    for turn in stuck_turns(root):
        ok, detail = _discard(turn, run)
        if not ok:
            done.append(f"FAILED {turn['profile'] or 'hq'}/{turn['session']}: {detail}")
            continue
        told = _tell(turn, slack)
        done.append(f"healed {turn['profile'] or 'hq'}/{turn['session']} ({turn['user']}){'' if told else ' — could not notify'}")
    return done

"""Content calendar: a planned week of posts per client, with a reminder at each slot.

A slot is a saved suggestion (its code) plus a time. It counts as approved when the slot was
approved ("approve the plan") or the suggestion itself was approved or edited ("ok T4"). Only
approved slots get a reminder, once, and never for drafts that were rejected or already posted.
"""

from datetime import datetime, timedelta, timezone

LATE_AFTER = timedelta(hours=3)        # don't nag about a slot that passed hours ago


def save(conn, client, slots):
    """slots: [{code, slot_at (ISO time with zone)}]. Re-planning a code moves its slot."""
    out = []
    for s in slots:
        at = datetime.fromisoformat(str(s["slot_at"]).replace("Z", "+00:00"))
        if at.tzinfo is None:
            raise ValueError(f"slot_at needs a timezone: {s['slot_at']}")
        code = s["code"].upper()
        if not conn.execute("SELECT 1 FROM suggestions WHERE client = %s AND code = %s", (client, code)).fetchone():
            raise ValueError(f"{code} isn't a saved draft for @{client}; suggest_save it first")
        conn.execute(
            """INSERT INTO calendar_slots(client, slot_at, code, note) VALUES (%s,%s,%s,%s)
               ON CONFLICT (client, code) DO UPDATE SET slot_at = EXCLUDED.slot_at, note = EXCLUDED.note,
                   status = CASE WHEN calendar_slots.status IN ('posted', 'skipped') THEN calendar_slots.status
                                 ELSE 'planned' END""",
            (client, at, code, s.get("note")))
        out.append({"code": code, "slot_at": at.isoformat()})
    conn.commit()
    return out


def approve(conn, client, codes=None):
    """Approve the listed slots, or every planned slot from now on."""
    if codes:
        rows = conn.execute(
            """UPDATE calendar_slots SET status = 'approved' WHERE client = %s AND code = ANY(%s)
               AND status = 'planned' RETURNING code""", (client, [c.upper() for c in codes])).fetchall()
    else:
        rows = conn.execute(
            """UPDATE calendar_slots SET status = 'approved' WHERE client = %s AND status = 'planned'
               AND slot_at > now() RETURNING code""", (client,)).fetchall()
    conn.commit()
    return [r["code"] for r in rows]


def due(conn, client, now=None):
    """Approved slots whose time has come: mark them reminded and return them (once each)."""
    now = now or datetime.now(timezone.utc)
    rows = conn.execute(
        """SELECT c.id, c.code, c.slot_at, c.note, s.kind, COALESCE(s.final_text, s.text) AS text, s.status AS s_status
           FROM calendar_slots c JOIN suggestions s ON s.client = c.client AND s.code = c.code
           WHERE c.client = %s AND c.slot_at <= %s AND c.slot_at > %s
             AND (c.status = 'approved' OR (c.status = 'planned' AND s.status IN ('approved', 'edited')))
           ORDER BY c.slot_at""", (client, now, now - LATE_AFTER)).fetchall()
    out = []
    for r in rows:
        if r["s_status"] in ("rejected", "posted", "expired"):
            conn.execute("UPDATE calendar_slots SET status = %s WHERE id = %s",
                         ("posted" if r["s_status"] == "posted" else "skipped", r["id"]))
            continue
        conn.execute("UPDATE calendar_slots SET status = 'reminded' WHERE id = %s", (r["id"],))
        out.append(dict(r))
    conn.commit()
    return out


def week(conn, client, days=8):
    return [dict(r) for r in conn.execute(
        """SELECT c.code, c.slot_at, c.status, s.kind, s.status AS draft_status,
                  left(COALESCE(s.final_text, s.text), 140) AS preview
           FROM calendar_slots c JOIN suggestions s ON s.client = c.client AND s.code = c.code
           WHERE c.client = %s AND c.slot_at > now() - interval '1 day'
             AND c.slot_at < now() + make_interval(days => %s)
           ORDER BY c.slot_at""", (client, days))]

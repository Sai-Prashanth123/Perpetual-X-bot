"""Lead finder storage: people who engaged with a client's posts and look like buyers.

The agent finds and scores them; this module keeps them deduplicated per client with a code
(L1, L2…) so the team can say "L3 contacted" or "L5 not a fit". Nobody is ever contacted
automatically; the suggested opener is for a person to send.
"""

STATUSES = ("new", "contacted", "replied", "won", "not_a_fit")


def _next_code(conn, client):
    row = conn.execute(
        "SELECT COALESCE(MAX(substring(code from 2)::int), 0) + 1 AS n FROM leads WHERE client = %s", (client,)).fetchone()
    return f"L{row['n']}"


def save(conn, client, items):
    """Add new leads (skipping anyone already on this client's list). Returns [{code, handle}]."""
    saved = []
    for it in items:
        handle = (it.get("handle") or "").lstrip("@").lower()
        if not handle or handle == client:
            continue
        if conn.execute("SELECT 1 FROM leads WHERE client = %s AND handle = %s", (client, handle)).fetchone():
            continue
        code = _next_code(conn, client)
        conn.execute(
            """INSERT INTO leads(client, code, handle, name, bio, followers, role_company, said, said_url,
                                 fit_score, why, opener)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)""",
            (client, code, handle, it.get("name"), it.get("bio"), it.get("followers"), it.get("role_company"),
             it.get("said"), it.get("said_url"), it.get("fit_score"), it.get("why"), it.get("opener")))
        saved.append({"code": code, "handle": handle})
    conn.commit()
    return saved


def known(conn, client, handles):
    hs = [h.lstrip("@").lower() for h in handles if h]
    return [r["handle"] for r in conn.execute(
        "SELECT handle FROM leads WHERE client = %s AND handle = ANY(%s)", (client, hs))]


def update(conn, client, code, status, note=None):
    if status not in STATUSES:
        raise ValueError(f"status must be one of {', '.join(STATUSES)}")
    row = conn.execute(
        """UPDATE leads SET status = %s, note = COALESCE(%s, note), updated_at = now()
           WHERE client = %s AND code = %s RETURNING code, handle, status""",
        (status, note, client, code.upper())).fetchone()
    conn.commit()
    if not row:
        raise ValueError(f"no lead {code} for @{client}")
    return dict(row)


def listing(conn, client=None, status=None, days=60):
    return [dict(r) for r in conn.execute(
        """SELECT client, code, handle, name, role_company, followers, fit_score, status, why, said_url, found_at
           FROM leads WHERE found_at > now() - make_interval(days => %s)
             AND (%s::text IS NULL OR client = %s) AND (%s::text IS NULL OR status = %s)
           ORDER BY found_at DESC LIMIT 100""", (days, client, client, status, status))]

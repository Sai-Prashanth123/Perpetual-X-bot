---
name: agency-digest
description: "Daily agency digest for #hq: leaderboard movers, clients needing attention, what's viral in each niche, and employee follow-through."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, agency, reporting]
---

# Agency digest (daily)

1. `agency_overview` days 1 and days 7.
2. `viral_feed` days 1, limit 30.
3. Write ≤ 20 lines:
   - **Headline** — one sentence on the agency's day.
   - **Leaderboard (7d)** — compact table: client, followers Δ, views, views Δ%, engagement rate,
     suggestions used/posted. Sorted by views.
   - **Needs attention** — every flag from the overview, each with the one action that fixes it.
   - **Viral today** — the 5 biggest tweets across niches: author, gist, likes, link, and which
     client should jump on it (then `agency_request` that client's bot with "draft replies and a
     quote for <url>" — at most 3 requests).
   - **Team** — per employee: actions, posted, median minutes to act. Call out great work too.

---
name: performance-report
description: "Weekly what's-working report for the client's X account: results, winners, losers, patterns, best times, suggestion hit-rate, and next week's focus."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, reporting]
    related_skills: [playbook]
---

# Performance report

## Data
1. `outcomes_sync` days 7.
2. `x_client_analytics` kind summary days 7, and days 28 for the baseline.
3. `x_client_analytics` kind rhythm days 28; kind followers days 28.
4. `stats_query` days 7 and days 28.
5. `x_trends` for what's working in the niche overall.

## Report (scannable, ≤ 25 lines)
1. **Headline** — one sentence: better or worse than the 4-week baseline, and why.
2. **Numbers** — posts, likes, views, bookmarks, followers: this week vs the 4-week weekly average.
3. **What worked** — top 3 posts and the reason each worked (hook, format, topic, timing).
4. **What didn't** — bottom 2–3 and the likely reason.
5. **Suggestions** — how many made, approved, edited, rejected, posted; how the posted ones did
   against the client's own average. Top rejection reasons.
6. **Timing** — best hours and days from rhythm.
7. **Niche** — what's breaking out that they aren't doing yet.
8. **Next week** — 3 concrete focuses.

Never invent numbers. If data is missing, say what's missing.

## Deliver
Also read `research_memory` and save this week's key finding to it (kind performance). Publish the
report with `publish_document` (kind `report`, weekly-report template from `doc-templates`); the
summary is the headline plus the 3 focuses. Then reply `[SILENT]`.

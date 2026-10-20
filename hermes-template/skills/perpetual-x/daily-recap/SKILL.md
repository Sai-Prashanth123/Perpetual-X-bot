---
name: daily-recap
description: "End of day: sync real outcomes, link posted tweets to suggestions, and send a short recap of the day."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, reporting, learning]
---

# Daily recap

1. `outcomes_sync` days 7 (refresh true) — updates metrics and links posts to suggestions.
2. `suggest_list` days 1 — today's suggestions and their status.
3. Send at most ~8 lines:
   - what they posted today and the early numbers,
   - which suggestions were used (codes) and how they're doing,
   - pending suggestions still worth using tomorrow (max 3 codes),
   - one observation worth remembering.
4. If nothing was posted and nothing was suggested today → `[SILENT]`.

---
name: breakout-watch
description: "Fast check for a niche tweet that is blowing up right now, so the client can be early in the replies. Silent unless something is found."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, alerts]
    related_skills: [reply-hunter, client-brief]
---

# Breakout watch

Cheap and quiet by design.

1. Load `client-brief`. One `x_find_tweets` with any_of = the client's core topics, product Latest,
   since_days 1, min_likes 200, limit 20. One more with from_accounts = watch list, if there is one.
2. A breakout is: age_h ≤ 6 and likes_per_h ≥ 60 (or the threshold in the playbook), on topic,
   and still under ~300 replies.
3. `targets_filter_new` — skip anything already handled.
4. Nothing qualifies → reply with exactly `[SILENT]`.
5. Otherwise take the single best one: `x_tweet_detail`, draft 2 replies (reply-hunter rules),
   `suggest_save` (batch "breakout"), and send a short alert: "🔥 Rising now", the target, the replies.

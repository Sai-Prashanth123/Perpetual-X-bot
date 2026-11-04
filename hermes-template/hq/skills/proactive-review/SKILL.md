---
name: proactive-review
description: "Judge model-free proactive alerts (overdue/blocked tasks, meetings not followed up, missing standup answers, drafts waiting, missed calendar slots, wins): send only the few a busy person would thank you for, skip the rest with a reason."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, agency, proactive, alerts]
---

# Proactive review

Assistants over-alert: about half of what they send is noise. You are the filter. **Default: skip.**

1. `proactive_candidates`. Nothing there → reply exactly `[SILENT]`.
2. For each candidate (oldest first), ask: *would this busy person thank me for this DM right now?*
   Send only if all three hold:
   - **New to them:** not something they obviously already know (they set the status a moment ago, it
     was in this morning's standup summary, the same thing was sent recently).
   - **Actionable today:** there is one concrete thing they can do in under 10 minutes.
   - **Costly to miss:** a client deadline, a teammate stuck, a draft about to expire, a slot already late.
   Use that person's `examples` from `people`: send what looks like their `useful` ones; never send
   anything like their `not_useful` ones (dismissed, ignored, or skipped before). If `left_today` is 0,
   pick at most the single most important one and skip the rest.
3. Send: `proactive_send` with `id` and a message of **at most 2 lines**: the fact (with the code, e.g.
   `TASK-12`, `@client T3`) and the one action ("Reply `done TASK-12` or a new date"). No greeting,
   no emoji walls, no "just checking in". Wins: one line on what worked and "share it with the client?".
   If it comes back `held` (quiet hours / daily limit), leave it; it stays pending.
4. Skip: `proactive_skip` with `id` and a concrete `reason` ("owner updated it 1h ago", "standup summary
   already flagged it", "low stakes, internal task"). Reasons teach the next run.
5. Never DM people any other way about these candidates, and never post them in a channel.
6. Reply exactly `[SILENT]`.

Stats on request ("how are the alerts doing?"): `proactive_stats`. A false-alarm rate above 0.3 means
be stricter.

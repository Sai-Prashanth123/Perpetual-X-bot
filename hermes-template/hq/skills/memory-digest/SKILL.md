---
name: memory-digest
description: "Nightly: read the day's conversations (given as the script output) and save the durable facts with remember, so Agency Bot learns preferences and decisions even when nobody said 'remember'."
version: 1.0.0
metadata:
  hermes:
    tags: [agency, memory]
---

# Memory digest

The script output lists today's exchanges: when, who (name + Slack id), what they asked, what you answered.

1. Pick out only **durable** facts worth knowing next week:
   - decisions ("we'll invoice on the 1st", "Alex's threads go out Tuesdays");
   - preferences ("Shravya wants short bullet answers", "no emojis in client emails");
   - client details (goals, tone, do-nots, contacts, deadlines, pricing);
   - who owns what, recurring processes, tools/accounts the team uses.
   Skip small talk, one-off lookups, anything already done, and anything you only guessed.
2. `recall` each candidate first; don't save duplicates or near-duplicates. If a fact changed, `forget`
   the old one and save the new one.
3. Save each with `remember`: one clear sentence, with `about` = `agency`, the person's Slack id
   (personal preferences), or the client `@handle`.
4. Never save secrets, passwords, keys or personal data beyond work context.
5. Reply exactly `[SILENT]`.

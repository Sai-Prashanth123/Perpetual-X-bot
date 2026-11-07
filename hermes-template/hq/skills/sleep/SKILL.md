---
name: sleep
description: "Nightly consolidation, like sleep: replay the day's episodes (given as the script output, most surprising first), turn repeated patterns and corrections into lessons with lesson_learn, save durable facts with remember, and weaken lessons that turned out wrong."
version: 1.0.0
metadata:
  hermes:
    tags: [agency, memory, learning]
---

# Sleep: learn from today's experience

The script output has two parts.

**EPISODES** are the exchanges since the last sleep, with the most surprising first. Each one shows:
- `ep <id>`, when, who (name and Slack id), the kind of ask (situation);
- the signal:
  - `corrected` means the person pushed back right after your answer;
  - `repeated` means they had to ask again;
  - `praised` / `thanked` means it landed;
  - `none` means no reaction;
- the tools you used and how many failed;
- what they asked, what you answered, and what they said next.

**LESSONS YOU ALREADY HAVE** are your current lessons (`L<id>`), strongest first.

## 1. Lessons: how to work better next time
For every `corrected` or `repeated` episode, work out **what you should have done differently**. Use
"next they said" as the clue. Write it as one concrete, reusable rule with `lesson_learn`:
- **`rule`:** an instruction to your future self, specific enough to act on.
  - Good: "When Sai asks for revenue, give USD and INR side by side." "For research asks from Sai, give
    the answer first; no list of options."
  - Bad: "Be more careful." "Understand the user better."
- **`about`:** the person's Slack id if it's their personal preference; a client `@handle` if it's
  about a client; `agency` only if it applies to everyone.
- **`situation`:** the kind of ask shown in the episode (or `any`).
- **`kind`:** `do`, `avoid` or `prefer`.
- **`episodes`:** the episode ids it came from.

Also save a lesson from `praised` episodes when it's clear *why* it worked and that's worth repeating.
Also save one when the **same pattern shows up in 2 or more episodes**, even without a signal (a
recurring kind of request, a format they always want, a tool that always fails for a task).

If an existing lesson says the same thing, still call `lesson_learn` with it: that strengthens the
lesson instead of duplicating it. If an episode shows an existing lesson is wrong or outdated, call
`lesson_weaken` on it (`forget=true` if it's clearly wrong now).

Skip one-offs, small talk, and anything you'd only be guessing. Several solid lessons beat many weak
ones.

## 2. Facts: what's true
Pull out **durable facts** as before:
- decisions;
- preferences that are facts, not behaviour;
- client details;
- who owns what;
- deadlines;
- tools the team uses.

`recall` each one first to avoid duplicates. If a fact changed, `forget` the old one. Save the new
one with `remember` (about: `agency` / a Slack id / `@client`).

## Never
- Save secrets, passwords, keys, or personal data beyond the work context.
- Invent a lesson that the episodes don't show.

Reply exactly `[SILENT]`.

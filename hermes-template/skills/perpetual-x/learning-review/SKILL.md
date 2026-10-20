---
name: learning-review
description: "Weekly self-improvement: turn feedback and real outcomes into updated playbook and client-voice skills, with evidence, and log what changed."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, learning]
    related_skills: [playbook, client-voice]
---

# Learning review

This is how the bot gets better. Be rigorous: small samples are hypotheses, not rules.

## Gather
1. `outcomes_sync` days 7.
2. `stats_query` days 28 (and days 7 for what changed this week).
3. `x_client_content` days 28 — their real posts, including ones that didn't come from suggestions.
4. `suggest_list` status edited and status rejected, days 14. Read the client's edits closely:
   every edit is a direct lesson about their voice.
5. Read the current `playbook` and `client-voice` skills and the last `learning_log` entry.

## Decide
- Which hooks, formats and topics get approved and perform? Which get rejected, and why?
- What do the client's edits consistently change (length, words, tone, structure)?
- Which of their own posts beat their average, and what do those posts share?
- Which playbook rules held up, which failed, which hypotheses can now be confirmed?

## Update (use skill_manage to rewrite the skills)
- `playbook`: keep the frontmatter and header; rewrite the body as **Do more**, **Do less**,
  **Avoid**, **Timing**, **Reply targets**, **Experiments for next week** — every rule with its
  evidence and sample size. Bump `version`.
- `client-voice`: fold in the edit lessons — add phrases they use, remove ones they strip out,
  update length and structure guidance, refresh the 5 real example posts. Bump `version`.
- Don't delete a hard-won rule because of one bad week; downgrade it to a hypothesis instead.

## Log and tell
`learning_log` with a one-line summary and the list of changes. Then post a short changelog,
"This week I learned: …", max 6 bullets, each with its evidence.

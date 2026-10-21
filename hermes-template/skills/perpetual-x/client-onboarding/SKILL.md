---
name: client-onboarding
description: "First run for a new client: study their profile and history, write client-voice v1 and seed the playbook, then introduce the bot."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, setup]
    related_skills: [client-voice, playbook, client-brief]
---

# Client onboarding

1. Load `client-brief`. `x_user_info` on the client.
2. `x_client_content` days 90, refresh true, limit 100; then again with tab replies.
   If the console has little data yet (new to the roster), also run `x_user_tweets` since_days 90,
   limit 100, include_replies true.
3. `x_client_analytics` summary days 90 and rhythm days 90 (may be thin for a new client).
4. `x_trends`; `x_user_tweets` for up to 3 watch-list accounts, limit 15.

## Write client-voice v1 (skill_manage)
Keep the frontmatter but set `version: 1.0.0` (this marks onboarding as done — while it stays
0.0.0, onboarding re-runs on every restart); replace the body with:
- **Who they are on X** (2 lines), **audience**, **topics they own**.
- **Tone**, **sentence style**, **typical length** (one-liner vs long-form split, as %),
  **structure habits** (lists, line breaks, arrows), **openers they use**, **how they end posts**.
- **Vocabulary**: words and phrases they use; words they never use.
- **Replies**: how they reply (length, tone), from the replies tab.
- **Never**: hashtags, emojis, hype — whatever they don't do.
- **5 real example posts** (their best, verbatim) and **3 real replies**.

## Seed the playbook (skill_manage)
From their history: top posts and what they share, formats by average likes, best hours, topics
that win — each with evidence and n. Mark thin evidence `(hypothesis)`. Version 1.0.0.

## Introduce yourself
`learning_log` "onboarding complete", then `notify_team` with one line ("Onboarded: voice learned from N posts, intro posted in the channel"). Then send a short intro: what you learned about their voice
(3 bullets); what you'll send and when (hunts every 2h during the day, breakout alerts, ideas at
8:00, engage-back at 13:00, recap at 21:00, report on Monday, learning review on Sunday); and how
to give feedback (`ok T1`, `edit R2: …`, `no Q3 reason`).

---
name: tweet-ideas
description: "Suggest original tweets (and one thread) for the client, grounded in today's trends, their winners and the playbook. Used by the daily ideas job and on demand."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, suggestions]
    related_skills: [client-voice, playbook, client-brief]
---

# Tweet ideas

## Gather (keep tool calls lean)
1. Load `client-brief`, `client-voice`, `playbook`.
2. `x_trends` — what is breaking out in the niche right now, plus client-specific recommendations.
3. `x_client_content` days=14 — what they've said recently (never repeat it) and which posts won.
4. `x_find_tweets` for 1–2 niche topics from the brief, min_likes 100, since_days 2 — fresh angles.
5. Optional: `x_user_tweets` on one watch-list account if it has posted a winner.

## Write
Default batch: **5 tweets + 1 thread outline** (on demand: what was asked, default 3).

- Each idea must be something only this client could credibly say — anchored in their work,
  opinions and experience as shown in their posts. Generic advice anyone could post is a fail.
- Mix hooks and formats per the playbook. Favour proven winners ~70%, test something new ~30%
  and label the test in the rationale.
- Tie at least 2 ideas to a live trend or conversation, and say which one.
- Respect length and style from `client-voice`. No hashtags or emojis unless the voice uses them.
- Use `[placeholder]` for any specific number or fact you don't actually have.
- Thread: write it in full — hook tweet, then 4–8 tweets — separated by a line containing only `---`
  (that is how `/post` splits it into a real thread).

## Save and show
`suggest_save` with kind tweet/thread, hook, format, topic, rationale, batch "tweet-ideas".
Show each with its code. End with one line on the best time to post today, if the playbook or
`x_client_analytics kind=rhythm` supports one.

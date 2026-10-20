---
name: reply-hunter
description: "Find rising tweets in the client's niche worth replying to and draft replies in their voice. Used by the hunt job and when someone pastes a tweet URL."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, suggestions, replies]
    related_skills: [client-voice, playbook, client-brief, quote-hunter]
---

# Reply hunter

## Find targets
1. Load `client-brief` (topics, watch accounts, accounts to avoid), `client-voice`, `playbook`.
2. `x_find_tweets` per 2–3 niche topics: product Top, since_days 1, min_likes 30–100 (raise it for
   broad topics). Also `x_find_tweets` with from_accounts = watch list, since_days 1.
3. Pool the results and drop: the client's own tweets, anything older than ~24h, anything with
   more than ~1000 replies (a reply there is invisible), rage-bait, politics, tragedies, and
   authors on the avoid list.
4. `targets_filter_new` on the remaining ids.
5. Rank by likes_per_h and fit with the client's expertise. Keep the top **3** for replies
   (quote-hunter picks its own from the same pool).

## Draft
For each target, call `x_tweet_detail` to see the thread and the top replies — don't repeat what
has already been said. Then write **2–3 reply options**, each a different move:
- add a specific insight or number from the client's experience,
- a sharp disagreement or nuance,
- a short question that pulls the author into conversation.

Replies are short (usually under 200 characters), no links, no self-promotion, no "Great post!".
They must make sense to someone reading only the original tweet.

## Save and show
`suggest_save` kind reply, with target_id, target_url, target_text, target_by, hook, topic,
batch "reply-hunter". Show the target (author, gist, likes, link), then the coded options, each with
its Open in X link (replies to other people's tweets can't go through the API, so the link is how they post).

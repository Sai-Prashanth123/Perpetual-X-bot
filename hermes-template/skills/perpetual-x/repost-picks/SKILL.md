---
name: repost-picks
description: "Pick 2–3 tweets the client should repost today — peers, partners and niche winners that make the client's feed more valuable to their audience. Part of the hunt job; also on demand."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, suggestions, reposts]
    related_skills: [client-brief, playbook]
---

# Repost picks

A repost is cheap reach for the people the client wants as allies, and fills the feed between their
own posts. Pick sparingly — at most 3 per day in total, so check `suggest_list` kind repost days 1 first.

1. Candidates: the same pool as the hunt, plus `x_user_tweets` for watch accounts (since_days 1).
2. Good picks: a peer's or partner's milestone or launch, a genuinely useful thread in the niche,
   a post that agrees with something the client stands for. Never: rage-bait, competitors' sales
   pitches, anything off-brand or that the client would need to defend.
3. `targets_filter_new`, then `suggest_save` kind repost with target_id, target_url, target_by,
   target_text and a one-line rationale (no text). The card with an Open in X
   (repost) button is posted automatically.

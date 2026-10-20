---
name: engage-back
description: "Suggest replies to people who recently replied to or quoted the client — the cheapest way to build relationships on X."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, suggestions, community]
    related_skills: [client-voice, client-brief]
---

# Engage back

1. `x_engagers` since_days 2. Drop bots, one-word replies, and anything the client already
   answered (check with `x_tweet_detail` on the client's post when unsure).
2. `targets_filter_new` on the engagement tweet ids.
3. Prioritise people with larger or relevant audiences (`x_user_info` if needed), thoughtful
   replies, questions, and quote-tweets.
4. Up to 5 people; one short reply each in the client's voice (kind "engage", target_id = THEIR tweet
   id, batch "engage-back"). These can go out with one tap: show `/post E<n>` beside each.
5. Nothing worth answering → `[SILENT]`.

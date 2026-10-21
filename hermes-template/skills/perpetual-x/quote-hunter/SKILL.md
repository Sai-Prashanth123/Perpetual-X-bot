---
name: quote-hunter
description: "Pick trending tweets where a quote-tweet lets the client add a real take, and draft the quote copy. Used by the hunt job and on demand."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, suggestions, quotes]
    related_skills: [client-voice, playbook, client-brief, reply-hunter]
---

# Quote hunter

A quote-tweet puts the client's take in front of *their* followers, so the bar is higher than for a
reply: the original must matter to their audience, and the take must stand on its own.

## Pick
From the same candidate pool as reply-hunter (or a fresh `x_find_tweets`, product Top, since_days 2,
min_likes 150), after `targets_filter_new`, choose up to **2** tweets that:
- are clearly relevant to the client's audience,
- the client has real authority or a contrarian angle on,
- were not already used for a reply suggestion in this run.

## Draft
**2 quote options** per pick:
- a "here's what this misses / here's how we actually do it" take with something concrete,
- a compact framework, list or one-liner reaction.
The copy must read well even if someone never opens the quoted tweet.

## Save and show
`suggest_save` kind quote, with the target fields, hook, format, topic, batch "quote-hunter". The cards with
buttons are posted automatically.

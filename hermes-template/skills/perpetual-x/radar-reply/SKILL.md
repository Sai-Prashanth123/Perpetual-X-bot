---
name: radar-reply
description: "Handle an early-reply radar hit: a big account in the client's niche posted minutes ago. Draft 2 replies fast, in voice, and send one compact alert."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, radar, replies]
    related_skills: [client-voice, reply-hunter]
---

# Radar reply (speed matters: finish in a few minutes)

The request gives you the tweet URL, id, text and age. Replies in the first hour land near the
top, where the client is seen by the author's audience.

1. **Check it's worth it.** It must be on-topic for the client and the client must have something
   real to add. If it's off-topic or a bad look (politics, drama), reply `[SILENT]`.
2. Read the top replies with `x_tweet_detail` (limit 20), so you don't repeat what's said.
3. **Draft 2 different replies** in the client's voice:
   - one that adds a specific (a number from receipts, a named tool, a step, a short story);
   - one that is a sharp take or question.

   Keep them short: most land at 1–2 sentences. No pitch, no "great post", no links.
4. Run `lint_draft` and `fact_check` on both, and fix any HARD hit or unsourced number. Skip the
   blind lineup; speed wins here.
5. Save them with `suggest_save`: kind `reply`, target fields, batch `radar`, `cards: false`.
6. Send `send_radar_alert` with:
   - headline "⚡ Early-reply window: @author posted N min ago";
   - the tweet URL and text;
   - the 2 codes.
7. Reply `[SILENT]`. The alert is the message.

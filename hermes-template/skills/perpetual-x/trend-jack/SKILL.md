---
name: trend-jack
description: "Handle a trend radar hit: a post in the client's niche is taking off right now. Decide if the client has a real angle, draft an original take and a quote-tweet fast, and send one compact alert."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, trends, quotes]
    related_skills: [content-studio, client-voice, quote-hunter, radar-reply]
---

# Trend jack (speed matters: finish in a few minutes)

The request starts with "TREND" and gives the rising post's URL, id, author, age, likes, likes/h and
text. A topic is worth riding only while it is still climbing, so work fast. Never post anything
yourself: you save drafts and send one alert; a human decides.

1. **Is there a real angle?** The topic must matter to the client's audience, and the client must
   have something of their own to say: a number from their work, a method they use, a story, or a
   clear disagreement. If it's off-niche, a bad look (politics, tragedy, drama, pile-ons), or the
   client would only be repeating the post, reply `[SILENT]` and stop.
2. **Draft 2 options** with `content-studio` in **FAST mode** (no lineup, no long research), in the
   client's voice:
   - **Original take** (kind `tweet`): the client's own post riding the topic. It must stand on its
     own without the rising post: no "this", no "as @x said".
   - **Quote-tweet** (kind `quote`) of the rising post: a "here's what this misses / here's how we
     do it" take with something concrete. It must read well even if nobody opens the quoted post.

   No pitch, no links in the copy, no engagement bait.
3. Run `lint_draft` and `fact_check` on both. Fix any HARD hit and drop or source any unsourced
   number or claim.
4. Save both with one `suggest_save` call: batch `trend-jack`, `cards: false`, topic set; the quote
   carries `target_id`, `target_url`, `target_text` and `target_by` from the request.
5. Send ONE `send_radar_alert`:
   - headline "📈 Rising in your niche: @author, N min ago" (use the age from the request);
   - `tweet_url` and `tweet_text` of the rising post;
   - `codes`: the 2 codes from `suggest_save`.
6. Reply `[SILENT]`. The alert is the message; no document, no cards.

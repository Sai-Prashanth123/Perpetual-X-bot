---
name: repurpose
description: "Turn one source into a full set: 'repurpose A4', 'repurpose <url>', 'repurpose my best post this week'. Makes an X thread, 5 standalone tweets, a LinkedIn post, a newsletter blurb and 3 quote-card lines, all in the client's voice. One document."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, content]
    related_skills: [content-pipeline, client-voice, doc-templates, x-long-tweet]
---

# Repurpose (one idea, every format)

1. **Find the source.** Exactly one:
   - a saved draft code (A4, T12): `suggest_list` (days 60) and take the one with that code;
   - "my best post this week": `x_client_content` (days 7, refresh true), pick the original post
     with the most bookmarks, then likes;
   - a URL: an x.com link goes to `x_tweet_detail`; any other link goes to `web_extract`.

   Say which source you used at the top of the document.
2. **Pull out the core.** One idea, the receipts (numbers, names, steps) that are actually in the
   source, and the best line. Only these receipts may appear in the outputs, plus ones from
   `client-voice` receipts.
3. **Write the set** in the client's voice (load `client-voice`):
   - **X thread:** 5 to 8 posts, hook post first, one point per post, last post lands the idea.
   - **5 standalone tweets:** each a different hook (number, contrarian, story, how-to, question),
     each works without the source.
   - **LinkedIn post:** same idea, LinkedIn pacing (short lines, a line break between thoughts), no
     hashtags pile.
   - **Newsletter blurb:** 60 to 100 words that tease the idea and send readers to the source.
   - **3 quote-card lines:** under 20 words each, quotable on their own.
4. **Check every piece.** `lint_draft` and fix HARD hits; `fact_check` and remove or `[placeholder]`
   anything unsourced. Re-run until clean.
5. **Render the quote cards.** If `make_quote_card` exists, render each of the 3 lines and keep the
   image links. Otherwise leave them as text.
6. **Save** each piece with `suggest_save` (`cards: false`), format set per piece (thread, tweet,
   linkedin, newsletter, quote-card), rationale "repurposed from <source>".
7. **Publish one document** with `publish_document` (kind `package`), titled
   `Repurposed: <source in a few words>`, laid out per `doc-templates`: the source, then each format
   under its own heading with codes and Open in X links where relevant, then the quote cards.

   The summary reads: "11 pieces from <source>. Start with the thread (T…)".

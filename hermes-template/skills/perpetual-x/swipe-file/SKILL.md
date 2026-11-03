---
name: swipe-file
description: "Weekly swipe file (Wednesday, and on demand: 'swipe file', 'what worked in my niche this week'). The posts that spread among the client's radar accounts, torn down, plus 3 drafts of our own version. One document."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, research, content]
    related_skills: [content-studio, content-pipeline, client-voice, doc-templates]
---

# Swipe file (what spread in the niche, and our version of it)

1. **Collect.** Run `swipe_candidates` (days 7, top 12). It ranks original posts by engagement as a
   multiple of each author's own median, so a small account's breakout counts as much as a big
   account's. If someone named accounts, pass them as `accounts`. Note any `failed` accounts in one
   line at the end of the document; never retry them.
2. **Pick the top 5** by `multiple_of_median`, skipping anything off the client's topics or anything
   the client would never say (check `client-brief` / `client-voice`).
3. **See why they spread.** Run `x_tweet_detail` (limit 40) on each of the 5. Read the replies: what
   people quoted back, argued with, asked, or bookmarked it for.
4. **Tear each one down** in four short lines:
   - **Hook:** the first line, and the trick it uses (number, contrarian claim, named result, open loop).
   - **Structure:** format (`format` field) and how it moves (list, story, before/after, steps).
   - **Why it spread:** the evidence from step 3, not a guess.
   - **What we can borrow:** the transferable move, in one line. Never the words or the idea itself.
5. **Write 3 "our version" drafts.** Take the 3 most borrowable moves and run each through
   `content-studio` (and so `content-pipeline`): the client's own receipt and voice, the borrowed
   structure. Save each with `suggest_save` (`cards: false`, rationale names the source post).
6. **Save the winning hooks.** If `hooks_library` offers action `save`, call it for each of the 5
   hooks (text, author, url, the trick it uses). Otherwise list them under a
   "Hooks to keep" heading in the document.
7. **Publish one document** with `publish_document`, kind `answer`, titled
   `Swipe file: week of <Monday's date>` (e.g. "Swipe file: week of Nov 2"), laid out per
   `doc-templates`:
   - the 5 teardowns, each with author, `multiple_of_median` (e.g. "6.2x their usual"), metrics and an
     Open in X link;
   - the 3 drafts with codes, copy and scores;
   - the hooks (if not saved to the library).

   The summary reads: "5 posts that spread this week, 3 drafts in your voice. Start with T…".
8. **Scheduled run:** after publishing, reply with exactly `[SILENT]`. Nothing worth tearing down
   (fewer than 2 posts above 2x median) also ends with `[SILENT]` and no document.

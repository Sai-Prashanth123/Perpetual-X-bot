---
name: reply-pack
description: "Replies for the comments on one of the client's own posts or articles: 'give me replies for my latest article', 'replies for <url>'. One document with every comment worth answering and a reply in the client's voice."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, replies]
    related_skills: [content-pipeline, client-voice, doc-templates]
---

# Reply pack (answer the people who answered us)

1. **Find the post.** If a URL is given, use it. For "latest article" or "latest post", run
   `x_client_content` (days 14, refresh true) and pick the newest one of that type (an article has
   `article_title`). Say which post you picked in the document.
2. **Collect the conversation.** Run `x_tweet_detail` (limit 100) for the replies, and `x_engagers`
   (since_days 14) for quote tweets of it. Drop the client's own replies, obvious bots, and
   anything already handled (`targets_filter_new`).
3. **Read every comment and sort it** into: question, pushback, adds a point, praise, prospect
   signal (they describe a problem the client solves), or noise. Skip noise. For praise, one short
   warm reply is enough (group several if they say the same thing).
4. **Prioritise.** Questions, pushback and prospect signals come first; then bigger accounts
   (followers) and comments with likes.
5. **Draft one reply per comment worth it**, in the client's real voice:
   - answer the actual point;
   - add one specific (a number from receipts, a named tool, a step);
   - keep it short, and lowercase if that's the voice;
   - never a pitch, never "great question".

   Use `research_memory` for context.
6. **Run the pipeline in a batch.** `lint_draft` and `fact_check` every reply (fix HARD hits and
   unsourced numbers). Run `blind_lineup` on the 3 longest.
7. **Save without cards.** Use `suggest_save` with `cards: false`, kind `reply`, and the target
   fields filled.
8. **Publish one document** with `publish_document` (kind `reply-pack`), using the reply-pack
   template in `doc-templates`. It contains:
   - the post;
   - a one-paragraph read of the conversation (what people are asking, any objection pattern, any
     leads);
   - each comment with its reply, code, Open in X link, and one line on why.

   The summary reads: "N replies ready for <post>. Start with R…, R…, R…".

---
name: daily-brief
description: "The one unasked message each day: a short brief on yesterday's results, today's best opportunities, ready-to-post drafts and one growth move. Published as a document."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, brief]
    related_skills: [doc-templates, content-pipeline, deep-research]
---

# Daily brief

1. **Load context.** Run `research_memory` (read, days 7) and `outcomes_sync` (days 2).
2. **Yesterday.** List what was posted and how it did vs the 30-day median (`x_client_content`,
   `x_client_analytics`). Add one sentence on why.
3. **Today's 3 best opportunities.** Pull them from research memory plus a quick `x_find_tweets` /
   `x_trends` on the niche (at most 2 `web_search` calls). Give each a link and the angle.
4. **Post today.** Write 2-3 drafts (a tweet, a reply to a big conversation, a quote) through
   `content-pipeline`, saved with `suggest_save` (`cards: false`).
5. **One growth move** for this week, from research memory, with the evidence.
6. **Waiting for review.** Drafts still pending (`suggest_list` status pending, last 3 days), by
   code, at most 5.
7. **Publish.** Run `publish_document` (kind `brief`, title "Daily brief: <weekday, date>") with the
   brief template. Keep the summary to 3 lines max. Then reply `[SILENT]`, because the summary is
   already in the channel.

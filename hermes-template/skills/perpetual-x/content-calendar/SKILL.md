---
name: content-calendar
description: "Plan the client's next week of posts: slots with day and time, a ready, pipeline-checked draft for each, published as one plan document. Approved slots get a reminder in the channel at slot time. On demand ('plan next week') and Fridays."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, planning]
    related_skills: [content-pipeline, doc-templates, research-memory, post-mortem]
---

# Content calendar

1. **Inputs:**
   - `research_memory` (findings, growth moves);
   - `postmortems` (action recent, days 30), for what wins and flops;
   - `x_client_analytics` (kind rhythm) for the best hours;
   - `content_history`, so nothing repeats;
   - the package in `client-brief`.
2. **Shape the week.** By default, one post a weekday plus the article day. Mix formats the way
   the playbook and post-mortems suggest, for example:
   - an article (if the package has one);
   - a long how-to;
   - 2 short opinion posts;
   - a free-resource post.

   Put each in the client's best hour for that format, in the client's timezone. No two posts
   within 3 hours of each other.
3. **Draft every slot** through `content-pipeline`, and save with `suggest_save` (`cards: false`).
   Weekly-package pieces count as slots.
4. Call `content_calendar` (action save) with `[{code, slot_at, note}]`. `slot_at` is an ISO time
   with the timezone offset.
5. **Publish the plan** with `publish_document` (kind `package`, title "Plan: week of <date>"),
   using the calendar template:
   - day by day: the time, the code, the format, the copy in a quote, why this slot, and sources;
   - the summary says "Reply `ok week` to approve all, or `ok T4` per post".
6. **"ok week" / "approve the plan"** → `content_calendar` (action approve) with no codes. Single
   `ok T4` → `suggest_feedback` approve. Both count as approval for reminders.
7. **Reminders are automatic.** At each approved slot, the channel gets "⏰ Time to post T4" with
   Open in X. The daily brief shows today's slots and any skipped ones.

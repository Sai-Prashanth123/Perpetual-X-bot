---
name: post-mortem
description: "Judge the client's posts that are 48 hours old: win, par or flop against the client's own 30-day median for that format, and exactly why. Feeds the brief, the weekly report and the learning review."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, learning]
    related_skills: [playbook, performance-report, learning-review]
---

# Post-mortem

1. Call `postmortems` (action open). Each row has `metrics`, `baseline` (the client's 30-day
   medians for that format) and `vs_median` (ratios).
2. **Verdict per post:**
   - **win:** views or bookmarks ≥ 1.5× median.
   - **flop:** views < 0.6× median.
   - **par:** everything else.

   Use judgement when they disagree: bookmarks matter more than likes for how-to posts.
3. **Why, specifically.** Open the post (url) and compare it with the client's recent winners in
   that format. Name the cause:
   - the hook (first line);
   - the topic (in-lane or not);
   - timing (day and hour);
   - format and length;
   - whether a big account engaged;
   - whether a link was in the body.

   One or two sentences. Never "the algorithm".
4. **Lesson.** What to repeat or avoid next time, in one line. Leave it empty if nothing new is
   learned.
5. Call `postmortems` (action judge) for each post.
6. If a lesson is new or confirms a pattern twice, save it with `research_memory` (add, kind
   content), with the post URL as its source.
7. Reply `[SILENT]`. Post-mortems show up in the daily brief and the weekly report, never as a
   separate message.

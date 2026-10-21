---
name: agency-requests
description: "Handle requests sent to this client's bot from the agency dashboard or the Main X agent (e.g. 'draft a quote for <url>', 'ideas about <topic>')."
version: 2.0.0
metadata:
  hermes:
    tags: [perpetual-x, agency]
---

# Agency requests

1. `agency_requests_take` — returns each pending request once.
2. Handle every request the way you would if someone had typed it in this channel, at full depth:
   - "replies for my latest article/post" or a URL of the client's own post → `reply-pack`
   - someone else's tweet URL → replies + quotes (`reply-hunter` / `quote-hunter` rules)
   - "what's working", "how do we grow", "review the profile" → `deep-research`
   - "produce your weekly package" → `weekly-package`
   - "write a promo QRT for … article A3" → `article-qrt` with the parent fields
   - a topic → `tweet-ideas`
   - a request starting "RADAR" (kind radar) → `radar-reply` (fast; it sends its own alert, no document)
   - "find leads", "who in my comments could buy?" → `lead-finder`
   - "plan next week", "make a content calendar" → `content-calendar`
   - "why did my post flop / win?" → `post-mortem` on that post (`postmortems` action recent first)
   Always through `content-pipeline`, always starting from `research_memory`.
3. Publish the result with `publish_document`, passing the request's `requester_slack` as both
   `asked_by_slack` and `dm_to`, so the person who asked gets the link by DM. Title it after the ask.
4. Reply with nothing more than one line. No requests → `[SILENT]`.

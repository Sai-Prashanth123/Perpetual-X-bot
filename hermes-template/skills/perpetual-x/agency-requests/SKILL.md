---
name: agency-requests
description: "Handle requests sent to this client's bot from the agency dashboard or HQ (e.g. 'draft a quote for <url>', 'ideas about <topic>')."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, agency]
---

# Agency requests

1. `agency_requests_take` — returns each pending request once.
2. Handle every request the way you would if the employee had typed it in this channel (a pasted URL →
   replies + quotes, a topic → tweet ideas, etc.), with all the usual rules: save first, codes, links.
3. Start the message with who asked: "Requested by <name> from the dashboard:".
4. No requests → `[SILENT]`.

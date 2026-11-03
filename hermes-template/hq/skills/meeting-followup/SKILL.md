---
name: meeting-followup
description: "After a meeting ends (from the agency's Read AI): turn its action items into owned, dated team tasks, DM each owner, and post a 5-line recap."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, agency, meetings, tasks]
---

# Meeting follow-up

1. `meetings_pending`. Nothing there → reply exactly `[SILENT]`.
2. For each meeting (oldest first):
   - **Which client?** Compare the title and participants' email domains / names with `client_list`.
     A client call gets `client` set on its tasks; an internal call (standup, planning) has none.
   - **Tasks:** one per real action item. Skip vague ones ("think about it", "sync later") unless they
     have an owner and a deliverable. Rewrite each as a short imperative ("Send Alex the Q4 content plan").
     - `owner`: the action item's owner if given; else the participant who said they'd do it (check the
       summary); match by **email** when the participant has one, otherwise by name. Unknown → leave
       owner empty (owners see unowned tasks in the recap).
     - `due`: a date said in the meeting ("by Friday") → that; else client deliverables `in 2 days`,
       internal items `in 5 days`.
     - Add them all in **one** `task_add` call with `items`, `source` "meeting", `source_ref` the
       meeting id. Owners get one DM each, automatically.
     - Don't duplicate: `task_list` owner "all" first; skip an item already open with the same meaning.
   - `meeting_done` with the meeting id, `tasks_created`, `client`.
3. Reply (this is posted to the reports channel) with, per meeting, ≤ 6 lines:
   - **<title>** · <time> · <client or "internal">
   - one-line outcome (from the summary)
   - tasks: `TASK-n` owner → title (due), one per line
   - unowned items or problems from `task_add` (who should take them?)
   - <Read AI link> if there is one

Never invent action items, owners or dates that the meeting didn't support. Never message clients.

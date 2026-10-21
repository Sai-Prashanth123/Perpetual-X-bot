---
name: lead-finder
description: "Find people in the client's comments and quote tweets who look like buyers (fit the ICP and show a buying signal), and publish a leads document with a suggested DM opener for each. Weekly, or on demand ('find leads from my last 2 weeks')."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, leads]
    related_skills: [doc-templates, client-voice]
---

# Lead finder

Leads are for the client's team to contact by hand. Never DM, follow or contact anyone yourself.

1. **ICP.** Call `client_icp`. If it's empty, write one from `client-voice` receipts and the client
   brief (roles, company types and sizes, the problems the client solves), save it, and say so in
   the document.
2. **Gather engagement.** Run `x_client_content` (days 14) and pick the 8 posts with the most
   replies. Run `x_tweet_detail` (limit 60) on each, and `x_engagers` (since_days 14). Collect
   every commenter and quoter, with what they said and the link.
3. **Drop the noise:**
   - the client, teammates, and competitors on the watch list;
   - bots, and one-word praise;
   - anyone already listed (`leads` action known).
4. **Look up the best candidates.** Run `x_user_info` on the 25 strongest by signal: questions
   about "how", mentions of their own team, tooling, pipeline or hiring, or complaints about the
   problem.
5. **Score fit from 1 to 10** against the ICP:
   - role and seniority;
   - company type and size (from bio, website, pinned post);
   - the strength of the buying signal in what they said.

   Keep 6 and up. Be honest: a peer creator or a student is not a lead.
6. **For each lead, write:**
   - **why:** one line tying them to the ICP and the signal;
   - **opener:** a suggested first DM in the client's voice. It references what they said. It is
     short, curious, not salesy, and has no link.
7. Save with `leads` (action save) to get L codes.
8. Publish with `publish_document` (kind `answer`, title "Leads: <date range>"), using the leads
   template in `doc-templates`. Order the leads by fit score.
   - Summary: "N leads (M strong). Start with L…".
   - Pass `asked_by_slack` and `dm_to` when you have a requester.
9. When people report back ("L3 contacted", "L5 not a fit, too small"), call `leads` (action
   update). Save each not-a-fit reason with `research_memory` (kind note), so the ICP gets sharper.

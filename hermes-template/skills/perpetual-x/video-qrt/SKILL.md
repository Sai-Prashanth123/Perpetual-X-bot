---
name: video-qrt
description: "Write a video QRT: quote-tweet copy designed to sit on top of a short clip, plus a clip brief so a person can attach the right video."
version: 1.0.0
metadata:
  hermes:
    tags: [perpetual-x, formats, qrt, video]
    related_skills: [content-pipeline, client-voice]
---

# Video QRT

Read `agency-writing-standards/references/video-qrt-writer.md`, `proven-formats-2026.md`,
`meme-native-formats.md` and `video-qrt-examples.md` first.

- "The video does the entertaining": the text is the setup, the clip is the payoff. **Always** pair
  with a clip.
- Most winners are **self-QRTs**: quote the client's own recent post or article (`x_client_content`
  for their best recent one) with a clip that re-frames it. Otherwise quote a rising post in their niche.
- Write **3 variants** in one draft: A direct, B self-deprecating, C provocative. Short, often
  lowercase if that's their voice. For Mich: contradiction, "meanwhile me", POV or reaction formats;
  operator-architect tone, personal receipts, no profanity, no hype.
- Add a **clip brief** at the end of the text, after a line `---`:
  `Clip: <what the clip shows, 3–8 s>  ·  Search: "<what to search for>"  ·  Why: <the joke/contrast>`

Save with `kind: "video_qrt"` (the clip is attached by a person in X), `target_url` / `target_id` of
the post being quoted, and the usual scores and references.

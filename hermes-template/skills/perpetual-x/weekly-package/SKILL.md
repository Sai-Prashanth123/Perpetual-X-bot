---
name: weekly-package
description: "Produce this client's weekly content package: 1 X article (+ teammate/self promo QRTs requested), 1 long tweet, and any extras in the package (e.g. video QRTs). Then DM a summary."
version: 2.0.0
metadata:
  hermes:
    tags: [perpetual-x, package]
    related_skills: [content-pipeline, x-article, x-long-tweet, video-qrt, article-qrt]
---

# Weekly package

1. Read the package from `client-brief` (default: 1 article, 1 long tweet, article QRTs on).
2. `content_history` and `x_trends` once — pick topics that are fresh for this client and timely.
3. **Article** → `x-article` (full pipeline) → save → `request_article_qrts` with its code.
4. **Long tweet** → `x-long-tweet` on a *different* idea from the article.
5. **Extras** → e.g. `video_qrt: 2` → two separate `video-qrt` drafts (different source posts).
6. Save every piece with `suggest_save` (`cards: false`), then publish ONE document with
   `publish_document` (kind `package`) using the content-package template in `doc-templates`: what's in
   the pack, the post order, then each piece with its full text, scores, fact-check, what to check, and
   sources. Pass `asked_by_slack` and `dm_to` from the request when there is one.
7. If nobody is DM'd by the publish, `notify_team` with one line and the document link.
8. Your channel reply: nothing more (the summary message is already there).

If a piece can't clear the pipeline, deliver the rest and say which piece is blocked and why.

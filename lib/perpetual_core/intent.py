"""Understand the message before answering it.

A fast model reads the person's message together with what it is most likely replying to (the last
exchanges in this chat and anything the bot posted here on its own, like a task check-in) and says:
what they mean, what it refers to, what they want done, and whether it's clear enough to act on.
Unclear → the agent asks the one question that settles it instead of guessing.

The result goes into the turn's context as a short "How to read this message" note. Never blocks a reply:
any failure (no key, timeout, bad JSON) just skips the note.
"""

import json

from .settings import env

SCHEMA = {
    "type": "object",
    "properties": {
        "means": {"type": "string"},
        "replying_to": {"type": "string"},
        "wants": {"type": "string"},
        "clear": {"type": "boolean"},
        "question": {"type": "string"},
    },
    "required": ["means", "replying_to", "wants", "clear", "question"],
    "additionalProperties": False,
}
SKIP = {"", "none", "ok", "okay", "thanks", "thank you", "ty", "thx", "hi", "hello", "hey", "👍", "yes", "no"}


def understand(message, recent=(), bot_posts=(), who=None, client=None):
    """{'means', 'replying_to', 'wants', 'clear', 'question'} or None."""
    text = (message or "").strip()
    if text.lower() in SKIP and not bot_posts and not recent:
        return None
    if client is None:
        if not env("ANTHROPIC_API_KEY"):
            return None
        import anthropic
        client = anthropic.Anthropic(api_key=env("ANTHROPIC_API_KEY"), timeout=12, max_retries=0)
    ctx = []
    for r in recent:
        ctx.append(f"THEM: {r['user_text'][:500]}\nBOT: {(r['reply_text'] or '')[:500]}")
    for p in bot_posts:
        ctx.append(f"BOT (sent on its own, e.g. a scheduled check-in): {p[:700]}")
    prompt = (
        "You help a Slack work assistant understand a teammate's message before it answers. "
        f"The teammate is {who or 'a teammate'}. Here is the conversation so far in this chat (oldest first), "
        "then their new message.\n\n" + ("\n\n".join(ctx) or "(no earlier messages)") +
        f"\n\nNEW MESSAGE: {text[:3000]}\n\n"
        "Say:\n- means: what they mean, in plain words (resolve 'it', 'this', 'all', 'done', 'yes' using the context).\n"
        "- replying_to: which earlier message it answers ('' if it's a new request).\n"
        "- wants: what the assistant should do now, concretely (e.g. 'acknowledge; mark TASK-27, 28 as doing', "
        "'add 3 tasks', 'answer the question about X', 'nothing beyond a short thanks').\n"
        "- clear: false only if a reasonable colleague genuinely couldn't tell what they want or who/what it's "
        "about, or acting on a wrong guess would cause real work or confusion. Short replies to a clear "
        "question ARE clear.\n- question: if not clear, the ONE short question that would settle it; else ''.")
    try:
        resp = client.messages.create(
            model=env("PX_ROUTINE_MODEL") or "claude-haiku-5-5", max_tokens=800,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}})
        if getattr(resp, "stop_reason", None) == "refusal":
            return None
        raw = next(b.text for b in resp.content if getattr(b, "type", "") == "text")
        out = json.loads(raw)
    except Exception:  # noqa: BLE001 — understanding is a bonus; never block the reply
        return None
    return out if isinstance(out, dict) and out.get("means") else None


def note(u):
    """The context lines for a turn."""
    if not u:
        return []
    lines = ["How to read their message (pre-read; trust it unless the chat clearly says otherwise):",
             f"- They mean: {u['means']}"]
    if u.get("replying_to"):
        lines.append(f"- It answers: {u['replying_to']}")
    if u.get("clear", True):
        lines.append(f"- Do: {u['wants']}")
    else:
        lines.append(f"- It's ambiguous. Before doing anything, ask exactly one short question: "
                     f"\"{u.get('question') or 'Could you say a bit more about what you need?'}\" "
                     "(you may say in a few words what you understood so far).")
    return lines

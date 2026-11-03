"""Tool guard: everyone in the workspace can talk to the bot, so the powerful tools (shell, code
execution, browser, file access) are for the agency owners only, and nobody (owners included) can
read the bot's secrets through them.

Registered as a Hermes `pre_tool_call` hook by both plugins. Hermes treats this hook as a policy
gate: a crash or timeout blocks the tool (fail closed), and so does an unknown caller.
"""

import json
import re

from .settings import env

# Tools that can run code, touch the machine or drive a browser: owners only.
POWER_TOOLS = {
    "terminal", "process_manage", "execute_code",
    "read_file", "write_file", "patch", "search_files",
    "browser_exec", "browser_cdp", "browser_dialog", "browser_vault", "browser_navigate",
    "computer_use",
}
POWER_PREFIXES = ("browser_", "computer_")

# Never allowed, for anyone: anything that would expose credentials.
SECRET_PATTERNS = [
    r"\.env\b", r"perpetual-shared", r"\bprintenv\b", r"(^|[\"'\s;&|`(])env([\"'\s;&|`)]|$)", r"/proc/[^\s]*/environ",
    r"\bset\s*$", r"mcp-tokens", r"auth\.json", r"\.ssh", r"id_rsa", r"\bdocker\b",
    r"TOKEN|SECRET|API_KEY|PASSWORD|DATABASE_URL", r"curl[^|]*\|\s*(ba)?sh",
]
_SECRET_RE = re.compile("|".join(SECRET_PATTERNS), re.I)


def owners():
    return {u.strip() for u in (env("OWNER_SLACK_IDS") or "").split(",") if u.strip()}


def is_power(tool_name):
    return tool_name in POWER_TOOLS or tool_name.startswith(POWER_PREFIXES)


def touches_secrets(args):
    try:
        text = json.dumps(args or {}, ensure_ascii=False)
    except (TypeError, ValueError):
        text = str(args)
    return bool(_SECRET_RE.search(text))


def decide(tool_name, args, user):
    """None to allow, else a block message (it becomes the tool result the model sees)."""
    if not is_power(tool_name):
        return None
    if user is None or user not in owners():
        return (f"'{tool_name}' is limited to the agency owners. Tell the person it's restricted "
                "and do the task another way, or ask an owner to run it.")
    if touches_secrets(args):
        return ("Blocked: this would expose the bot's credentials or configuration. "
                "That is never allowed, for anyone.")
    return None


def _current_user():
    try:
        from gateway.session_context import get_session_env
        return get_session_env("HERMES_SESSION_USER_ID") or None
    except Exception:  # noqa: BLE001 — no gateway session (cron, CLI): treated as nobody
        return None


def pre_tool_call(tool_name="", args=None, **_):
    msg = decide(tool_name, args, _current_user())
    if msg:
        return {"action": "block", "message": msg}
    if is_power(tool_name):
        # Owners still confirm every power-tool call in Slack.
        return {"action": "approve", "message": f"Owner check: allow {tool_name}?", "rule_key": tool_name}
    return None


def register(ctx):
    ctx.register_hook("pre_tool_call", pre_tool_call)

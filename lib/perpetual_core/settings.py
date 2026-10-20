"""Settings lookup that respects Hermes' per-profile secret scope.

Under the multiplexing gateway every client is a Hermes profile in one process, and each
profile's .env lives in a context-local scope — never in os.environ. get_secret() reads
the active profile's scope; outside the gateway (tests, scripts) it falls back to os.environ.
"""

import os
from pathlib import Path



def env(name, default=None):
    try:
        from agent.secret_scope import get_secret
    except ImportError:
        return os.environ.get(name, default)
    try:
        value = get_secret(name, None)
    except Exception:  # UnscopedSecretError outside a profile turn: process env is the answer
        value = None
    return value if value not in (None, "") else os.environ.get(name, default)


def client_handle():
    """The client this profile serves. Never falls back to another profile's value."""
    try:
        from agent.secret_scope import get_secret
        handle = get_secret("CLIENT_HANDLE", None)
    except Exception:
        handle = None
    handle = handle or os.environ.get("CLIENT_HANDLE")
    if not handle:
        raise RuntimeError("CLIENT_HANDLE is not set for this profile")
    return handle.lstrip("@").lower()


def hermes_home():
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def profile_slug(handle):
    """Hermes profile name for a client: lowercase alphanumeric."""
    import re
    return re.sub(r"[^a-z0-9]", "", handle.lstrip("@").lower())

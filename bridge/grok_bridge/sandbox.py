"""The Bridge's only non-bypassable security boundary.

Why this carries so much weight: the Phase 0 spike established that
`grok agent stdio` rejects `--allow`/`--deny` outright, and that grok never
sends `session/request_permission` -- it resolves permission internally, even
with permission_mode="default". So on the remote path there is no rule engine
and no interactive gate. What is left is grok's OS sandbox: applied to the whole
process at startup via Seatbelt, inherited by child processes, and irreversible,
so a prompt-injected model cannot talk its way out of it.

Two things this module exists to handle:

1. `grok agent` has no `--sandbox` flag. The env var GROK_SANDBOX (documented on
   the top-level flag) is the only way in, and it is honoured -- measured, not
   assumed.
2. grok is fail-OPEN: if the sandbox cannot be applied it logs a warning and
   runs unconfined. Since this is our only boundary, the Bridge is fail-CLOSED.
   `verify_enforced` reads the event log grok writes and refuses to serve unless
   the profile actually took effect.

Measured limitation, deliberately not hidden: ~/.grok/auth.json cannot be
denied, because grok reads its own credential from it -- denying it produces
"Authentication required / no auth method id provided". The agent's shell tool
can therefore read grok's own xAI token. No sandbox rule can fix that; process
isolation (a dedicated user with its own GROK_HOME) would be the real answer.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import config

log = logging.getLogger("grok_bridge.sandbox")


class SandboxNotEnforced(RuntimeError):
    """The sandbox did not take effect, so the Bridge must not serve."""


_PROFILE_TEMPLATE = '''
[profiles.{name}]
# Written by grok-remote-bridge (plan v2 §0.6c). Safe to edit; the Bridge only
# adds this block if it is missing, and never rewrites an existing one.
#
# read everywhere / write only CWD + /tmp + ~/.grok / child network allowed
extends = "workspace"

# Child-process network stays on so npm install, git push etc. keep working.
# It would only ever restrict child processes anyway -- the agent's own HTTP
# (model API, web fetch) is unaffected -- so this is not an exfiltration control.
restrict_network = false

# Denied entirely, reads included. grok's built-in profiles only *write*-protect
# these, but reading is what leaks a key. Literal paths, not globs.
#
# ~/.grok/auth.json is intentionally absent: grok needs to read its own
# credential, and denying it breaks the agent outright.
deny = [
{deny}
]
'''


def ensure_profile() -> Path:
    """Make sure our profile exists in ~/.grok/sandbox.toml, without clobbering.

    The file is the user's; other profiles in it are left alone, and an existing
    [profiles.grok-remote] block is treated as deliberate and kept.
    """
    path = config.SANDBOX_PATH
    marker = f"[profiles.{config.SANDBOX_PROFILE_NAME}]"
    existing = path.read_text() if path.exists() else ""
    if marker in existing:
        return path

    deny = "\n".join(
        f'  "{Path(p).expanduser()}",' for p in config.SANDBOX_DENY_PATHS
    )
    block = _PROFILE_TEMPLATE.format(name=config.SANDBOX_PROFILE_NAME, deny=deny)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text((existing.rstrip() + "\n" if existing.strip() else "") + block)
    log.info("wrote sandbox profile %s to %s", config.SANDBOX_PROFILE_NAME, path)
    return path


def _events_since(since: float) -> list[dict]:
    path = config.SANDBOX_EVENTS_PATH
    if not path.exists():
        return []
    out = []
    for line in path.read_text(errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        ts = event.get("timestamp")
        if not isinstance(ts, str):
            continue
        # "2026-09-08T08:03:50.162801Z"
        try:
            from datetime import datetime, timezone
            parsed = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if parsed.replace(tzinfo=timezone.utc).timestamp() >= since:
                out.append(event)
        except ValueError:
            continue
    return out


def verify_enforced(*, since: float, timeout: float = 10.0) -> dict:
    """Confirm the agent we just spawned is actually confined.

    grok writes a ProfileApplied event at startup; `enforced` is the field that
    distinguishes a real Seatbelt policy from the fail-open path. Raises rather
    than returning False, because the caller must not fall back to serving an
    unconfined agent.
    """
    deadline = time.monotonic() + timeout
    last_seen: dict | None = None
    while time.monotonic() < deadline:
        for event in _events_since(since):
            if event.get("event_type") != "ProfileApplied":
                continue
            if event.get("profile") != config.SANDBOX_PROFILE_NAME:
                continue
            last_seen = event
            if event.get("enforced") is True:
                return event
        time.sleep(0.25)

    if not config.SANDBOX_REQUIRED:
        log.warning("sandbox not confirmed enforced, continuing because "
                    "GROK_BRIDGE_ALLOW_UNSANDBOXED=1 (development only)")
        return last_seen or {}

    detail = (f"profile applied but enforced={last_seen.get('enforced')!r}"
              if last_seen else "no ProfileApplied event was written")
    raise SandboxNotEnforced(
        f"sandbox profile {config.SANDBOX_PROFILE_NAME!r} is not enforcing: {detail}. "
        "Refusing to run an unconfined agent -- this is the only boundary the "
        "remote path has. Set GROK_BRIDGE_ALLOW_UNSANDBOXED=1 to override locally."
    )

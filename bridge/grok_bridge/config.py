"""Bridge configuration.

Kept as plain module-level values (overridable via env vars) rather than a
config-file framework — this is a single-user, single-Mac service, a
pydantic Settings class would be ceremony without payoff here.
"""

from __future__ import annotations

import os
from pathlib import Path

HOME = Path.home()

GROK_BIN = os.environ.get("GROK_BIN", str(HOME / ".grok" / "bin" / "grok"))

BRIDGE_DIR = Path(os.environ.get("GROK_BRIDGE_DIR", str(HOME / "grok-remote-bridge")))
DB_PATH = BRIDGE_DIR / "bridge.db"
UPLOADS_DIR = BRIDGE_DIR / "uploads"

# cwd security boundary (see plan v1.3 §1 "cwd 安全边界").
#
# This user's actual projects live directly under $HOME (~/pianotuner_film,
# ~/voidself, ~/test-agent-project, ...) rather than under a single
# "~/Projects"-style root, so $HOME itself is the only allowed_root that
# actually satisfies "can point at any of my project directories". The real
# safety boundary is the sensitive-subdirectory denylist below, not the root.
ALLOWED_ROOTS = [Path(p).expanduser() for p in
                  os.environ.get("GROK_BRIDGE_ALLOWED_ROOTS", str(HOME)).split(":")]

# Any cwd whose resolved path is under one of these is refused outright,
# regardless of ALLOWED_ROOTS. Names, not a blanket "no dotfiles" rule --
# a project's own .git/.venv/node_modules/.cache must keep working.
DENIED_SUBPATH_NAMES = {
    ".ssh", ".aws", ".gnupg", ".config", ".docker",
}
DENIED_ABSOLUTE_PATHS = [
    HOME / "Library",  # covers Library/Keychains, Library/Application Support, etc.
]

# grok --allow/--deny rules applied to every remotely-triggered call.
# Deliberately NOT `--always-approve` (see plan v1.3 "权限/安全默认值") --
# headless mode has no interactive approval channel, so instead of a blanket
# yes-to-everything flag, ALLOW_RULES grants the tool categories the agent
# needs to actually work, and DENY_RULES (which wins on conflict) carves out
# the specific dangerous operations. Separate from this Mac's interactive-TUI
# `permission_mode = always-approve`, which is untouched.
ALLOW_RULES = ["Bash", "Edit", "Write", "Read", "Grep", "WebFetch"]

DENY_RULES = [
    'Bash(rm*)',
    'Bash(sudo*)',
    'Bash(ssh*)',
    'Bash(curl*)',
    'Bash(wget*)',
    'Bash(chmod*)',
    'Bash(chown*)',
    'Bash(launchctl*)',
    'Bash(defaults*)',
    'Read(**/.ssh/**)',
    'Read(**/.aws/**)',
    'Read(**/.env)',
    'Read(**/*.pem)',
    'Read(**/*.key)',
    'Grep(**/.ssh/**)',
]

# Upload limits (plan v1.3 §1 "上传大小/类型限制").
MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_FILE_BYTES = 100 * 1024 * 1024
ALLOWED_UPLOAD_EXTENSIONS = {".png", ".jpg", ".jpeg", ".pdf", ".txt", ".md", ".json"}

DEFAULT_MODEL = "qwen3.8-27b-uncensored"
DEFAULT_REASONING_EFFORT = "high"

# --- ACP agent process management (plan v2 §1.1) -----------------------------
#
# One `grok agent stdio` process per Bridge session. cwd is a session/new
# parameter in ACP mode (unlike the old `grok -p --cwd`), so per-session
# processes map 1:1 onto sessions.cwd and a crash affects one conversation.

# Reap an idle agent after this long. Safe because grok persists its own
# session to disk and session/load resumes losslessly -- verified by the Phase 0
# spike, which restored context across a process restart.
ACP_IDLE_TTL_SECONDS = 900          # 15 minutes
ACP_REAPER_INTERVAL_SECONDS = 60

# Only IDLE agents are ever reaped. A tool that has been running for a long
# time without producing output is not idle, and when every slot is busy the
# next request is refused rather than something in flight being killed.
ACP_MAX_LIVE_AGENTS = 6

ACP_REQUEST_TIMEOUT_SECONDS = 120.0   # ordinary RPCs (initialize, set_model, ...)
ACP_PROMPT_TIMEOUT_SECONDS = 1800.0   # a turn may legitimately run for a while
ACP_STARTUP_TIMEOUT_SECONDS = 90.0
ACP_STDERR_RING_BYTES = 16384         # bounded; never forwarded verbatim to a client

# --- sandbox: the ONLY non-bypassable security boundary (plan v2 §0.6c) ------
#
# The Phase 0 spike established that `grok agent stdio` accepts neither --allow
# nor --deny (the flag is rejected outright), so the permission-rule engine that
# guarded the old `grok -p` path does not exist here, and grok never delegates
# approval to the ACP client either. What remains is grok's OS-level sandbox:
# applied to the whole process at startup via Seatbelt, inherited by child
# processes, and irreversible -- "the model cannot convince the agent to relax
# restrictions at runtime".
SANDBOX_PROFILE_NAME = "grok-remote"
SANDBOX_PATH = HOME / ".grok" / "sandbox.toml"
SANDBOX_EVENTS_PATH = HOME / ".grok" / "sandbox-events.jsonl"

# Paths the agent may not read OR write. grok's built-in profiles only
# write-protect these; reading is what leaks a key, so they are denied outright.
SANDBOX_DENY_PATHS = [
    "~/.ssh", "~/.aws", "~/.gnupg", "~/.grok/auth",
    "~/Library/Keychains", "~/.config",
]

# grok is fail-OPEN: if the sandbox cannot be applied it logs a warning and
# runs unconfined. Since this is our only boundary, the Bridge is fail-CLOSED --
# it verifies the profile actually took effect and refuses to serve otherwise.
# Set GROK_BRIDGE_ALLOW_UNSANDBOXED=1 to override (local development only).
SANDBOX_REQUIRED = os.environ.get("GROK_BRIDGE_ALLOW_UNSANDBOXED", "") != "1"

# --- pairing / auth hardening (plan v2 §0.2) ---------------------------------
#
# Cloudflare Access was removed (see plan v2 §0.1): an Access Service Token is a
# machine-to-machine client secret and cannot be shipped inside an app bundle.
# That collapses two identity layers into one, so the Bridge's own pairing flow
# is now the ONLY thing between the public tunnel and an agent that can run
# shell commands. Hence: pairing tokens expire, pairing is rate limited, and the
# endpoint hides itself when there is nothing to redeem.

# A pairing token is meant to be typed/scanned within a minute of install.sh
# printing it. Before this, tokens never expired -- a QR screenshot or a stale
# terminal scrollback from months ago stayed a valid credential forever.
PAIRING_TOKEN_TTL_SECONDS = 600  # 10 minutes

# Failed-auth throttling, per client IP. Deliberately coarse: this is a
# single-user service, so any real traffic pattern stays far below these.
AUTH_FAIL_WINDOW_SECONDS = 300
AUTH_FAIL_MAX = 10          # failures per window before a client is refused
PAIR_ATTEMPT_WINDOW_SECONDS = 300
PAIR_ATTEMPT_MAX = 5

BRIDGE_DIR.mkdir(parents=True, exist_ok=True)
UPLOADS_DIR.mkdir(parents=True, exist_ok=True)

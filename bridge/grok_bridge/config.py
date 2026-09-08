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

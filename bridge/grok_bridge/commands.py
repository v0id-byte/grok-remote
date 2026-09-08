"""The slash-command registry.

The Phase 0 spike settled what this can honestly be. grok exposes 43 commands
over ACP, but most of them are this machine's installed skills and plugins
(apple-design, easyeda-pro, deep-research...). The TUI commands people actually
reach for -- /model, /new, /effort, /plan, /status, /mcp, /skills, /export --
are NOT among them. "Slash commands are a TUI feature" is literally true of the
protocol.

So the registry has three kinds, and the UI labels each one's origin:

  acp     discovered live from available_commands_update. Executed by sending
          the command text as an ordinary prompt, which is how ACP clients
          invoke agent commands -- there is no executeCommand RPC.
  bridge  implemented here against session state or the ACP session RPCs,
          because grok does not expose them over ACP at all.
  shell   delegated to a `grok <subcommand>` process. `grok inspect --json` is
          preferred wherever it covers the ground, being machine-readable.

Argument metadata is included so a phone can render a picker instead of making
someone type a model id on a touchscreen.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from . import config, grok_disk

log = logging.getLogger("grok_bridge.commands")

PERMISSION_MODES = ["default", "acceptEdits", "auto", "dontAsk", "bypassPermissions", "plan"]


class CommandError(RuntimeError):
    pass


# name -> spec. `argType` drives the client's input affordance:
#   none | text | enum   (enum options are resolved per session, see describe())
BRIDGE_COMMANDS: dict[str, dict[str, Any]] = {
    "model": {
        "description": "Switch the model for this session",
        "argType": "enum", "argHint": "<model-id>", "aliases": ["m"],
        "group": "Model",
    },
    "effort": {
        "description": "Set reasoning effort (restarts the agent, keeps context)",
        "argType": "enum", "argHint": "<level>", "aliases": [],
        "group": "Model",
    },
    "rename": {
        "description": "Rename this session",
        "argType": "text", "argHint": "<title>", "aliases": [],
        "group": "Session",
    },
    "clear": {
        "description": "Start a fresh conversation in the same directory",
        "argType": "none", "argHint": None, "aliases": ["new"],
        "group": "Session",
    },
    "cwd": {
        "description": "Show this session's working directory",
        "argType": "none", "argHint": None, "aliases": [],
        "group": "Session",
    },
    "help": {
        "description": "List available commands",
        "argType": "none", "argHint": None, "aliases": [],
        "group": "Session",
    },
}

# name -> (argv builder, spec). Kept read-mostly on purpose: a command that
# reconfigures the Mac is not something to expose to a phone by default.
SHELL_COMMANDS: dict[str, dict[str, Any]] = {
    "status": {
        "argv": ["inspect", "--json"], "json": True,
        "description": "What grok discovers for this directory",
        "argType": "none", "group": "Diagnostics",
    },
    "skills": {
        "argv": ["inspect", "--json"], "json": True, "extract": "skills",
        "description": "Skills available in this directory",
        "argType": "none", "group": "Project",
    },
    "mcp": {
        "argv": ["mcp", "list"], "json": False,
        "description": "Configured MCP servers",
        "argType": "none", "group": "Project",
    },
    "plugins": {
        "argv": ["plugin", "list"], "json": False,
        "description": "Installed plugins",
        "argType": "none", "group": "Project",
    },
    "worktree": {
        "argv": ["worktree", "list"], "json": False,
        "description": "Tracked git worktrees",
        "argType": "none", "group": "Project",
    },
    "doctor": {
        "argv": ["doctor"], "json": False,
        "description": "Check terminal and environment support",
        "argType": "none", "group": "Diagnostics",
    },
    "du": {
        "argv": ["du"], "json": False,
        "description": "What ~/.grok uses on disk",
        "argType": "none", "group": "Diagnostics",
    },
}


def describe(
    *,
    acp_commands: list[dict[str, Any]] | None = None,
    session_models: dict[str, Any] | None = None,
    current_model: str | None = None,
) -> list[dict[str, Any]]:
    """The merged command list a client should render.

    Enum options come from the live session where possible. grok's on-disk model
    cache is only used to *describe* a model (name, context window); whether it
    can be selected is answered by the session's own availableModels, so the UI
    can never offer something this session would reject.
    """
    catalogue = grok_disk.read_models()["models"]
    available = (session_models or {}).get("availableModels") or []
    model_ids = [m.get("modelId") for m in available if m.get("modelId")]
    if not model_ids:
        model_ids = list(catalogue)

    model_options = []
    for model_id in model_ids:
        info = catalogue.get(model_id, {})
        model_options.append({
            "value": model_id,
            "label": info.get("name") or model_id,
            "detail": info.get("description"),
            "contextWindow": info.get("contextWindow"),
        })

    efforts = (catalogue.get(current_model or "", {}) or {}).get("reasoningEfforts") or []

    out: list[dict[str, Any]] = []
    for name, spec in BRIDGE_COMMANDS.items():
        entry = {
            "name": name, "source": "bridge", "kind": "bridge",
            "description": spec["description"], "aliases": spec["aliases"],
            "argType": spec["argType"], "argHint": spec["argHint"],
            "group": spec["group"],
        }
        if name == "model":
            entry["options"] = model_options
        elif name == "effort":
            # Per-model: grok-4.6 offers xhigh, grok-4.5 does not. An effort the
            # current model does not support must not be offered.
            entry["options"] = [{"value": e, "label": e} for e in efforts]
            entry["disabled"] = not efforts
        out.append(entry)

    for name, spec in SHELL_COMMANDS.items():
        out.append({
            "name": name, "source": "bridge", "kind": "shell",
            "description": spec["description"], "aliases": [],
            "argType": spec.get("argType", "none"), "argHint": None,
            "group": spec["group"],
        })

    known = {c["name"] for c in out}
    for command in acp_commands or []:
        name = command.get("name")
        if not name or name in known:
            continue
        hint = (command.get("input") or {}).get("hint") if command.get("input") else None
        out.append({
            "name": name, "source": "acp", "kind": "acp",
            "description": command.get("description") or "",
            "aliases": [], "argType": "text" if hint else "none",
            "argHint": hint, "group": "Agent",
        })

    out.sort(key=lambda c: (c["group"], c["name"]))
    return out


def resolve(name: str) -> tuple[str, str]:
    """Map a typed name (possibly an alias) to (canonical_name, kind)."""
    for canonical, spec in BRIDGE_COMMANDS.items():
        if name == canonical or name in spec["aliases"]:
            return canonical, "bridge"
    if name in SHELL_COMMANDS:
        return name, "shell"
    return name, "acp"


async def run_shell(name: str, cwd: str, timeout: float = 30.0) -> dict[str, Any]:
    """Run a `grok <subcommand>` and return its output."""
    spec = SHELL_COMMANDS.get(name)
    if spec is None:
        raise CommandError(f"unknown command: /{name}")

    try:
        proc = await asyncio.create_subprocess_exec(
            config.GROK_BIN, *spec["argv"],
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as e:
        # Most often the session's directory has been deleted since it was
        # created -- a 400 saying so beats an unhandled 500.
        raise CommandError(f"/{name} could not run in {cwd}: {e.strerror or e}") from e
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise CommandError(f"/{name} timed out after {timeout:.0f}s") from None

    text = stdout.decode("utf-8", "replace")
    if proc.returncode != 0:
        # stderr can carry absolute paths; keep the detail short and local.
        log.warning("/%s exited %s: %s", name, proc.returncode,
                    stderr.decode("utf-8", "replace")[:400])
        raise CommandError(f"/{name} failed (exit {proc.returncode})")

    if spec.get("json"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            return {"text": text}
        section = spec.get("extract")
        return {"data": data.get(section) if section else data}
    return {"text": text}

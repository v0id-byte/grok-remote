"""Everything that reads grok's private on-disk layout, in one place.

`~/.grok/sessions/<urlencoded-cwd>/<session-id>/` is grok's own storage format,
not part of ACP and not a contract. Keeping it behind one adapter means a grok
release that moves or renames things breaks one module instead of app.py,
protocol.py and the sessions service at once.

Shapes below were read off this machine, not inferred:

  chat_history.jsonl   one JSON object per line, `type` in
                       {system, user, assistant, reasoning, tool_result}.
                       `content` is sometimes a list of blocks and sometimes a
                       bare string. `synthetic_reason` marks entries grok
                       injected itself (compaction summaries, system reminders)
                       rather than anything a person or the model said.
  summary.json         session_summary, current_model_id, git_root_dir,
                       head_branch, num_chat_messages, last_turn_summary, ...
  models_cache.json    per-model info incl. context_window and reasoning_efforts
"""

from __future__ import annotations

import json
import logging
import tomllib
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import config

log = logging.getLogger("grok_bridge.grok_disk")

GROK_HOME = Path(config.GROK_BIN).expanduser().resolve().parent.parent
SESSIONS_DIR = GROK_HOME / "sessions"
MODELS_CACHE = GROK_HOME / "models_cache.json"
CONFIG_TOML = GROK_HOME / "config.toml"

# Entries grok inserted for its own bookkeeping. Replaying them would show the
# user a "message" they never sent and the model never wrote.
_SYNTHETIC_TYPES = frozenset({"system", "reasoning", "tool_result"})


def encode_cwd(cwd: str | Path) -> str:
    return quote(str(cwd), safe="")


def session_dir(cwd: str | Path, grok_session_id: str) -> Path | None:
    """Locate a session's directory, tolerating encoding differences.

    The obvious encoding works for every path on this machine, but a fallback
    scan that decodes each directory name costs nothing and avoids a silent
    "no history" for a path that quotes differently.
    """
    direct = SESSIONS_DIR / encode_cwd(cwd) / grok_session_id
    if direct.is_dir():
        return direct

    target = str(Path(cwd))
    if not SESSIONS_DIR.is_dir():
        return None
    for entry in SESSIONS_DIR.iterdir():
        if not entry.is_dir():
            continue
        if unquote(entry.name) == target:
            candidate = entry / grok_session_id
            if candidate.is_dir():
                return candidate
    return None


def _blocks_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, list):
        return "".join(_blocks_to_text(block) for block in content)
    return ""


def read_history(
    cwd: str | Path,
    grok_session_id: str,
    *,
    limit: int = 50,
    before: int | None = None,
) -> dict[str, Any]:
    """Return a page of the conversation, newest-last.

    Paginated because a long session is thousands of turns and an iPhone should
    not be handed all of it. `before` is an index into the filtered list, so
    paging is stable even though the underlying file keeps growing.
    """
    directory = session_dir(cwd, grok_session_id)
    if directory is None:
        return {"messages": [], "total": 0, "hasMore": False, "nextBefore": None}

    path = directory / "chat_history.jsonl"
    if not path.is_file():
        return {"messages": [], "total": 0, "hasMore": False, "nextBefore": None}

    messages: list[dict[str, Any]] = []
    with path.open(encoding="utf-8", errors="replace") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                # The agent may be mid-write on the final line; a partial record
                # is not a reason to fail the whole request.
                continue

            kind = entry.get("type")
            if kind in _SYNTHETIC_TYPES:
                continue
            if entry.get("synthetic_reason"):
                # Compaction summaries and system reminders: real rows in grok's
                # history, but not part of the conversation a person had.
                continue

            text = _blocks_to_text(entry.get("content"))
            tool_calls = entry.get("tool_calls") or []
            if not text and not tool_calls:
                continue

            message: dict[str, Any] = {"role": kind, "text": text}
            if tool_calls:
                message["toolCalls"] = [
                    {"id": c.get("id"), "name": c.get("name")}
                    for c in tool_calls if isinstance(c, dict)
                ]
            if entry.get("model_id"):
                message["model"] = entry["model_id"]
            messages.append(message)

    total = len(messages)
    end = total if before is None else max(0, min(before, total))
    start = max(0, end - limit)
    return {
        "messages": messages[start:end],
        "total": total,
        "hasMore": start > 0,
        "nextBefore": start if start > 0 else None,
    }


def read_summary(cwd: str | Path, grok_session_id: str) -> dict[str, Any]:
    """grok's own metadata for a session: title, model, git branch, counts."""
    directory = session_dir(cwd, grok_session_id)
    if directory is None:
        return {}
    path = directory / "summary.json"
    if not path.is_file():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}
    return {
        "title": raw.get("session_summary") or raw.get("generated_title"),
        "model": raw.get("current_model_id"),
        "messageCount": raw.get("num_chat_messages"),
        "gitRoot": raw.get("git_root_dir"),
        "headBranch": raw.get("head_branch"),
        "lastTurnSummary": raw.get("last_turn_summary"),
        "updatedAt": raw.get("updated_at"),
    }


def read_models() -> dict[str, Any]:
    """Merge grok's model cache with the locally-defined models in config.toml.

    Both are needed: the xAI models live in models_cache.json, while this
    machine's self-hosted qwen/muse endpoints exist only as [model.*] blocks in
    config.toml. A picker built from either source alone is missing half.

    This is display metadata only. Whether a model can actually be selected is
    answered by the live session, which advertises its own availableModels.
    """
    models: dict[str, dict[str, Any]] = {}

    try:
        cache = json.loads(MODELS_CACHE.read_text(encoding="utf-8"))
        for model_id, entry in (cache.get("models") or {}).items():
            info = entry.get("info") or {}
            models[model_id] = {
                "id": model_id,
                "name": info.get("name") or model_id,
                "description": info.get("description"),
                "contextWindow": info.get("context_window"),
                "reasoningEfforts": [e.get("id") for e in (info.get("reasoning_efforts") or [])],
                "supportsReasoningEffort": info.get("supports_reasoning_effort", False),
                "source": "cache",
            }
    except (OSError, json.JSONDecodeError):
        log.debug("no usable models_cache.json")

    default_model = None
    try:
        with CONFIG_TOML.open("rb") as handle:
            toml = tomllib.load(handle)
        default_model = (toml.get("models") or {}).get("default")
        for model_id, entry in (toml.get("model") or {}).items():
            existing = models.get(model_id, {})
            models[model_id] = {
                **existing,
                "id": model_id,
                "name": entry.get("name") or existing.get("name") or model_id,
                "contextWindow": entry.get("context_window") or existing.get("contextWindow"),
                "reasoningEfforts": existing.get("reasoningEfforts", []),
                "supportsReasoningEffort": existing.get("supportsReasoningEffort", False),
                "source": "config",
            }
    except (OSError, tomllib.TOMLDecodeError):
        log.debug("no usable config.toml")

    return {"models": models, "default": default_model}

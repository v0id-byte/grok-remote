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
import re
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
    if direct.is_dir() and not direct.is_symlink():
        return direct

    target = str(Path(cwd))
    if not SESSIONS_DIR.is_dir():
        return None
    try:
        entries = list(SESSIONS_DIR.iterdir())
    except OSError:
        return None
    for entry in entries:
        if entry.is_symlink() or not entry.is_dir():
            continue
        if unquote(entry.name) == target:
            candidate = entry / grok_session_id
            if candidate.is_dir() and not candidate.is_symlink():
                return candidate
    return None


def discover_sessions() -> list[dict[str, Any]]:
    """Find Grok sessions that were not created through this Bridge.

    The directory layout is Grok's private storage format, so discovery stays
    in this adapter rather than leaking it into the HTTP layer.  A malformed
    or half-written session is skipped independently; one bad entry must not
    make the phone lose every other session.
    """
    if not SESSIONS_DIR.is_dir():
        return []

    discovered: list[dict[str, Any]] = []
    try:
        cwd_entries = list(SESSIONS_DIR.iterdir())
    except OSError:
        return []

    for cwd_entry in cwd_entries:
        if not cwd_entry.is_dir() or cwd_entry.is_symlink():
            continue
        cwd = unquote(cwd_entry.name)
        if not Path(cwd).is_absolute():
            # A session path that cannot be represented as an absolute cwd is
            # not safe to expose as runnable workspace state.
            continue
        try:
            session_entries = list(cwd_entry.iterdir())
        except OSError:
            continue
        for session_entry in session_entries:
            if not session_entry.is_dir() or session_entry.is_symlink():
                continue
            try:
                summary = read_summary(cwd, session_entry.name)
                stat = session_entry.stat()
                history_path = session_entry / "chat_history.jsonl"
                summary_path = session_entry / "summary.json"
                has_history = history_path.is_file() and not history_path.is_symlink()
                if not summary and not has_history:
                    continue
                mtimes = [stat.st_mtime]
                for path in (summary_path, history_path):
                    if path.is_file() and not path.is_symlink():
                        mtimes.append(path.stat().st_mtime)
            except OSError:
                continue

            birth = getattr(stat, "st_birthtime", stat.st_mtime)
            discovered.append({
                "cwd": cwd,
                "grokSessionId": session_entry.name,
                "summary": summary,
                "createdAt": birth,
                "lastActiveAt": max(mtimes),
            })

    return discovered


# grok wraps the actual thing a person typed in <user_query>; the surrounding
# <user_info>/<git_status>/<rules>/<image_files> text is environment context it
# assembles itself, and showing it as a "message the user sent" is noise. These
# were all read off real transcripts on this machine, not guessed.
_USER_QUERY_RE = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.DOTALL)
_ENV_BLOCK_RE = re.compile(
    r"<(user_info|git_status|rules|user_rules|system_reminder|image_files|"
    r"image_compression_notice)>.*?</\1>",
    re.DOTALL,
)


def _clean_user_text(text: str) -> str | None:
    """Reduce a stored user entry to what the person actually typed.

    Returns None for an entry that is *only* environment preamble (the first
    turn's <user_info>/<git_status>/<rules> block is stored as its own user
    record with no query in it) so the caller can drop it entirely.
    """
    queries = _USER_QUERY_RE.findall(text)
    if queries:
        return "\n\n".join(q.strip() for q in queries if q.strip()) or None
    # No explicit query wrapper: strip any environment blocks and keep the rest,
    # which is how plain user turns (no wrappers at all) survive untouched.
    stripped = _ENV_BLOCK_RE.sub("", text).strip()
    return stripped or None


def _blocks_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, dict):
        text = content.get("text")
        return text if isinstance(text, str) else ""
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
    if not path.is_file() or path.is_symlink():
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
            if not isinstance(entry, dict):
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
            if not isinstance(tool_calls, list):
                tool_calls = []

            if kind == "user":
                cleaned = _clean_user_text(text)
                if cleaned is None:
                    # Pure environment preamble, not a message anyone sent.
                    continue
                text = cleaned

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
    if not path.is_file() or path.is_symlink():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
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

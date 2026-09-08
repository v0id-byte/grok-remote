"""Translate ACP `session/update` notifications into the Bridge's own event
schema, so the iOS app never depends on grok's wire format directly.

Shapes here were captured from a real `grok agent stdio` (grok 1.0.13) during
the Phase 0 spike -- see bridge/scripts/acp_spike*.py -- not taken from docs.
Representative frames:

    {"sessionUpdate":"agent_message_chunk","content":{"type":"text","text":"I'll"}}
    {"sessionUpdate":"agent_thought_chunk","content":{"type":"text","text":"The"}}
    {"sessionUpdate":"tool_call","toolCallId":"call-...-0","title":"run_terminal_command",
     "rawInput":{...},"_meta":{"x.ai/tool":{"name":"run_terminal_command",
     "kind":"execute","label":"Run Command","read_only":false}}}
    {"sessionUpdate":"tool_call_update","toolCallId":"call-...-0","status":"completed",
     "content":[{"type":"content","content":{"type":"text","text":""}}],
     "rawOutput":{"exit_code":0,...}}
    {"sessionUpdate":"available_commands_update","availableCommands":[
     {"name":"compact","description":"...","input":{"hint":"..."}}]}

Two things this module deliberately does NOT do:

1. It never emits `message.done`. The canonical end of a turn is the response to
   `session/prompt` (ACP v1), produced by the ACP layer. grok also announces
   completion on two separate vendor channels; treating those as terminal too
   would end one turn three times. They become `turn.telemetry` instead.
2. It never emits `message.usage` from vendor telemetry. Authoritative usage
   arrives in the prompt response's `_meta.usage`, which carries cost and cache
   figures the notification stream does not.

Event names already known to the iOS client are preserved -- BridgeClient.swift
terminates a stream on message.done / message.error / cancelled, so renaming
those would force a lockstep app rewrite.
"""

from __future__ import annotations

from typing import Any

# Vendor notifications that are pure chatter for our purposes. Everything not
# listed here still reaches the client as `message.unknown`, so a new grok event
# type shows up in the app instead of silently vanishing -- the passthrough in
# the previous version of this module earned its keep and is kept.
_IGNORED_VENDOR_METHODS = frozenset({
    "_x.ai/queue/changed",
    "_x.ai/sessions/changed",
    "_x.ai/models/update",
    "_x.ai/settings/update",
    "_x.ai/announcements/update",
    "_x.ai/mcp/init_progress",
    "_x.ai/mcp/servers_updated",
    "_x.ai/mcp_initialized",
    "_x.ai/session/prompt_complete",
})

_TERMINAL_TOOL_STATUSES = frozenset({"completed", "failed", "error", "cancelled"})


def _text_of(content: Any) -> str:
    """ACP content blocks are {"type":"text","text":...}; be liberal about lists."""
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, list):
        return "".join(_text_of(c) for c in content)
    return ""


def translate(msg: dict[str, Any]) -> dict[str, Any] | None:
    """Map one ACP notification to a Bridge event, or None to drop it.

    Takes the whole JSON-RPC notification, because the channel (`method`)
    distinguishes standard ACP updates from grok's `_x.ai/*` extensions.
    """
    method = msg.get("method") or ""
    params = msg.get("params") or {}
    update = params.get("update")
    if not isinstance(update, dict):
        return None if method in _IGNORED_VENDOR_METHODS else _unknown(msg)

    kind = update.get("sessionUpdate")

    # ---- standard ACP updates -------------------------------------------
    if kind == "agent_message_chunk":
        return {"type": "message.delta", "data": _text_of(update.get("content"))}

    if kind == "agent_thought_chunk":
        return {"type": "message.thought", "data": _text_of(update.get("content"))}

    if kind == "user_message_chunk":
        # The echo of the prompt we just sent; the client already rendered it.
        return None

    if kind == "tool_call":
        meta = (update.get("_meta") or {}).get("x.ai/tool") or {}
        return {
            "type": "tool.started",
            "toolCallId": update.get("toolCallId"),
            "tool": meta.get("name") or update.get("title"),
            "label": meta.get("label"),
            "kind": meta.get("kind") or update.get("kind"),
            "readOnly": meta.get("read_only"),
            "title": update.get("title"),
            "input": update.get("rawInput"),
            "locations": update.get("locations") or [],
        }

    if kind == "tool_call_update":
        # Empirically status cycles None -> "in_progress" -> a terminal value as
        # a long-running command streams output. Treating "in_progress" as
        # terminal (an early version of this code did) shows a still-running
        # command as finished.
        status = update.get("status")
        meta = (update.get("_meta") or {}).get("x.ai/tool") or {}
        event = {
            "type": "tool.finished" if status in _TERMINAL_TOOL_STATUSES else "tool.output",
            "toolCallId": update.get("toolCallId"),
            "status": status,
            "kind": meta.get("kind") or update.get("kind"),
            "title": update.get("title"),
            "content": update.get("content") or [],
            "locations": update.get("locations") or [],
        }
        raw_output = update.get("rawOutput")
        if isinstance(raw_output, dict):
            # Keep the useful scalars; the full payload can be enormous.
            event["exitCode"] = raw_output.get("exit_code")
            event["truncated"] = raw_output.get("truncated")
        return event

    if kind == "plan":
        return {"type": "plan.updated", "entries": update.get("entries") or []}

    if kind == "current_mode_update":
        return {"type": "mode.changed", "mode": update.get("currentModeId")}

    if kind == "available_commands_update":
        commands = (update.get("availableCommands")
                    or update.get("available_commands")
                    or update.get("commands") or [])
        return {"type": "commands.available", "commands": commands}

    if kind == "config_option_update":
        return {"type": "config.changed", "config": update}

    if kind == "session_info_update":
        # grok auto-generates a session title a turn or two in.
        return {"type": "session.titled", "title": update.get("title")}

    if kind == "usage_update":
        return {"type": "message.usage", "usage": update.get("usage") or update}

    # ---- grok's vendor extensions ---------------------------------------
    if kind == "turn_completed":
        # NOT a terminal event: see the module docstring.
        return {
            "type": "turn.telemetry",
            "stopReason": update.get("stop_reason"),
            "promptId": update.get("prompt_id"),
            "elapsedMs": update.get("elapsed_ms"),
            "usage": update.get("usage"),
        }

    if kind == "retry_state":
        return {"type": "agent.retry", "detail": update}

    if kind in ("session_recap", "session_summary_generated"):
        return {"type": "session.recap", "detail": update}

    if kind and kind.startswith("auto_compact_"):
        return {"type": "session.compaction", "phase": kind, "detail": update}

    if kind in ("subagent_spawned", "subagent_finished"):
        return {"type": "subagent.status", "phase": kind, "detail": update}

    if kind in ("pending_interaction", "interaction_resolved"):
        # grok resolves these itself -- it never delegates approval to the ACP
        # client (confirmed across three spike runs, including with
        # permission_mode="default"). Surfaced for visibility, not for action.
        return {"type": "agent.interaction", "phase": kind, "detail": update}

    if kind in ("response_completed", "tool_call_delta_chunk", "model_changed"):
        return None

    if method in _IGNORED_VENDOR_METHODS:
        return None

    return _unknown(msg)


def _unknown(msg: dict[str, Any]) -> dict[str, Any]:
    return {"type": "message.unknown", "raw": msg}

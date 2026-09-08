"""Translate grok's native streaming-json (NDJSON) events into the Bridge's
own stable event schema, so the iOS app never depends on grok's CLI output
format directly (plan v1.3 §1 "协议适配层").

Real grok event shapes were captured empirically against grok 1.0.4
(`grok -p ... --output-format streaming-json`) rather than guessed from
docs -- see /tmp/grok-stream-test*.jsonl from the Phase 1 validation run:

    {"type":"available_commands","tools":[...],"commands":[...]}
    {"type":"thought","data":"..."}
    {"type":"usage","usage":{"input_tokens":N,"output_tokens":N,...}}
    {"type":"tool_call","toolCallId":"...","title":"read_file","kind":"read",
     "status":"pending","toolName":"read_file","rawInput":{...},
     "content":[],"locations":[]}
    {"type":"tool_call_update","toolCallId":"...","status":null|"completed",
     "content":[...],"rawOutput":{...},"locations":[...]}
    {"type":"error","message":"..."}

`text` and `end` are documented in ~/.grok/README.md but weren't hit in the
Phase 1 test run (it never got past the first tool call before the fixed
test budget ran out) -- their shapes are taken from the README's own
example and should be double-checked against a real run once one completes
end-to-end.
"""

from __future__ import annotations

from typing import Any


def translate(raw: dict[str, Any]) -> dict[str, Any] | None:
    """Map one raw grok NDJSON event to a Bridge event, or None to drop it."""
    rtype = raw.get("type")

    if rtype == "available_commands":
        return None  # capability listing, not per-turn signal; not forwarded

    if rtype == "thought":
        return {"type": "message.thought", "data": raw.get("data", "")}

    if rtype == "text":
        return {"type": "message.delta", "data": raw.get("data", "")}

    if rtype == "usage":
        return {"type": "message.usage", "usage": raw.get("usage")}

    if rtype == "tool_call":
        return {
            "type": "tool.started",
            "toolCallId": raw.get("toolCallId"),
            "tool": raw.get("toolName") or raw.get("title"),
            "kind": raw.get("kind"),
            "input": raw.get("rawInput"),
            "locations": raw.get("locations") or [],
        }

    if rtype == "tool_call_update":
        # Empirically (grok 1.0.4), status cycles null -> "in_progress" -> a
        # terminal value ("completed"/"failed") as a long-running command
        # streams output. Only the terminal statuses are actually done --
        # treating "in_progress" as terminal (an earlier version of this
        # code did) makes the UI show a still-running command as finished.
        status = raw.get("status")
        if status in ("completed", "failed", "error"):
            return {
                "type": "tool.finished",
                "toolCallId": raw.get("toolCallId"),
                "status": status,
                "content": raw.get("content") or [],
                "locations": raw.get("locations") or [],
            }
        return {
            "type": "tool.output",
            "toolCallId": raw.get("toolCallId"),
            "status": status,
            "content": raw.get("content") or [],
            "locations": raw.get("locations") or [],
        }

    if rtype == "end":
        return {
            "type": "message.done",
            "stopReason": raw.get("stopReason"),
        }

    if rtype == "error":
        return {"type": "message.error", "message": raw.get("message", "unknown error")}

    # Unknown event type: forward as a generic passthrough rather than
    # silently dropping it, so new grok event types are visible instead of
    # invisible. The App can safely ignore event types it doesn't recognize.
    return {"type": "message.unknown", "raw": raw}

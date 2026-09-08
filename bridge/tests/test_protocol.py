"""Event translation, pinned to frames captured from a real grok agent.

fixtures/acp_notifications.jsonl holds actual `grok agent stdio` output from the
Phase 0 spike (grok 1.0.13), covering all 26 distinct notification shapes it
produced. Pinning against recorded traffic rather than hand-written samples is
what caught the previous version of this module treating "in_progress" as a
terminal tool status.
"""
from __future__ import annotations

import json
from pathlib import Path

from grok_bridge.protocol import translate

FIXTURE = Path(__file__).parent / "fixtures" / "acp_notifications.jsonl"
FRAMES = [json.loads(line) for line in FIXTURE.read_text().splitlines() if line.strip()]


def _update(frame):
    return (frame.get("params") or {}).get("update") or {}


def _of_kind(kind, method=None):
    return [f for f in FRAMES
            if _update(f).get("sessionUpdate") == kind
            and (method is None or f.get("method") == method)]


def test_every_recorded_frame_translates_without_error():
    for frame in FRAMES:
        translate(frame)


def test_nothing_recorded_falls_through_as_unknown():
    """A frame we do not understand still reaches the client, but none of the
    shapes grok actually produced should need that escape hatch."""
    unknown = [f for f in FRAMES if (translate(f) or {}).get("type") == "message.unknown"]
    assert unknown == [], [f.get("method") for f in unknown]


def test_translate_never_emits_the_terminal_event():
    """The end of a turn is the session/prompt response, and nothing else.

    grok announces completion on `_x.ai/session/update` AND
    `_x.ai/session_notification`; if either were mapped to message.done a single
    turn would end two or three times.
    """
    types = {(translate(f) or {}).get("type") for f in FRAMES}
    assert "message.done" not in types
    assert _of_kind("turn_completed"), "fixture should contain completion frames"
    for frame in _of_kind("turn_completed"):
        assert translate(frame)["type"] == "turn.telemetry"


def test_translate_never_emits_usage_from_telemetry():
    """Authoritative usage comes from the prompt response's _meta.usage, which
    carries cost and cache figures the notification stream does not."""
    assert "message.usage" not in {(translate(f) or {}).get("type") for f in FRAMES}


def test_message_and_thought_chunks():
    msg = translate(_of_kind("agent_message_chunk")[0])
    assert msg["type"] == "message.delta" and isinstance(msg["data"], str)
    thought = translate(_of_kind("agent_thought_chunk")[0])
    assert thought["type"] == "message.thought"


def test_user_message_echo_is_dropped():
    assert translate(_of_kind("user_message_chunk")[0]) is None


def test_tool_call_carries_the_vendor_metadata_the_ui_needs():
    event = translate(_of_kind("tool_call")[0])
    assert event["type"] == "tool.started"
    assert event["tool"] == "run_terminal_command"
    assert event["kind"] == "execute"
    assert event["readOnly"] is False
    assert event["label"] == "Run Command"
    assert event["input"]["command"]


def test_tool_status_progression_only_finishes_on_a_terminal_status():
    got = [(translate(f)["type"], translate(f)["status"]) for f in _of_kind("tool_call_update")]
    assert ("tool.output", None) in got
    assert ("tool.output", "in_progress") in got
    assert ("tool.finished", "completed") in got
    # The regression this pins: "in_progress" must not read as finished.
    assert not any(t == "tool.finished" and s == "in_progress" for t, s in got)


def test_available_commands_are_surfaced():
    event = translate(_of_kind("available_commands_update")[0])
    assert event["type"] == "commands.available"
    assert event["commands"] and "name" in event["commands"][0]


def test_session_title_is_surfaced():
    event = translate(_of_kind("session_info_update")[0])
    assert event["type"] == "session.titled" and event["title"]


def test_grok_internal_permission_interactions_are_visible_but_not_actionable():
    """grok resolves permission itself and never asks the client, so these are
    reported for visibility only -- there is nothing for a user to answer."""
    event = translate(_of_kind("pending_interaction")[0])
    assert event["type"] == "agent.interaction"


def test_pure_chatter_is_dropped():
    for method in ("_x.ai/queue/changed", "_x.ai/models/update", "_x.ai/mcp/init_progress"):
        frames = [f for f in FRAMES if f.get("method") == method]
        assert frames and all(translate(f) is None for f in frames), method


def test_unrecognised_frames_still_reach_the_client():
    event = translate({"jsonrpc": "2.0", "method": "session/update",
                       "params": {"update": {"sessionUpdate": "something_new_in_1_1"}}})
    assert event["type"] == "message.unknown"

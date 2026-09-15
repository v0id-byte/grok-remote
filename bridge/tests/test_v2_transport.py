"""The resumable /v2/chat transport (plan v2 §2.1-2.3).

What this is really testing is that a phone which drops mid-turn loses nothing.
iOS suspends backgrounded apps, so a dropped socket is the normal case, not an
edge case -- v1 treated it as a cancellation and threw the turn away.
"""
from __future__ import annotations

import importlib
import tempfile
from pathlib import Path

import pytest


@pytest.fixture()
def bridge(monkeypatch):
    monkeypatch.setenv("GROK_BRIDGE_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("GROK_BRIDGE_ALLOW_UNSANDBOXED", "1")
    import grok_bridge.config as config
    importlib.reload(config)
    # The synthetic sessions below live under pytest's temporary directory;
    # keep them subject to the production allowlist rather than bypassing it.
    config.ALLOWED_ROOTS.append(Path(tempfile.gettempdir()).resolve())
    import grok_bridge.db as db
    importlib.reload(db)
    import grok_bridge.app as app
    importlib.reload(app)
    return app


@pytest.fixture()
def client(bridge):
    from fastapi.testclient import TestClient
    return TestClient(bridge.app)


def _device(client, store):
    token = store.create_pairing_token()
    r = client.post("/v1/pair", json={"pairing_token": token, "device_name": "t"})
    creds = r.json()
    return f"{creds['device_id']}.{creds['secret']}"


def _session(bridge, tmp_path):
    return bridge.store.create_session(str(tmp_path), "test")


def test_upgrade_is_rejected_without_a_valid_token(client, bridge, tmp_path):
    from websockets.exceptions import WebSocketException          # noqa: F401
    import starlette.websockets as sw
    with pytest.raises((sw.WebSocketDisconnect, Exception)):
        with client.websocket_connect("/v2/chat") as ws:
            ws.receive_json()


def test_hello_ack_reports_sequence_and_state(client, bridge, tmp_path):
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    bridge.store.update_session(sid, model="grok-4.6", reasoning_effort="high")
    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "protocolVersion": 2, "sessionId": sid,
                      "afterSeq": 0})
        ack = ws.receive_json()
    assert ack["type"] == "hello.ack"
    assert ack["sessionId"] == sid
    assert ack["currentSeq"] == 0
    assert ack["sessionState"] == "stopped"
    assert ack["config"] == {
        "model": "grok-4.6",
        "reasoningEffort": "high",
        "reasoningEffortConfigured": True,
    }


def test_events_missed_while_disconnected_are_replayed(client, bridge, tmp_path):
    """The core promise: reconnect with afterSeq and get everything since."""
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    for i in range(5):
        bridge.agents.emit(sid, {"type": "message.delta", "data": f"chunk{i}"})

    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "sessionId": sid, "afterSeq": 2})
        ack = ws.receive_json()
        assert ack["replayed"] == 3
        replayed = [ws.receive_json() for _ in range(3)]

    assert [e["data"] for e in replayed] == ["chunk2", "chunk3", "chunk4"]
    assert [e["seq"] for e in replayed] == [3, 4, 5]


def test_a_fresh_client_replays_the_whole_journal(client, bridge, tmp_path):
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    bridge.agents.emit(sid, {"type": "message.delta", "data": "only"})
    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "sessionId": sid, "afterSeq": 0})
        assert ws.receive_json()["replayed"] == 1
        assert ws.receive_json()["data"] == "only"


def test_a_fresh_entry_omitting_afterseq_does_not_replay_completed_turns(
        client, bridge, tmp_path):
    """The phone loads the transcript from grok's history; if the journal also
    replayed those turns they would appear twice. Omitting afterSeq tails."""
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    bridge.agents.emit(sid, {"type": "message.delta", "data": "old turn"})
    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "sessionId": sid})  # no afterSeq
        ack = ws.receive_json()
        assert ack["replayed"] == 0
        assert ack["currentSeq"] == 1
        bridge.agents.emit(sid, {"type": "message.delta", "data": "new"})
        assert ws.receive_json()["data"] == "new"


def test_a_fresh_entry_still_replays_an_in_flight_turn(client, bridge, tmp_path):
    """Opening a session while a turn is mid-generation should show that turn,
    even though it is not yet in the completed transcript."""
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    bridge.agents.emit(sid, {"type": "message.delta", "data": "done earlier"})
    bridge.agents.set_job(sid, "job-live")
    bridge.agents.emit(sid, {"type": "message.delta", "data": "streaming now"})
    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "sessionId": sid})  # no afterSeq
        assert ws.receive_json()["replayed"] == 1
        assert ws.receive_json()["data"] == "streaming now"


def test_live_events_stream_after_the_replay(client, bridge, tmp_path):
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "sessionId": sid, "afterSeq": 0})
        ws.receive_json()
        bridge.agents.emit(sid, {"type": "message.delta", "data": "live"})
        assert ws.receive_json()["data"] == "live"


def test_sequence_survives_a_bridge_restart(bridge, tmp_path):
    """Numbering comes from the journal, not memory: restarting must not reset
    seq to 1 and collide with numbers the client already holds."""
    sid = _session(bridge, tmp_path)
    for i in range(3):
        bridge.agents.emit(sid, {"type": "message.delta", "data": str(i)})
    assert bridge.store.latest_seq(sid) == 3

    from grok_bridge.session_manager import AcpSessionManager
    fresh = AcpSessionManager(journal=bridge.store)
    fresh.emit(sid, {"type": "message.delta", "data": "after restart"})
    assert bridge.store.latest_seq(sid) == 4
    assert bridge.store.events_after(sid, 3)[0]["data"] == "after restart"


def test_unknown_frames_are_reported_not_fatal(client, bridge, tmp_path):
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "sessionId": sid, "afterSeq": 0})
        ws.receive_json()
        ws.send_json({"type": "nonsense"})
        err = ws.receive_json()
        assert err["code"] == "unknown_frame"
        ws.send_json({"type": "ping"})
        assert ws.receive_json()["type"] == "pong"


def test_unknown_session_is_refused(client, bridge):
    bearer = _device(client, bridge.store)
    import starlette.websockets as sw
    with pytest.raises((sw.WebSocketDisconnect, Exception)):
        with client.websocket_connect(
            "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
        ) as ws:
            ws.send_json({"type": "hello", "sessionId": "nope", "afterSeq": 0})
            ws.receive_json()


def test_journal_retention_is_bounded(bridge, tmp_path):
    sid = _session(bridge, tmp_path)
    for i in range(60):
        bridge.agents.emit(sid, {"type": "message.delta", "data": str(i)})
    bridge.store.prune_session_events(sid, keep=10)
    remaining = bridge.store.events_after(sid, 0)
    assert len(remaining) == 10
    # Pruning drops the oldest and keeps sequence numbers stable.
    assert [e["seq"] for e in remaining] == list(range(51, 61))


def test_a_second_prompt_on_the_same_socket_is_refused(client, bridge, tmp_path,
                                                       monkeypatch):
    """Two devices on one session must not silently interleave turns."""
    import asyncio

    async def slow_prompt(**kwargs):
        await asyncio.sleep(2)
        return {"stopReason": "end_turn", "usage": None}

    monkeypatch.setattr(bridge.agents, "prompt", slow_prompt)
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "sessionId": sid, "afterSeq": 0})
        ws.receive_json()
        ws.send_json({"type": "prompt", "text": "first"})
        ws.send_json({"type": "prompt", "text": "second"})
        err = ws.receive_json()
    assert err["type"] == "error" and err["code"] == "session_busy"


def test_a_busy_agent_is_reported_as_an_error_event(client, bridge, tmp_path,
                                                    monkeypatch):
    """A prompt arriving from another connection while a turn runs surfaces on
    the journalled stream, so every attached device sees why it was refused."""
    from grok_bridge.acp import AgentBusy

    async def busy(**kwargs):
        raise AgentBusy("a turn is already running in this session")

    monkeypatch.setattr(bridge.agents, "prompt", busy)
    bearer = _device(client, bridge.store)
    sid = _session(bridge, tmp_path)
    with client.websocket_connect(
        "/v2/chat", headers={"Authorization": f"Bearer {bearer}"}
    ) as ws:
        ws.send_json({"type": "hello", "sessionId": sid, "afterSeq": 0})
        ws.receive_json()
        ws.send_json({"type": "prompt", "text": "go"})
        event = ws.receive_json()
    assert event["type"] == "message.error" and event["code"] == "busy"
    # It is on the journal too, so a device that reconnects still learns about it.
    assert any(e["type"] == "message.error" for e in bridge.store.events_after(sid, 0))

"""ACP core tests, driven by the deterministic fake agent.

These cover the failure modes a real-agent smoke test cannot reach on demand:
a client-side handler stalling the reader, a crash mid-turn, a malformed line,
an unsupported protocol version, and capacity/state-machine behaviour.
"""
from __future__ import annotations

import asyncio
import importlib
import os
import sys
import tempfile
from pathlib import Path

import pytest

FAKE = str(Path(__file__).parent / "fake_acp_agent.py")


@pytest.fixture()
def acp(monkeypatch):
    """Point the ACP layer at the fake agent and disable the sandbox gate.

    The sandbox is grok's, not ours -- the fake cannot apply one, and
    verify_enforced is covered separately in test_sandbox.py.
    """
    monkeypatch.setenv("GROK_BRIDGE_DIR", tempfile.mkdtemp())
    monkeypatch.setenv("GROK_BRIDGE_ALLOW_UNSANDBOXED", "1")
    monkeypatch.setenv("GROK_BIN", sys.executable)
    import grok_bridge.config as config
    importlib.reload(config)
    import grok_bridge.acp as acp_mod
    importlib.reload(acp_mod)

    # argv is [GROK_BIN, "agent", ..., "stdio"]; run the fake instead.
    def argv(self):
        return [sys.executable, FAKE]
    monkeypatch.setattr(acp_mod.AcpProcess, "_argv", argv)
    return acp_mod


async def _start(acp, cwd, scenario="basic", events=None, **kw):
    os.environ["FAKE_ACP_SCENARIO"] = scenario
    proc = acp.AcpProcess(
        session_id="s1", grok_session_id="g1", cwd=Path(cwd),
        emit=(events.append if events is not None else (lambda e: None)), **kw,
    )
    await proc.start(resume=False)
    return proc


@pytest.mark.asyncio
async def test_basic_turn_streams_and_terminates_once(acp):
    events: list[dict] = []
    cwd = tempfile.mkdtemp()
    proc = await _start(acp, cwd, "basic", events)
    result = await proc.prompt([{"type": "text", "text": "hi"}])
    await proc.close()

    assert result["stopReason"] == "end_turn"
    assert result["usage"]["costUsdTicks"] == 42
    text = "".join(e["data"] for e in events if e["type"] == "message.delta")
    assert text == "Hello world"
    assert any(e["type"] == "message.thought" for e in events)
    # The terminal event is the prompt response; the vendor channel is telemetry.
    assert [e["type"] for e in events].count("turn.telemetry") == 1
    assert not any(e["type"] == "message.done" for e in events)


@pytest.mark.asyncio
async def test_available_commands_are_captured(acp):
    cwd = tempfile.mkdtemp()
    proc = await _start(acp, cwd, "basic")
    await proc.prompt([{"type": "text", "text": "hi"}])
    names = [c["name"] for c in proc.available_commands]
    await proc.close()
    assert names == ["compact", "context"]


@pytest.mark.asyncio
async def test_tool_call_lifecycle(acp):
    events: list[dict] = []
    cwd = tempfile.mkdtemp()
    proc = await _start(acp, cwd, "tool_call", events)
    await proc.prompt([{"type": "text", "text": "run it"}])
    await proc.close()
    kinds = [e["type"] for e in events if e["type"].startswith("tool.")]
    assert kinds == ["tool.started", "tool.output", "tool.finished"]
    started = next(e for e in events if e["type"] == "tool.started")
    assert started["tool"] == "run_terminal_command" and started["readOnly"] is False


@pytest.mark.asyncio
async def test_reader_is_not_blocked_by_a_slow_request_handler(acp, monkeypatch):
    """The bug this exists for: if the reader awaited its own agent->client
    handlers, one slow handler would stall every notification and response
    behind it on the same pipe."""
    cwd = tempfile.mkdtemp()
    target = Path(cwd) / "read-me.txt"
    target.write_text("payload")
    os.environ["FAKE_ACP_READ_PATH"] = str(target)

    handler_done = asyncio.Event()
    real_read = acp.AcpProcess._fs_read

    def slow_read(self, params):
        async def finish():
            await asyncio.sleep(1.5)
            handler_done.set()
        asyncio.get_running_loop().create_task(finish())
        return real_read(self, params)

    events: list[dict] = []
    proc = await _start(acp, cwd, "reader_not_blocked", events)
    monkeypatch.setattr(acp.AcpProcess, "_fs_read", slow_read)

    result = await asyncio.wait_for(proc.prompt([{"type": "text", "text": "go"}]), 5)
    # The turn completed while the handler's own work was still outstanding.
    assert result["stopReason"] == "end_turn"
    assert not handler_done.is_set(), "reader waited for the handler before continuing"
    assert any(e.get("data") == "sent-while-request-outstanding" for e in events)
    await proc.close()


@pytest.mark.asyncio
async def test_fs_requests_are_confined_to_the_session_directory(acp):
    cwd = tempfile.mkdtemp()
    inside = Path(cwd) / "in.txt"
    inside.write_text("ok")
    os.environ["FAKE_ACP_READ_PATH"] = "/etc/passwd"      # outside the workspace
    os.environ["FAKE_ACP_WRITE_PATH"] = str(Path(cwd) / "written.txt")

    events: list[dict] = []
    proc = await _start(acp, cwd, "fs_roundtrip", events)
    await proc.prompt([{"type": "text", "text": "go"}])
    await proc.close()

    results = [e["data"] for e in events
               if e["type"] == "message.delta" and e["data"].startswith("FSRESULT:")]
    assert any("escapes the session directory" in r for r in results), results
    assert (Path(cwd) / "written.txt").read_text() == "written by agent"


@pytest.mark.asyncio
async def test_unsupported_protocol_version_fails_closed(acp, monkeypatch):
    monkeypatch.setenv("FAKE_ACP_PROTOCOL_VERSION", "2")
    cwd = tempfile.mkdtemp()
    with pytest.raises(acp.AgentUnavailable, match="unsupported ACP protocol version"):
        await _start(acp, cwd, "basic")


@pytest.mark.asyncio
async def test_crash_mid_turn_reports_sanitized_error(acp):
    events: list[dict] = []
    cwd = tempfile.mkdtemp()
    proc = await _start(acp, cwd, "crash_mid_turn", events)
    with pytest.raises(acp.AgentUnavailable):
        await proc.prompt([{"type": "text", "text": "hi"}])

    errors = [e for e in events if e["type"] == "message.error"]
    assert errors and errors[0]["code"] == "agent_exited"
    # stderr held a filesystem path; it must not be forwarded to a client.
    assert "/Users/secret/path" not in errors[0]["message"]
    assert "/Users/secret/path" in proc.stderr_tail()
    await proc.close()


@pytest.mark.asyncio
async def test_malformed_line_does_not_kill_the_stream(acp):
    events: list[dict] = []
    cwd = tempfile.mkdtemp()
    proc = await _start(acp, cwd, "malformed_line", events)
    result = await proc.prompt([{"type": "text", "text": "hi"}])
    await proc.close()
    assert result["stopReason"] == "end_turn"
    assert any(e.get("data") == "recovered" for e in events)


@pytest.mark.asyncio
async def test_second_prompt_is_refused_while_a_turn_runs(acp):
    cwd = tempfile.mkdtemp()
    proc = await _start(acp, cwd, "slow_turn")
    first = asyncio.create_task(proc.prompt([{"type": "text", "text": "slow"}]))
    await asyncio.sleep(0.4)
    with pytest.raises(acp.AgentBusy):
        await proc.prompt([{"type": "text", "text": "second"}])
    await proc.cancel()
    assert (await asyncio.wait_for(first, 10))["stopReason"] == "cancelled"
    # Still usable: no session-id rotation, unlike the SIGTERM path this replaced.
    assert proc.state is acp.SessionState.IDLE
    await proc.close()


@pytest.mark.asyncio
async def test_the_grok_session_id_is_written_back(acp, monkeypatch):
    """session/new mints grok's own id, not the one the Bridge generated.

    Losing it is silent and expensive: history lookups search a directory that
    does not exist, and a respawned agent tries to session/load an id grok never
    issued. This is the regression that made a smoke-tested session show an
    empty transcript.
    """
    from grok_bridge.session_manager import AcpSessionManager

    persisted: dict = {}

    class FakeJournal:
        def append_event(self, sid, event, job_id=None):
            return 1

        def latest_seq(self, sid):
            return 0

        def update_session(self, sid, **fields):
            persisted.update(fields)
            return True

    monkeypatch.setenv("FAKE_ACP_SCENARIO", "basic")
    manager = AcpSessionManager(journal=FakeJournal())
    cwd = tempfile.mkdtemp()
    proc = await manager.get(session_id="s1", grok_session_id="bridge-uuid",
                             cwd=Path(cwd))
    try:
        assert proc.grok_session_id == "fake-session-0001"
        assert persisted.get("grok_session_id") == "fake-session-0001"
    finally:
        await manager.shutdown()

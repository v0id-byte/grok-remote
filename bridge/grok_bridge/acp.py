"""One `grok agent stdio` process, driven as an ACP client.

Replaces the old model of spawning a fresh `grok -p` per turn. That design had
to rotate grok's session id after every cancellation, because SIGTERM left the
session lock stuck -- losing conversation continuity at the cancel point. A
resident process cancels with `session/cancel` and stays usable.

Facts this is built on, all measured in the Phase 0 spike against grok 1.0.13
(bridge/scripts/acp_spike*.py), not taken from documentation:

  * `initialize` negotiates protocolVersion 1, and advertises loadSession.
  * cwd is a `session/new` parameter here, not a process flag.
  * The response to `session/prompt` is the authoritative end of a turn, and
    carries usage (including cost) in `_meta.usage`.
  * `session/load` restores conversation context across a process restart --
    which is what makes idle reaping safe.
  * `session/set_model` works; there is no channel for reasoning effort, so
    effort is a spawn-time flag and changing it restarts the process.
  * `--allow` / `--deny` are rejected by `agent stdio`, and grok never sends
    `session/request_permission`; it resolves permission internally. The only
    real boundary is the OS sandbox (see sandbox.py).

Threading rule that matters most here: the reader task demultiplexes and
nothing else. Agent->client requests are dispatched into their own tasks, so a
slow handler can never stall the stream of notifications, responses and
cancellations behind it.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
import os
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from . import config
from .protocol import translate
from .security import InvalidCwd, validate_path_in_cwd

log = logging.getLogger("grok_bridge.acp")


class SessionState(str, Enum):
    """Explicit states, rather than a bare lock.

    A lock would make a second client's prompt wait silently, which reads to
    that user as "my message vanished". Callers get told the session is busy
    instead.
    """

    STARTING = "starting"
    IDLE = "idle"
    RUNNING = "running"
    RECOVERING = "recovering"
    CLOSED = "closed"


class AcpError(RuntimeError):
    """A JSON-RPC error returned by the agent."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message
        self.data = data


class AgentBusy(RuntimeError):
    """A turn is already running in this session."""


class AgentUnavailable(RuntimeError):
    """The agent process is gone, or was never able to start."""


def _looks_like_text(block: Any) -> bool:
    return isinstance(block, dict) and block.get("type") == "text"


class AcpProcess:
    """Owns one agent subprocess and the JSON-RPC conversation with it."""

    def __init__(
        self,
        *,
        session_id: str,
        grok_session_id: str,
        cwd: Path,
        emit: Callable[[dict[str, Any]], None],
        model: str | None = None,
        reasoning_effort: str | None = None,
    ):
        self.session_id = session_id
        self.grok_session_id = grok_session_id
        self.cwd = cwd
        self.model = model
        self.reasoning_effort = reasoning_effort
        self._emit = emit

        self.state = SessionState.STARTING
        self.last_activity = asyncio.get_event_loop().time()
        self.available_commands: list[dict[str, Any]] = []
        self.models: dict[str, Any] = {}
        self.agent_capabilities: dict[str, Any] = {}
        self.protocol_version: Any = None

        self._proc: asyncio.subprocess.Process | None = None
        self._ids = itertools.count(1)
        self._pending: dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._reader_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._stderr_ring = bytearray()
        self._request_tasks: set[asyncio.Task] = set()

    # ------------------------------------------------------------------ argv

    def _argv(self) -> list[str]:
        # Flags live on the parent `agent` command; `agent stdio` itself accepts
        # only --debug/--debug-file/--leader-socket (verified against --help).
        argv = [config.GROK_BIN, "agent"]
        if self.model:
            argv += ["-m", self.model]
        if self.reasoning_effort:
            argv += ["--reasoning-effort", self.reasoning_effort]
        argv.append("stdio")
        return argv

    def _env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["GROK_SANDBOX"] = config.SANDBOX_PROFILE_NAME
        return env

    # --------------------------------------------------------------- lifecycle

    async def start(self, *, resume: bool) -> None:
        """Spawn, initialize, and either create or reload the grok session."""
        argv = self._argv()
        log.info("session %s: spawning %s (cwd=%s)", self.session_id, " ".join(argv), self.cwd)
        self._proc = await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._env(),
            cwd=str(self.cwd),
        )
        self._reader_task = asyncio.create_task(self._read_loop())
        self._stderr_task = asyncio.create_task(self._drain_stderr())

        init = await self._call(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": {
                    # Declining `terminal` is deliberate: grok then runs commands
                    # itself and reports them as tool calls, which is what the UI
                    # already renders. Advertising it would move the execution
                    # boundary into Bridge code we would have to secure.
                    "fs": {"readTextFile": True, "writeTextFile": True},
                    "terminal": False,
                },
            },
            timeout=config.ACP_STARTUP_TIMEOUT_SECONDS,
        )
        self.protocol_version = init.get("protocolVersion")
        self.agent_capabilities = init.get("agentCapabilities") or {}
        if self.protocol_version != 1:
            # Fail closed. In ACP v2 the prompt response only means "accepted"
            # and the turn ends on a state update, so silently continuing would
            # make every turn look like it finished early.
            raise AgentUnavailable(
                f"unsupported ACP protocol version {self.protocol_version!r}; "
                "this Bridge implements v1 semantics only"
            )

        params = {"cwd": str(self.cwd), "mcpServers": []}
        if resume:
            params["sessionId"] = self.grok_session_id
            result = await self._call("session/load", params,
                                      timeout=config.ACP_STARTUP_TIMEOUT_SECONDS)
        else:
            result = await self._call("session/new", params,
                                      timeout=config.ACP_STARTUP_TIMEOUT_SECONDS)
            self.grok_session_id = result.get("sessionId") or self.grok_session_id

        self.models = result.get("models") or {}
        self.state = SessionState.IDLE
        self._touch()
        log.info("session %s: ready (grok session %s, resumed=%s)",
                 self.session_id, self.grok_session_id, resume)

    async def close(self) -> None:
        if self.state is SessionState.CLOSED:
            return
        self.state = SessionState.CLOSED
        proc = self._proc
        if proc is None:
            return
        with contextlib.suppress(Exception):
            if proc.stdin and not proc.stdin.is_closing():
                proc.stdin.close()
        try:
            await asyncio.wait_for(proc.wait(), 5)
        except (asyncio.TimeoutError, Exception):
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(asyncio.TimeoutError, Exception):
                await asyncio.wait_for(proc.wait(), 3)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        for task in (self._reader_task, self._stderr_task):
            if task:
                task.cancel()
        self._fail_pending(AgentUnavailable("agent closed"))
        log.info("session %s: agent closed", self.session_id)

    @property
    def alive(self) -> bool:
        return (
            self._proc is not None
            and self._proc.returncode is None
            and self.state is not SessionState.CLOSED
        )

    def _touch(self) -> None:
        self.last_activity = asyncio.get_event_loop().time()

    # ------------------------------------------------------------------- RPC

    async def _write(self, msg: dict[str, Any]) -> None:
        if self._proc is None or self._proc.stdin is None:
            raise AgentUnavailable("agent is not running")
        payload = (json.dumps(msg) + "\n").encode()
        async with self._write_lock:
            self._proc.stdin.write(payload)
            await self._proc.stdin.drain()

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no response)."""
        await self._write({"jsonrpc": "2.0", "method": method, "params": params})

    async def _call(self, method: str, params: dict[str, Any],
                    timeout: float | None = None) -> dict[str, Any]:
        req_id = next(self._ids)
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[req_id] = fut
        await self._write({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        try:
            return await asyncio.wait_for(
                fut, timeout if timeout is not None else config.ACP_REQUEST_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            self._pending.pop(req_id, None)
            raise AgentUnavailable(f"{method} timed out") from None

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in list(self._pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    # ---------------------------------------------------------------- reader

    async def _read_loop(self) -> None:
        assert self._proc is not None and self._proc.stdout is not None
        try:
            async for raw in self._proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("session %s: undecodable line (%d bytes)",
                                self.session_id, len(line))
                    continue
                self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("session %s: reader failed", self.session_id)
        finally:
            await self._on_eof()

    def _dispatch(self, msg: dict[str, Any]) -> None:
        """Demultiplex only. Never awaits anything that could block the stream."""
        if "method" in msg and "id" in msg:
            # Agent -> client request. Handled in its own task: a handler that
            # takes a while must not stall notifications or responses queued
            # behind it on the same pipe.
            task = asyncio.create_task(self._handle_request(msg))
            self._request_tasks.add(task)
            task.add_done_callback(self._request_tasks.discard)
            return

        if "method" in msg:
            self._touch()
            event = translate(msg)
            if event is None:
                return
            if event["type"] == "commands.available":
                self.available_commands = event["commands"]
            self._emit(event)
            return

        req_id = msg.get("id")
        fut = self._pending.pop(req_id, None) if req_id is not None else None
        if fut is None or fut.done():
            return
        if "error" in msg:
            err = msg["error"] or {}
            fut.set_exception(AcpError(err.get("code", -1), err.get("message", "error"),
                                       err.get("data")))
        else:
            fut.set_result(msg.get("result") or {})

    async def _on_eof(self) -> None:
        if self.state is SessionState.CLOSED:
            return
        rc = self._proc.returncode if self._proc else None
        log.warning("session %s: agent exited (returncode=%s)", self.session_id, rc)
        self.state = SessionState.CLOSED
        self._fail_pending(AgentUnavailable(f"agent exited (returncode={rc})"))
        self._emit({
            "type": "message.error",
            "message": "The agent process stopped unexpectedly. The session can be reopened.",
            "code": "agent_exited",
        })

    async def _drain_stderr(self) -> None:
        """Keep a bounded tail of stderr for diagnostics.

        Never forwarded to a client verbatim: agent stderr carries absolute
        paths, prompt fragments and auth errors. Clients get a sanitized
        summary; this ring is for the Mac's own logs.
        """
        assert self._proc is not None and self._proc.stderr is not None
        try:
            async for chunk in self._proc.stderr:
                self._stderr_ring.extend(chunk)
                if len(self._stderr_ring) > config.ACP_STDERR_RING_BYTES:
                    del self._stderr_ring[:-config.ACP_STDERR_RING_BYTES]
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    def stderr_tail(self) -> str:
        return self._stderr_ring.decode("utf-8", "replace")

    # -------------------------------------------------- agent->client requests

    async def _handle_request(self, msg: dict[str, Any]) -> None:
        method = msg.get("method")
        params = msg.get("params") or {}
        try:
            if method == "fs/read_text_file":
                result = self._fs_read(params)
            elif method == "fs/write_text_file":
                result = self._fs_write(params)
            else:
                # Includes terminal/* -- we did not advertise that capability.
                await self._write({
                    "jsonrpc": "2.0", "id": msg["id"],
                    "error": {"code": -32601, "message": f"method not supported: {method}"},
                })
                return
            await self._write({"jsonrpc": "2.0", "id": msg["id"], "result": result})
        except InvalidCwd as e:
            log.warning("session %s: refused %s outside cwd: %s", self.session_id, method, e)
            await self._write({
                "jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32602, "message": str(e)},
            })
        except Exception as e:  # noqa: BLE001 - must always answer the agent
            log.exception("session %s: %s failed", self.session_id, method)
            await self._write({
                "jsonrpc": "2.0", "id": msg["id"],
                "error": {"code": -32603, "message": f"{type(e).__name__}: {e}"},
            })

    def _fs_read(self, params: dict[str, Any]) -> dict[str, Any]:
        path = validate_path_in_cwd(params["path"], self.cwd)
        text = path.read_text(encoding="utf-8", errors="replace")
        line = params.get("line")
        limit = params.get("limit")
        if line is not None or limit is not None:
            lines = text.splitlines(keepends=True)
            start = max((line or 1) - 1, 0)
            end = start + limit if limit is not None else None
            text = "".join(lines[start:end])
        return {"content": text}

    def _fs_write(self, params: dict[str, Any]) -> dict[str, Any]:
        path = validate_path_in_cwd(params["path"], self.cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(params.get("content", ""), encoding="utf-8")
        return {}

    # ------------------------------------------------------------------ turns

    async def prompt(self, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """Run one turn. Returns the canonical terminal payload.

        The response to session/prompt IS the end of the turn under ACP v1, and
        it carries usage. grok additionally announces completion on two vendor
        channels; those become `turn.telemetry` so a turn ends exactly once.
        """
        if not self.alive:
            raise AgentUnavailable("agent is not running")
        if self.state is SessionState.RUNNING:
            raise AgentBusy("a turn is already running in this session")
        if self.state is not SessionState.IDLE:
            raise AgentUnavailable(f"session is {self.state.value}")

        self.state = SessionState.RUNNING
        self._touch()
        try:
            result = await self._call(
                "session/prompt",
                {"sessionId": self.grok_session_id, "prompt": blocks},
                timeout=config.ACP_PROMPT_TIMEOUT_SECONDS,
            )
        finally:
            if self.state is SessionState.RUNNING:
                self.state = SessionState.IDLE
            self._touch()

        usage = (result.get("_meta") or {}).get("usage")
        if usage:
            self._emit({"type": "message.usage", "usage": usage})
        return {"stopReason": result.get("stopReason"), "usage": usage}

    async def cancel(self) -> None:
        """Ask the agent to stop the current turn.

        Sent as a NOTIFICATION, not a request. ACP defines session/cancel that
        way, and grok enforces the distinction: sent with an id it answers
        politely and keeps generating to completion, reporting stopReason
        "end_turn". As a notification the turn really stops and reports
        "cancelled". Measured -- a request-form cancel let a 12k-word generation
        run to the end while the client believed it had cancelled.

        Unlike the SIGTERM this replaces, the process survives and the session
        stays usable, which is why db.rotate_grok_session_id is gone.
        """
        if not self.alive or self.state is not SessionState.RUNNING:
            return
        with contextlib.suppress(AgentUnavailable):
            await self._notify("session/cancel", {"sessionId": self.grok_session_id})

    async def set_model(self, model_id: str) -> None:
        if self.state is SessionState.RUNNING:
            raise AgentBusy("cannot change model while a turn is running")
        await self._call("session/set_model",
                         {"sessionId": self.grok_session_id, "modelId": model_id})
        self.model = model_id
        self._touch()
        self._emit({"type": "config.changed", "config": {"model": model_id}})

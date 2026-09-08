"""Owns the pool of ACP agent processes, one per Bridge session.

Replaces AgentManager, which spawned a fresh `grok -p` for every turn. The
change is what makes real cancellation, slash commands, live model switching and
history all possible -- none of which headless mode exposes.

Three behaviours here are deliberate and were argued for before being built:

  * A busy session refuses a second prompt with AgentBusy rather than queueing
    it behind a lock. Silent queueing reads to the user as a lost message.
  * Only IDLE agents are ever reaped or evicted. A tool that has run for ten
    minutes without printing anything is not idle, and when every slot is busy
    the *new* request is refused rather than a running turn being killed.
  * Events carry a session-level monotonic `seq`, not a per-job one, so prompts,
    commands and config changes share one order. Phase 2's resume-after-
    reconnect replays against exactly this sequence.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from . import config, sandbox
from .acp import AcpProcess, AcpError, AgentBusy, AgentUnavailable, SessionState

log = logging.getLogger("grok_bridge.sessions")


class AgentCapacity(RuntimeError):
    """Every agent slot is occupied by a running turn."""


class AcpSessionManager:
    def __init__(self) -> None:
        self._procs: dict[str, AcpProcess] = {}
        self._queues: dict[str, set[asyncio.Queue]] = {}
        self._seq: dict[str, int] = {}
        self._starting: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._sandbox_verified = False

    # ------------------------------------------------------------- lifecycle

    async def start(self) -> None:
        sandbox.ensure_profile()
        if self._reaper is None:
            self._reaper = asyncio.create_task(self._reap_loop())

    async def shutdown(self) -> None:
        if self._reaper:
            self._reaper.cancel()
            self._reaper = None
        for proc in list(self._procs.values()):
            await proc.cancel()
            await proc.close()
        self._procs.clear()

    @property
    def live_count(self) -> int:
        return sum(1 for p in self._procs.values() if p.alive)

    def state_of(self, session_id: str) -> str:
        proc = self._procs.get(session_id)
        return proc.state.value if proc else "stopped"

    # ------------------------------------------------------------ fan-out

    def attach(self, session_id: str) -> asyncio.Queue:
        """Subscribe to a session's event stream.

        The queue outlives any single client connection, so a phone that drops
        mid-turn can reattach and keep receiving the turn that is still running.
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._queues.setdefault(session_id, set()).add(queue)
        return queue

    def detach(self, session_id: str, queue: asyncio.Queue) -> None:
        subscribers = self._queues.get(session_id)
        if not subscribers:
            return
        subscribers.discard(queue)
        if not subscribers:
            self._queues.pop(session_id, None)

    def _emit(self, session_id: str, event: dict[str, Any]) -> None:
        seq = self._seq.get(session_id, 0) + 1
        self._seq[session_id] = seq
        event = {**event, "seq": seq}
        for queue in list(self._queues.get(session_id, ())):
            queue.put_nowait(event)

    def emit(self, session_id: str, event: dict[str, Any]) -> None:
        """Publish a Bridge-originated event on the same ordered stream."""
        self._emit(session_id, event)

    # ------------------------------------------------------------- processes

    async def get(
        self,
        *,
        session_id: str,
        grok_session_id: str,
        cwd: Path,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AcpProcess:
        """Return a live agent for this session, starting one if needed."""
        proc = self._procs.get(session_id)
        if proc is not None and proc.alive:
            return proc

        lock = self._starting.setdefault(session_id, asyncio.Lock())
        async with lock:
            proc = self._procs.get(session_id)
            if proc is not None and proc.alive:
                return proc
            return await self._spawn(
                session_id=session_id,
                grok_session_id=grok_session_id,
                cwd=cwd,
                model=model,
                reasoning_effort=reasoning_effort,
                # A session that has run before is resumed rather than recreated,
                # so a reaped or crashed agent comes back with its context. The
                # Phase 0 spike confirmed session/load restores it losslessly.
                resume=session_id in self._seq,
            )

    async def _spawn(
        self,
        *,
        session_id: str,
        grok_session_id: str,
        cwd: Path,
        model: str | None,
        reasoning_effort: str | None,
        resume: bool,
    ) -> AcpProcess:
        self._make_room()
        started_at = time.time()
        proc = AcpProcess(
            session_id=session_id,
            grok_session_id=grok_session_id,
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
            emit=lambda event: self._emit(session_id, event),
        )
        try:
            await proc.start(resume=resume)
        except Exception:
            await proc.close()
            raise

        if not self._sandbox_verified:
            # Fail closed. grok logs a warning and runs unconfined if the sandbox
            # cannot be applied; this is the only boundary the remote path has,
            # so an unconfined agent is not something to serve traffic with.
            try:
                event = sandbox.verify_enforced(since=started_at)
                self._sandbox_verified = True
                log.info("sandbox enforced: profile=%s platform=%s",
                         event.get("profile"), event.get("platform"))
            except sandbox.SandboxNotEnforced:
                await proc.close()
                raise

        self._procs[session_id] = proc
        self._seq.setdefault(session_id, 0)
        return proc

    def _make_room(self) -> None:
        """Evict an idle agent if we are at capacity; never a running one."""
        for session_id, proc in list(self._procs.items()):
            if not proc.alive:
                self._procs.pop(session_id, None)

        if self.live_count < config.ACP_MAX_LIVE_AGENTS:
            return

        idle = [(p.last_activity, sid) for sid, p in self._procs.items()
                if p.alive and p.state is SessionState.IDLE]
        if not idle:
            raise AgentCapacity(
                f"all {config.ACP_MAX_LIVE_AGENTS} agent slots are running a turn"
            )
        _, victim = min(idle)
        log.info("evicting idle agent for session %s to free a slot", victim)
        asyncio.create_task(self._retire(victim))

    async def _retire(self, session_id: str) -> None:
        proc = self._procs.pop(session_id, None)
        if proc:
            await proc.close()

    async def _reap_loop(self) -> None:
        while True:
            try:
                await asyncio.sleep(config.ACP_REAPER_INTERVAL_SECONDS)
                now = asyncio.get_event_loop().time()
                for session_id, proc in list(self._procs.items()):
                    if not proc.alive:
                        self._procs.pop(session_id, None)
                        continue
                    # State, not just elapsed time: a long-running tool that has
                    # not printed anything recently must not look idle.
                    if proc.state is not SessionState.IDLE:
                        continue
                    if now - proc.last_activity > config.ACP_IDLE_TTL_SECONDS:
                        log.info("reaping idle agent for session %s", session_id)
                        await self._retire(session_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("reaper iteration failed")

    # ----------------------------------------------------------------- turns

    async def prompt(
        self,
        *,
        session_id: str,
        grok_session_id: str,
        cwd: Path,
        blocks: list[dict[str, Any]],
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> dict[str, Any]:
        proc = await self.get(
            session_id=session_id,
            grok_session_id=grok_session_id,
            cwd=cwd,
            model=model,
            reasoning_effort=reasoning_effort,
        )
        if model and proc.model != model:
            await proc.set_model(model)
        if reasoning_effort and proc.reasoning_effort != reasoning_effort:
            # No ACP channel exists for effort (measured): it is a spawn-time
            # flag, so changing it means restarting the agent and reloading the
            # session. Context survives -- that is what session/load is for.
            proc = await self.restart(
                session_id=session_id, grok_session_id=proc.grok_session_id,
                cwd=cwd, model=model or proc.model, reasoning_effort=reasoning_effort,
            )
        return await proc.prompt(blocks)

    async def cancel(self, session_id: str) -> bool:
        proc = self._procs.get(session_id)
        if proc is None or not proc.alive:
            return False
        await proc.cancel()
        return True

    async def restart(
        self,
        *,
        session_id: str,
        grok_session_id: str,
        cwd: Path,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> AcpProcess:
        """Stop and reload a session's agent, preserving conversation context."""
        old = self._procs.pop(session_id, None)
        if old:
            await old.cancel()
            await old.close()
        self._emit(session_id, {"type": "agent.restarting"})
        proc = await self._spawn(
            session_id=session_id, grok_session_id=grok_session_id, cwd=cwd,
            model=model, reasoning_effort=reasoning_effort, resume=True,
        )
        return proc

    async def commands(
        self, *, session_id: str, grok_session_id: str, cwd: Path
    ) -> list[dict[str, Any]]:
        proc = await self.get(session_id=session_id, grok_session_id=grok_session_id, cwd=cwd)
        return proc.available_commands


__all__ = [
    "AcpSessionManager",
    "AgentCapacity",
    "AgentBusy",
    "AgentUnavailable",
    "AcpError",
]

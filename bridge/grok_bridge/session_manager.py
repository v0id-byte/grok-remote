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
    def __init__(self, journal=None) -> None:
        self._procs: dict[str, AcpProcess] = {}
        self._queues: dict[str, set[asyncio.Queue]] = {}
        self._starting: dict[str, asyncio.Lock] = {}
        self._reaper: asyncio.Task | None = None
        self._sandbox_verified = False
        self._started_sessions: set[str] = set()
        # Store-shaped: append_event / latest_seq / events_after. Injected so the
        # manager can be exercised without a database.
        self._journal = journal
        self._fallback_seq: dict[str, int] = {}
        self._current_job: dict[str, str] = {}

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
        """Number the event, persist it, then fan it out.

        Persisting before delivery is what makes resume work: a client that was
        not connected -- or dropped mid-turn -- asks for everything after the
        last seq it saw, and the journal has it. Numbering comes from the
        journal too, so sequence continues across a Bridge restart instead of
        restarting at 1 and colliding with what the client already has.
        """
        job_id = self._current_job.get(session_id)
        if self._journal is not None:
            try:
                seq = self._journal.append_event(session_id, event, job_id)
            except Exception:
                log.exception("failed to journal event for session %s", session_id)
                seq = self._next_fallback_seq(session_id)
        else:
            seq = self._next_fallback_seq(session_id)

        event = {**event, "seq": seq}
        if job_id and "job_id" not in event:
            event["job_id"] = job_id
        for queue in list(self._queues.get(session_id, ())):
            queue.put_nowait(event)

    def _next_fallback_seq(self, session_id: str) -> int:
        seq = self._fallback_seq.get(session_id, 0) + 1
        self._fallback_seq[session_id] = seq
        return seq

    def set_job(self, session_id: str, job_id: str | None) -> None:
        """Tag subsequent events with the turn that produced them."""
        if job_id is None:
            self._current_job.pop(session_id, None)
        else:
            self._current_job[session_id] = job_id

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
                # A session that has run before is resumed rather than
                # recreated, so a reaped or crashed agent comes back with its
                # context. The Phase 0 spike confirmed session/load restores it
                # losslessly. "Has run before" survives a Bridge restart by
                # asking the journal, not an in-memory set.
                resume=self._has_history(session_id),
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

        if proc.grok_session_id != grok_session_id and self._journal is not None:
            # session/new mints grok's OWN session id, which is not the one the
            # Bridge generated. Without writing it back, history lookups search
            # a directory that does not exist and a respawn tries to
            # session/load an id grok never issued.
            try:
                self._journal.update_session(
                    session_id, grok_session_id=proc.grok_session_id)
                log.info("session %s: bound to grok session %s",
                         session_id, proc.grok_session_id)
            except Exception:
                log.exception("failed to persist grok session id for %s", session_id)

        self._procs[session_id] = proc
        self._started_sessions.add(session_id)
        return proc

    def _has_history(self, session_id: str) -> bool:
        if session_id in self._started_sessions:
            return True
        if self._journal is not None:
            try:
                return self._journal.latest_seq(session_id) > 0
            except Exception:
                log.exception("journal lookup failed for session %s", session_id)
        return False

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

    async def retire(self, session_id: str) -> None:
        """Stop this session's agent, if one is running."""
        await self._retire(session_id)

    def forget(self, session_id: str) -> None:
        """Drop the belief that this session has history.

        Used by /clear, which starts a fresh grok-side conversation under the
        same Bridge session id: without this the next spawn would try to
        session/load an id that has no transcript.
        """
        self._started_sessions.discard(session_id)
        self._fallback_seq.pop(session_id, None)

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

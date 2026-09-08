"""Owns grok subprocess lifecycle: one serial queue per session, streaming
NDJSON -> Bridge events, and cancellation (plan v1.3 §1 "AgentManager").

v1 is ephemeral: every call spawns a fresh `grok -p` process. Nothing here
assumes "process == one call forever" -- a persistent/leader-backed
implementation could replace `_run_grok` without touching the queueing or
cancel contract.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import signal
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from . import config
from .protocol import translate


class CancelledByClient(Exception):
    pass


class AgentManager:
    def __init__(self):
        self._session_locks: dict[str, asyncio.Lock] = {}
        self._running_procs: dict[str, asyncio.subprocess.Process] = {}

    def _lock_for(self, session_id: str) -> asyncio.Lock:
        return self._session_locks.setdefault(session_id, asyncio.Lock())

    async def cancel(self, session_id: str) -> bool:
        proc = self._running_procs.get(session_id)
        if proc is None or proc.returncode is not None:
            return False
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            return False
        return True

    async def run_turn(
        self,
        *,
        job_id: str,
        session_id: str,
        grok_session_id: str,
        cwd: Path,
        text: str,
        model: str | None,
        reasoning_effort: str | None,
        attachments: list[str] | None = None,
    ) -> AsyncIterator[dict[str, Any]]:
        """Yield Bridge protocol events for one turn. Serializes on session_id
        (the Bridge-facing id): a second concurrent call for the same session
        waits for the first to finish (grok treats concurrent access to one
        session id as best-effort / no file locking). `grok_session_id` is
        what's actually passed to `grok -s` -- kept separate because it can
        be rotated after a cancellation (see db.rotate_grok_session_id)
        without changing the Bridge session's own identity."""
        lock = self._lock_for(session_id)
        async with lock:
            # Track cancellation under the Bridge session_id (that's what
            # app.py's cancel handler knows about), but run the subprocess
            # against the current grok_session_id.
            async for event in self._run_grok(
                job_id=job_id,
                proc_key=session_id,
                grok_session_id=grok_session_id,
                cwd=cwd,
                text=text,
                model=model,
                reasoning_effort=reasoning_effort,
                attachments=attachments or [],
            ):
                yield event

    async def _run_grok(
        self,
        *,
        job_id: str,
        proc_key: str,
        grok_session_id: str,
        cwd: Path,
        text: str,
        model: str | None,
        reasoning_effort: str | None,
        attachments: list[str],
    ) -> AsyncIterator[dict[str, Any]]:
        prompt = text
        for path in attachments:
            prompt += f" @{path}"

        cmd = [
            config.GROK_BIN,
            "-p", prompt,
            "-s", grok_session_id,
            "--cwd", str(cwd),
            "--output-format", "streaming-json",
        ]
        if model:
            cmd += ["-m", model]
        if reasoning_effort:
            cmd += ["--reasoning-effort", reasoning_effort]
        for rule in config.ALLOW_RULES:
            cmd += ["--allow", rule]
        for rule in config.DENY_RULES:
            cmd += ["--deny", rule]

        seq = itertools.count()

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,  # own process group -> cancel() can killpg reliably
        )
        self._running_procs[proc_key] = proc

        try:
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event = translate(raw)
                if event is None:
                    continue
                event["job_id"] = job_id
                event["seq"] = next(seq)
                yield event

            returncode = await proc.wait()
            if returncode == -signal.SIGTERM or returncode == -signal.SIGKILL:
                yield {"type": "cancelled", "job_id": job_id, "seq": next(seq)}
            elif returncode != 0:
                stderr = (await proc.stderr.read()).decode(errors="replace") if proc.stderr else ""
                yield {
                    "type": "message.error",
                    "job_id": job_id,
                    "seq": next(seq),
                    "message": stderr.strip() or f"grok exited with code {returncode}",
                }
        finally:
            self._running_procs.pop(proc_key, None)
            if proc.returncode is None:
                try:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                except ProcessLookupError:
                    pass

"""FastAPI app: the Bridge's HTTP/WebSocket surface (plan v1.3 §1 "接口设计").

Auth: every /v1/* route except /v1/pair requires `Authorization: Bearer
<device_id>.<secret>` matching a row in the devices table.

This is now the ONLY authentication layer. Cloudflare Access used to sit in
front of it, but an Access Service Token is a machine-to-machine client secret
and shipping one inside the app bundle leaks it to anyone who runs `strings` on
the IPA, so it was removed (plan v2 ss0.1). Because the tunnel now exposes this
app directly -- and behind it an agent that can run shell commands -- the
pairing flow carries the hardening that Access used to provide for free:
expiring tokens, rate limiting, and hiding the endpoint when there is nothing
to redeem (plan v2 ss0.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import subprocess
import time
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

from fastapi import (
    FastAPI,
    HTTPException,
    Header,
    Request,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse

from . import commands as cmd_registry
from . import config, grok_disk, workspace
from .acp import AgentBusy, AgentUnavailable, SessionState
from .db import Store
from .sandbox import SandboxNotEnforced
from .security import InvalidCwd, validate_cwd
from .session_manager import AcpSessionManager, AgentCapacity

store = Store()
agents = AcpSessionManager(journal=store)
START_TIME = time.time()


@contextlib.asynccontextmanager
async def _lifespan(_app: FastAPI):
    await agents.start()
    try:
        yield
    finally:
        await agents.shutdown()


app = FastAPI(title="grok-remote-bridge", lifespan=_lifespan)
log = logging.getLogger("grok_bridge.auth")

# Drop unredeemed-but-expired pairing tokens left over from before the TTL
# existed, so a months-old QR screenshot stops being a working credential.
store.purge_expired_pairing_tokens()

# Failed-auth bookkeeping, per client IP. In-memory on purpose: this is a
# single-process service, and a restart clearing the counters is acceptable --
# the point is to make online guessing pointless, not to be an audit log.
_auth_failures: dict[str, deque[float]] = defaultdict(deque)
_pair_attempts: dict[str, deque[float]] = defaultdict(deque)
AUTH_FAIL_COUNT = 0


def _client_ip(request: Request | WebSocket | None) -> str:
    if request is None or request.client is None:
        return "unknown"
    return request.client.host


def _throttle(bucket: dict[str, deque[float]], key: str, window: float, limit: int) -> bool:
    """Sliding window. Returns True when the caller is over the limit."""
    now = time.time()
    hits = bucket[key]
    while hits and now - hits[0] > window:
        hits.popleft()
    return len(hits) >= limit


def _record(bucket: dict[str, deque[float]], key: str) -> None:
    bucket[key].append(time.time())


def _note_auth_failure(ip: str, reason: str) -> None:
    """Log the failure WITHOUT the token. The token is the credential; putting
    it in a log file just moves the secret somewhere with weaker permissions."""
    global AUTH_FAIL_COUNT
    AUTH_FAIL_COUNT += 1
    _record(_auth_failures, ip)
    log.warning("auth failure from %s: %s", ip, reason)


def _check_bearer(authorization: str | None, request: Request | None = None) -> None:
    ip = _client_ip(request)
    if _throttle(_auth_failures, ip, config.AUTH_FAIL_WINDOW_SECONDS, config.AUTH_FAIL_MAX):
        log.warning("auth throttled for %s", ip)
        raise HTTPException(429, "too many failed attempts")
    if not authorization or not authorization.startswith("Bearer "):
        _note_auth_failure(ip, "missing bearer token")
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    device_id, _, secret = token.partition(".")
    if not device_id or not secret or not store.check_device_token(device_id, secret):
        _note_auth_failure(ip, "invalid device token")
        raise HTTPException(401, "invalid device token")


def _device_id_of(authorization: str | None) -> str:
    return (authorization or "").removeprefix("Bearer ").strip().partition(".")[0]


def _runtime_access(cwd: str, source: str = "bridge") -> tuple[bool, str | None]:
    """Return whether a session may start an agent in its current cwd."""
    try:
        validate_cwd(cwd)
    except InvalidCwd as error:
        if not Path(cwd).is_dir():
            return False, "The working directory is missing. History is read-only."
        return False, f"The working directory is read-only: {error}"
    return True, None


def _sync_discovered_sessions() -> None:
    for item in grok_disk.discover_sessions():
        summary = item["summary"]
        store.upsert_discovered_session(
            cwd=item["cwd"],
            grok_session_id=item["grokSessionId"],
            title=summary.get("title"),
            model=summary.get("model"),
            created_at=item.get("createdAt"),
            last_active_at=item.get("lastActiveAt"),
        )


def _session_source(session) -> str:
    return session["source"] if "source" in session.keys() else "bridge"


def _session_model(session, proc=None) -> str | None:
    if proc is not None and proc.alive:
        live_model = proc.model or proc.models.get("currentModelId")
        if live_model:
            return live_model
    model = session["model"] if "model" in session.keys() else None
    if model:
        return model
    return grok_disk.read_summary(
        session["cwd"], session["grok_session_id"]).get("model")


def _known_model_ids(proc=None) -> set[str]:
    available = (proc.models if proc is not None else {}).get("availableModels") or []
    ids = {item.get("modelId") for item in available if item.get("modelId")}
    if ids:
        return ids
    return set(grok_disk.read_models()["models"])


def _validate_model_choice(model_id: str, proc=None) -> None:
    known = _known_model_ids(proc)
    if known and model_id not in known:
        raise HTTPException(400, f"unknown or unavailable model: {model_id}")


def _effort_info(model_id: str | None, proc=None) -> tuple[bool | None, list[str]]:
    """Return (supported, choices) for a model when metadata is available.

    A live ACP session is authoritative for the models it advertises. The
    on-disk catalogue fills the gap before an agent has been started. `None`
    means neither source knows enough to reject a value, which keeps a newly
    configured local model usable instead of guessing.
    """
    if not model_id:
        return None, []

    available = (proc.models if proc is not None else {}).get("availableModels") or []
    for entry in available:
        if entry.get("modelId") != model_id:
            continue
        meta = entry.get("_meta") or {}
        raw_efforts = (meta.get("reasoningEfforts")
                       or meta.get("reasoning_efforts")
                       or entry.get("reasoningEfforts")
                       or entry.get("reasoning_efforts")
                       or [])
        efforts = []
        for item in raw_efforts:
            value = (item.get("id") or item.get("value")) \
                if isinstance(item, dict) else str(item)
            if value:
                efforts.append(str(value))
        supports = meta.get("supportsReasoningEffort")
        if supports is None:
            supports = entry.get("supportsReasoningEffort")
        if supports is not None or efforts:
            return bool(supports) or bool(efforts), efforts
        break

    info = grok_disk.read_models()["models"].get(model_id)
    if info is None:
        return None, []
    efforts = info.get("reasoningEfforts") or []
    supports = info.get("supportsReasoningEffort")
    if supports is None:
        supports = bool(efforts)
    return bool(supports) or bool(efforts), efforts


def _validate_effort_choice(effort: str, model_id: str | None, proc=None) -> None:
    if not model_id:
        return
    supported, efforts = _effort_info(model_id, proc)
    if supported is False:
        raise HTTPException(400, f"reasoning effort is not supported by {model_id}")
    if efforts and effort not in efforts:
        raise HTTPException(400, f"reasoning effort is not supported by {model_id}: {effort}")


def _validate_prompt_config(session, frame: dict[str, Any], proc=None) -> None:
    model = frame.get("model")
    if model:
        _validate_model_choice(model, proc if proc and proc.alive else None)
    selected_model = model or _session_model(session, proc)
    effort = frame.get("reasoningEffort")
    if effort:
        _validate_effort_choice(
            effort, selected_model, proc if proc and proc.alive else None)
    elif model and session["reasoning_effort"]:
        _validate_effort_choice(
            session["reasoning_effort"], selected_model,
            proc if proc and proc.alive else None)


def _persist_prompt_config(session_id: str, session, frame: dict[str, Any]) -> None:
    """Remember the settings actually used for a successful prompt."""
    fields: dict[str, Any] = {}
    if frame.get("model"):
        fields["model"] = frame["model"]
    if "reasoningEffort" in frame:
        fields["reasoning_effort"] = frame.get("reasoningEffort") or None
    if not fields:
        return

    store.update_session(session_id, **fields)
    updated = store.get_session(session_id)
    if updated is None:
        return
    proc = agents._procs.get(session_id)
    agents.emit(session_id, {
        "type": "config.changed",
        "config": {
            "model": _session_model(updated, proc),
            "reasoningEffort": updated["reasoning_effort"],
            "reasoningEffortConfigured": updated["reasoning_effort"] is not None,
        },
    })


@app.get("/health")
def health() -> dict[str, Any]:
    grok_reachable = Path(config.GROK_BIN).exists()
    return {
        "status": "ok",
        "grok_reachable": grok_reachable,
        "active_agents": agents.live_count,
        "uptime_s": round(time.time() - START_TIME, 1),
        "auth_failures": AUTH_FAIL_COUNT,
        "pairing_open": store.has_redeemable_pairing_token(),
    }


@app.post("/v1/pair")
def pair(body: dict[str, str], request: Request) -> dict[str, str]:
    ip = _client_ip(request)
    if _throttle(_pair_attempts, ip, config.PAIR_ATTEMPT_WINDOW_SECONDS,
                 config.PAIR_ATTEMPT_MAX):
        log.warning("pairing throttled for %s", ip)
        raise HTTPException(429, "too many pairing attempts")

    store.purge_expired_pairing_tokens()
    # No live token means nobody is pairing right now. Return 404 rather than
    # 400 so a scanner cannot tell this endpoint exists, let alone probe it.
    if not store.has_redeemable_pairing_token():
        _record(_pair_attempts, ip)
        raise HTTPException(404, "not found")

    token = body.get("pairing_token", "")
    result = store.redeem_pairing_token(token, device_name=body.get("device_name", "iPhone"))
    if result is None:
        _record(_pair_attempts, ip)
        log.warning("pairing failure from %s: invalid, expired or used token", ip)
        raise HTTPException(400, "invalid, expired or already-used pairing token")
    device_id, secret = result
    log.info("paired new device %s from %s", device_id, ip)
    return {"device_id": device_id, "secret": secret}


@app.delete("/v1/devices/{device_id}")
def revoke_device(
    device_id: str,
    request: Request,
    authorization: str | None = Header(None),
) -> dict[str, bool]:
    _check_bearer(authorization, request)
    # Self-only. Any valid device used to be able to revoke any other, which
    # turns one compromised device into a way to lock the owner out.
    if device_id != _device_id_of(authorization):
        raise HTTPException(403, "a device may only revoke itself")
    return {"revoked": store.revoke_device(device_id)}


@app.get("/v1/models")
def list_models(request: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
    """Model catalogue, read from grok's own files.

    This used to shell out to `grok models` and screen-scrape stdout with a
    regex. Reading models_cache.json and config.toml instead gives names,
    context windows and per-model reasoning efforts, none of which the CLI's
    text output carries -- and the local qwen/muse models exist only in
    config.toml, so both sources are required.
    """
    _check_bearer(authorization, request)
    catalogue = grok_disk.read_models()
    models = list(catalogue["models"].values())
    for model in models:
        model["vision"] = model["id"] in _VISION_MODELS
    return {"models": models, "default": catalogue["default"]}


@app.get("/v1/recent-dirs")
def recent_dirs(request: Request, authorization: str | None = Header(None)) -> dict[str, list[str]]:
    _check_bearer(authorization, request)
    return {"dirs": store.recent_dirs()}


@app.get("/v1/sessions")
def list_sessions(request: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
    """Sessions, enriched with grok's own metadata.

    The Bridge stores little more than a cwd; grok's summary.json has the
    generated title, the model actually in use, the git branch and a summary of
    the last turn. Folded in lazily and tolerant of it being absent -- a session
    that has not run a turn yet has no summary file.
    """
    _check_bearer(authorization, request)
    _sync_discovered_sessions()
    out = []
    for row in store.list_sessions():
        can_run, read_only_reason = _runtime_access(row["cwd"], _session_source(row))
        session = {
            "id": row["id"],
            "cwd": row["cwd"],
            "title": row["title"],
            "created_at": row["created_at"],
            "last_active_at": row["last_active_at"],
            "last_message_preview": store.last_message_preview(row["id"]),
            "model": row["model"] if "model" in row.keys() else None,
            "reasoningEffort": row["reasoning_effort"] if "reasoning_effort" in row.keys() else None,
            "state": agents.state_of(row["id"]),
            # Sessions outlive their directories -- a deleted checkout should be
            # visibly broken in the list rather than failing on first use.
            "cwdExists": Path(row["cwd"]).is_dir(),
            "canRun": can_run,
            "readOnlyReason": read_only_reason,
        }
        summary = grok_disk.read_summary(row["cwd"], row["grok_session_id"])
        if summary:
            session["title"] = session["title"] or summary.get("title")
            session["model"] = session["model"] or summary.get("model")
            if not row["model"] and summary.get("model"):
                store.update_session(row["id"], model=summary["model"])
            session["messageCount"] = summary.get("messageCount")
            session["headBranch"] = summary.get("headBranch")
            session["lastTurnSummary"] = summary.get("lastTurnSummary")
        out.append(session)
    return {"sessions": out}


@app.post("/v1/sessions")
def create_session(body: dict[str, str], request: Request,
                   authorization: str | None = Header(None)) -> dict[str, str]:
    _check_bearer(authorization, request)
    cwd = body.get("cwd")
    if not cwd:
        raise HTTPException(400, "cwd is required")
    try:
        resolved = validate_cwd(cwd)
    except InvalidCwd as e:
        raise HTTPException(403, str(e)) from e
    session_id = store.create_session(cwd=str(resolved), title=body.get("title") or resolved.name)
    return {"id": session_id, "cwd": str(resolved)}


@app.post("/v1/upload")
async def upload(file: UploadFile, request: Request,
                 authorization: str | None = Header(None)) -> dict[str, str]:
    _check_bearer(authorization, request)
    ext = Path(file.filename or "").suffix.lower()
    if ext not in config.ALLOWED_UPLOAD_EXTENSIONS:
        raise HTTPException(415, f"unsupported file type: {ext or '(none)'}")

    limit = config.MAX_IMAGE_BYTES if ext in {".png", ".jpg", ".jpeg"} else config.MAX_FILE_BYTES
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise HTTPException(413, "file too large")

    import uuid
    dest = config.UPLOADS_DIR / f"{uuid.uuid4()}{ext}"
    dest.write_bytes(data)
    return {"path": str(dest)}


@app.websocket("/v1/chat")
async def chat(ws: WebSocket) -> None:
    """One turn per connection (the v1 shape the current iOS app speaks).

    Underneath it is now a resident ACP agent rather than a per-turn `grok -p`,
    so cancelling no longer destroys conversation continuity. The socket-per-turn
    protocol itself is what plan v2 §2.2 replaces with /v2/chat; the app is
    migrated there before this endpoint is retired.
    """
    await ws.accept()
    try:
        first = await ws.receive_json()
    except Exception:
        await ws.close(code=4400)
        return

    token = first.get("token", "")
    device_id, _, secret = token.partition(".")
    if not device_id or not secret or not store.check_device_token(device_id, secret):
        # Same throttling as the REST path -- the WebSocket must not be a way to
        # brute-force tokens without being counted. (v2 moves this check to the
        # upgrade's Authorization header so an unauthenticated socket is never
        # accepted in the first place; plan v2 §2.2.)
        _note_auth_failure(_client_ip(ws), "invalid device token (ws)")
        await ws.close(code=4401)
        return

    session_id = first.get("sessionId")
    session = store.get_session(session_id) if session_id else None
    if session is None:
        await ws.close(code=4404)
        return
    can_run, _ = _runtime_access(session["cwd"], _session_source(session))
    if not can_run:
        await ws.close(code=4403)
        return
    try:
        _validate_prompt_config(session, first, agents._procs.get(session_id))
    except HTTPException as e:
        await ws.send_json({"type": "message.error", "code": "invalid_config",
                            "message": str(e.detail)})
        await ws.close(code=4400)
        return

    job_id = store.create_job(session_id)
    store.add_message_preview(session_id, "user", first.get("text", ""))
    store.touch_session(session_id)

    blocks = _prompt_blocks(first.get("text", ""), first.get("attachments"))
    queue = agents.attach(session_id)

    turn = asyncio.create_task(agents.prompt(
        session_id=session_id,
        grok_session_id=session["grok_session_id"],
        cwd=Path(session["cwd"]),
        blocks=blocks,
        model=first.get("model"),
        reasoning_effort=first.get("reasoningEffort"),
        resume=True if _session_source(session) == "discovered" else None,
    ))

    async def _watch_for_cancel() -> None:
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "cancel":
                    await agents.cancel(session_id)
                    return
        except (WebSocketDisconnect, RuntimeError, ValueError):
            # A dropped socket is a cancellation in this one-turn protocol.
            await agents.cancel(session_id)

    watcher = asyncio.create_task(_watch_for_cancel())
    final_status = "done"
    last_text = ""

    async def _forward(event: dict[str, Any]) -> None:
        nonlocal last_text
        if event.get("type") == "message.delta":
            last_text += event.get("data") or ""
        await ws.send_json(event)

    try:
        while True:
            getter = asyncio.create_task(queue.get())
            done, _ = await asyncio.wait({getter, turn},
                                         return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                await _forward(getter.result())
                continue
            getter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await getter
            break

        # Drain whatever landed between the turn finishing and this point.
        while not queue.empty():
            await _forward(queue.get_nowait())

        result = await turn
        _persist_prompt_config(session_id, session, first)
        stop_reason = result.get("stopReason")
        if stop_reason == "cancelled":
            final_status = "cancelled"
        # The response to session/prompt is the single canonical end of a turn
        # (ACP v1). grok also announces completion on two vendor channels; those
        # arrive as turn.telemetry so a turn never ends more than once.
        await ws.send_json({"type": "message.done", "stopReason": stop_reason,
                            "usage": result.get("usage"), "job_id": job_id})
        if stop_reason == "cancelled":
            # Kept for the current app, which treats `cancelled` as terminal.
            await ws.send_json({"type": "cancelled", "job_id": job_id})

    except (AgentBusy, AgentCapacity) as e:
        final_status = "error"
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "message.error", "message": str(e),
                                "code": "busy", "job_id": job_id})
    except SandboxNotEnforced as e:
        final_status = "error"
        log.error("refusing to serve: %s", e)
        with contextlib.suppress(Exception):
            await ws.send_json({
                "type": "message.error", "code": "sandbox_not_enforced",
                "message": "The agent sandbox is not active on the Mac, so the "
                           "Bridge refused to start an agent.",
                "job_id": job_id})
    except AgentUnavailable as e:
        final_status = "error"
        log.warning("session %s: agent unavailable: %s", session_id, e)
        with contextlib.suppress(Exception):
            await ws.send_json({
                "type": "message.error", "code": "agent_unavailable",
                "message": "The agent stopped unexpectedly. Try sending again.",
                "job_id": job_id})
    except Exception:
        final_status = "error"
        log.exception("session %s: turn failed", session_id)
        with contextlib.suppress(Exception):
            await ws.send_json({
                "type": "message.error", "code": "internal",
                "message": "The Bridge hit an internal error handling this turn.",
                "job_id": job_id})
    finally:
        watcher.cancel()
        if not turn.done():
            turn.cancel()
        agents.detach(session_id, queue)
        store.finish_job(job_id, final_status)
        if last_text:
            store.add_message_preview(session_id, "assistant", last_text)
        with contextlib.suppress(Exception):
            await ws.close()


_VISION_MODELS = {"grok-4.6", "grok-4.5"}


def _prompt_blocks(text: str, attachments: list[str] | None) -> list[dict[str, Any]]:
    """Build ACP prompt content.

    Images go as real image blocks -- they work despite the agent advertising
    promptCapabilities.image = false, which was measured, not assumed. Anything
    else is referenced with grok's own `@path` syntax, as the previous
    implementation did.
    """
    import base64
    import mimetypes

    blocks: list[dict[str, Any]] = []
    extra_refs: list[str] = []
    for raw in attachments or []:
        path = Path(raw)
        mime, _ = mimetypes.guess_type(path.name)
        if mime and mime.startswith("image/") and path.is_file():
            blocks.append({
                "type": "image",
                "mimeType": mime,
                "data": base64.b64encode(path.read_bytes()).decode(),
            })
        else:
            extra_refs.append(f"@{raw}")

    body = text + ("".join(f" {ref}" for ref in extra_refs) if extra_refs else "")
    blocks.insert(0, {"type": "text", "text": body})
    return blocks


# ---------------------------------------------------------------- /v2/chat


class _V2Error(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _ws_bearer(ws: WebSocket) -> str | None:
    """Authenticate from the upgrade request's own headers.

    v1 accepted the socket first and read the token from the first frame, which
    means an unauthenticated peer got a live connection before proving anything.
    Here a bad token never gets past the handshake.
    """
    header = ws.headers.get("authorization") or ""
    if not header.startswith("Bearer "):
        return None
    token = header.removeprefix("Bearer ").strip()
    device_id, _, secret = token.partition(".")
    if not device_id or not secret or not store.check_device_token(device_id, secret):
        return None
    return device_id


@app.websocket("/v2/chat")
async def chat_v2(ws: WebSocket) -> None:
    """Session-scoped, resumable connection (plan v2 §2.2).

    Differences from /v1/chat, which stays until the app has migrated:

      * one socket per session, not per turn, so several turns and any
        out-of-band events share one ordered stream;
      * authentication happens at the upgrade, not in the first frame;
      * the client says how far it got (`afterSeq`) and the Bridge replays what
        it missed from the journal, so a phone that drops mid-turn -- which iOS
        does routinely when backgrounded -- loses nothing.
    """
    ip = _client_ip(ws)
    if _throttle(_auth_failures, ip, config.AUTH_FAIL_WINDOW_SECONDS, config.AUTH_FAIL_MAX):
        await ws.close(code=4429)
        return
    if _ws_bearer(ws) is None:
        _note_auth_failure(ip, "invalid device token (ws v2)")
        await ws.close(code=4401)
        return

    await ws.accept()
    try:
        hello = await asyncio.wait_for(ws.receive_json(), 30)
    except Exception:
        await ws.close(code=4400)
        return

    if hello.get("type") != "hello":
        await ws.close(code=4400)
        return

    session_id = hello.get("sessionId")
    session = store.get_session(session_id) if session_id else None
    if session is None:
        await ws.close(code=4404)
        return
    can_run, _ = _runtime_access(session["cwd"], _session_source(session))
    if not can_run:
        await ws.close(code=4403)
        return

    queue = agents.attach(session_id)
    send_lock = asyncio.Lock()
    writer: asyncio.Task | None = None

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            await ws.send_json(payload)

    try:
        # Subscribe first, then replay: an event produced between the replay
        # query and the subscription would otherwise fall through the gap.
        #
        # A fresh entry omits afterSeq: the client has just loaded the whole
        # transcript from grok's own history (`/messages`), so replaying the
        # journal from 0 would double every completed turn. Tail from the
        # current high-water mark instead, and replay only an in-flight turn so
        # a session opened mid-generation still streams. A reconnect within the
        # same screen sends the last seq it saw and gets an exact gap fill.
        raw_after = hello.get("afterSeq")
        if raw_after is None:
            after_seq = store.latest_seq(session_id)
            missed = agents.active_turn_events(session_id)
        else:
            after_seq = int(raw_after)
            missed = store.events_after(session_id, after_seq)
        live_proc = agents._procs.get(session_id)
        effective_effort = (
            live_proc.reasoning_effort
            if live_proc is not None and live_proc.alive
            else session["reasoning_effort"]
        )
        await send({
            "type": "hello.ack",
            "sessionId": session_id,
            "currentSeq": store.latest_seq(session_id),
            "sessionState": agents.state_of(session_id),
            "replayed": len(missed),
            "cwd": session["cwd"],
            "config": {
                "model": _session_model(session, live_proc),
                "reasoningEffort": effective_effort,
                "reasoningEffortConfigured": effective_effort is not None,
            },
        })
        for event in missed:
            await send(event)

        writer = asyncio.create_task(_v2_writer(send, queue, after_seq))
        turn: asyncio.Task | None = None

        while True:
            frame = await ws.receive_json()
            kind = frame.get("type")

            if kind == "prompt":
                if turn is not None and not turn.done():
                    await send({"type": "error", "code": "session_busy",
                                "message": "A turn is already running in this session."})
                    continue
                turn = asyncio.create_task(_v2_turn(session_id, session, frame))
            elif kind == "command":
                # ACP-native commands stream their output, so they go in as a
                # prompt; bridge and shell commands are request/response and
                # publish a single result event. Either way the outcome lands on
                # the journalled stream, in order with the rest of the session.
                name = (frame.get("name") or "").lstrip("/").strip()
                args = frame.get("args")
                canonical, ckind = cmd_registry.resolve(name)
                if ckind == "acp":
                    text = f"/{canonical}" + (f" {args}" if args else "")
                    if turn is not None and not turn.done():
                        await send({"type": "error", "code": "session_busy",
                                    "message": "A turn is already running in this session."})
                    else:
                        turn = asyncio.create_task(
                            _v2_turn(session_id, session, {**frame, "text": text}))
                else:
                    asyncio.create_task(_v2_command(session_id, session, name, args))
            elif kind == "cancel":
                await agents.cancel(session_id)
            elif kind == "ping":
                await send({"type": "pong"})
            else:
                await send({"type": "error", "code": "unknown_frame",
                            "message": f"unsupported frame type: {kind!r}"})

    except (WebSocketDisconnect, RuntimeError, ValueError):
        # A dropped socket is NOT a cancellation here: the turn keeps running and
        # the client picks it up again with afterSeq. That is the whole point.
        pass
    except Exception:
        log.exception("session %s: v2 socket failed", session_id)
    finally:
        if writer is not None:
            writer.cancel()
        agents.detach(session_id, queue)
        with contextlib.suppress(Exception):
            await ws.close()


async def _v2_writer(send, queue: asyncio.Queue, after_seq: int) -> None:
    """Forward live events, skipping anything the replay already delivered."""
    while True:
        event = await queue.get()
        if event.get("seq") is not None and event["seq"] <= after_seq:
            continue
        await send(event)


async def _v2_turn(session_id: str, session, frame: dict[str, Any]) -> None:
    """Run one turn, reporting entirely through the journalled event stream.

    Nothing is written straight to the socket: every event goes through the
    manager so it is numbered and persisted, which is what lets a client that
    disconnects mid-turn replay it.
    """
    # The socket keeps the row from hello, but another device may have changed
    # model/effort since then. Read the current row before validating or
    # starting a turn so the prompt cannot revive stale configuration.
    session = store.get_session(session_id) or session
    try:
        _validate_prompt_config(session, frame, agents._procs.get(session_id))
    except HTTPException as e:
        agents.emit(session_id, {
            "type": "message.error", "code": "invalid_config",
            "message": str(e.detail)})
        return

    job_id = store.create_job(session_id)
    agents.set_job(session_id, job_id)
    store.add_message_preview(session_id, "user", frame.get("text", ""))
    store.touch_session(session_id)

    status = "done"
    try:
        result = await agents.prompt(
            session_id=session_id,
            grok_session_id=session["grok_session_id"],
            cwd=Path(session["cwd"]),
            blocks=_prompt_blocks(frame.get("text", ""), frame.get("attachments")),
            model=frame.get("model"),
            reasoning_effort=frame.get("reasoningEffort"),
            resume=True if _session_source(session) == "discovered" else None,
        )
        _persist_prompt_config(session_id, session, frame)
        stop_reason = result.get("stopReason")
        if stop_reason == "cancelled":
            status = "cancelled"
        agents.emit(session_id, {"type": "message.done", "stopReason": stop_reason,
                                 "usage": result.get("usage")})
    except (AgentBusy, AgentCapacity) as e:
        status = "error"
        agents.emit(session_id, {"type": "message.error", "code": "busy",
                                 "message": str(e)})
    except SandboxNotEnforced as e:
        status = "error"
        log.error("refusing to serve session %s: %s", session_id, e)
        agents.emit(session_id, {
            "type": "message.error", "code": "sandbox_not_enforced",
            "message": "The agent sandbox is not active on the Mac, so the Bridge "
                       "refused to start an agent."})
    except AgentUnavailable as e:
        status = "error"
        log.warning("session %s: agent unavailable: %s", session_id, e)
        agents.emit(session_id, {
            "type": "message.error", "code": "agent_unavailable",
            "message": "The agent stopped unexpectedly. Try sending again."})
    except Exception:
        status = "error"
        log.exception("session %s: v2 turn failed", session_id)
        agents.emit(session_id, {
            "type": "message.error", "code": "internal",
            "message": "The Bridge hit an internal error handling this turn."})
    finally:
        agents.set_job(session_id, None)
        store.finish_job(job_id, status)
        store.prune_session_events(session_id)


# ------------------------------------------------------- workspace / picker


@app.get("/v1/repos")
def list_repos(request: Request, refresh: bool = False,
               authorization: str | None = Header(None)) -> dict[str, Any]:
    """Git repositories under the allowed roots.

    Deliberately returns path and name only. Branch and dirty count each cost a
    subprocess, and `git status` genuinely hangs in at least one worktree on
    this machine -- so status is a separate, bounded call for the rows a client
    is actually showing.
    """
    _check_bearer(authorization, request)
    return {"repos": workspace.find_repos(refresh=refresh)}


@app.post("/v1/repos/status")
def repos_status(body: dict[str, Any], request: Request,
                 authorization: str | None = Header(None)) -> dict[str, Any]:
    _check_bearer(authorization, request)
    paths = body.get("paths") or []
    if not isinstance(paths, list) or len(paths) > 100:
        raise HTTPException(400, "paths must be a list of at most 100 entries")
    return {"status": workspace.repo_status([str(p) for p in paths])}


@app.get("/v1/fs/list")
def fs_list(request: Request, path: str = str(config.HOME),
            authorization: str | None = Header(None)) -> dict[str, Any]:
    _check_bearer(authorization, request)
    try:
        return workspace.list_dir(path)
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except (FileNotFoundError, NotADirectoryError) as e:
        raise HTTPException(404, str(e)) from e


# ------------------------------------------------------------- sessions


@app.get("/v1/sessions/{session_id}/messages")
def session_messages(session_id: str, request: Request, limit: int = 50,
                     before: int | None = None,
                     authorization: str | None = Header(None)) -> dict[str, Any]:
    """Replay the conversation from grok's own transcript.

    The Bridge stores only 200-character previews, so before this endpoint
    existed, leaving a session and coming back showed an empty screen. Paginated
    because a long session is thousands of turns.
    """
    _check_bearer(authorization, request)
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    return grok_disk.read_history(session["cwd"], session["grok_session_id"],
                                  limit=min(max(limit, 1), 200), before=before)


@app.patch("/v1/sessions/{session_id}")
async def patch_session(session_id: str, body: dict[str, Any], request: Request,
                        authorization: str | None = Header(None)) -> dict[str, Any]:
    _check_bearer(authorization, request)
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")

    has_model = "model" in body and body["model"] not in (None, "")
    has_effort = "reasoningEffort" in body
    if has_model and not isinstance(body["model"], str):
        raise HTTPException(400, "model must be a string")
    if has_effort and body["reasoningEffort"] not in (None, "") \
            and not isinstance(body["reasoningEffort"], str):
        raise HTTPException(400, "reasoningEffort must be a string or null")
    if has_model or has_effort:
        can_run, reason = _runtime_access(session["cwd"], _session_source(session))
        if not can_run:
            raise HTTPException(403, reason or "session is read-only")

    fields: dict[str, Any] = {}
    if "title" in body:
        fields["title"] = body["title"]
    if has_model:
        fields["model"] = body["model"]
    proc = agents._procs.get(session_id)
    live_proc = proc if proc is not None and proc.alive else None
    selected_model = body.get("model") if has_model \
        else _session_model(session, live_proc)
    if has_model:
        _validate_model_choice(body["model"], live_proc)

    target_effort = (body.get("reasoningEffort") or None) if has_effort \
        else session["reasoning_effort"]
    effort_was_cleared = False
    if target_effort:
        try:
            _validate_effort_choice(target_effort, selected_model, live_proc)
        except HTTPException:
            if has_model and not has_effort:
                # A model switch should not strand the session with an effort
                # level the new model cannot accept. Fall back to that model's
                # default and restart a live agent below if necessary.
                target_effort = None
                effort_was_cleared = True
            else:
                raise

    if has_effort or effort_was_cleared:
        fields["reasoning_effort"] = target_effort

    needs_restart = bool(live_proc and (
        effort_was_cleared
        or (has_effort and target_effort != live_proc.reasoning_effort)
        or (has_model and target_effort != live_proc.reasoning_effort
            and target_effort is not None)
    ))
    if live_proc is not None and live_proc.state is SessionState.RUNNING \
            and (needs_restart or has_model):
        raise HTTPException(409, "cannot change session configuration while a turn is running")
    if live_proc is not None and needs_restart:
        try:
            await agents.restart(
                session_id=session_id,
                grok_session_id=live_proc.grok_session_id,
                cwd=Path(session["cwd"]),
                model=selected_model or live_proc.model,
                reasoning_effort=target_effort,
            )
        except AgentBusy as e:
            raise HTTPException(409, str(e)) from e
    elif live_proc is not None and has_model:
        try:
            if selected_model != _session_model(session, live_proc):
                await live_proc.set_model(selected_model)
        except AgentBusy as e:
            raise HTTPException(409, str(e)) from e

    if fields:
        store.update_session(session_id, **fields)
    config_changed = has_model or has_effort or effort_was_cleared
    updated = store.get_session(session_id)
    if config_changed and updated is not None:
        agents.emit(session_id, {
            "type": "config.changed",
            "config": {
                "model": _session_model(updated, agents._procs.get(session_id)),
                "reasoningEffort": updated["reasoning_effort"],
                "reasoningEffortConfigured": updated["reasoning_effort"] is not None,
            },
        })
    if updated is None:
        raise HTTPException(404, "unknown session")
    return {
        "ok": True,
        "model": _session_model(updated, agents._procs.get(session_id)),
        "reasoningEffort": updated["reasoning_effort"],
        **({"title": updated["title"]} if "title" in body else {}),
    }


@app.delete("/v1/sessions/{session_id}")
async def delete_session(session_id: str, request: Request,
                         authorization: str | None = Header(None)) -> dict[str, bool]:
    _check_bearer(authorization, request)
    await agents.retire(session_id)
    return {"deleted": store.delete_session(session_id)}


# ------------------------------------------------------------- commands


@app.get("/v1/sessions/{session_id}/commands")
async def session_commands(session_id: str, request: Request,
                           authorization: str | None = Header(None)) -> dict[str, Any]:
    """The merged command palette for one session.

    ACP-native commands are discovered live from the agent; the rest are
    implemented here because grok does not expose them over ACP at all. Each
    entry says which it is, so the UI can be honest about what it is offering.
    """
    _check_bearer(authorization, request)
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")

    proc = agents._procs.get(session_id)
    acp_commands = proc.available_commands if proc and proc.alive else []
    session_models = proc.models if proc and proc.alive else {}
    current_model = (session_models.get("currentModelId") if session_models else None) \
        or _session_model(session, proc if proc and proc.alive else None)

    return {
        # ACP-native commands are discovered from a running agent, so an idle
        # session lists only the bridge-native ones. Say so, rather than letting
        # the palette look mysteriously short.
        "acpAvailable": bool(proc and proc.alive),
        "commands": cmd_registry.describe(
            acp_commands=acp_commands, session_models=session_models,
            current_model=current_model),
    }


@app.post("/v1/sessions/{session_id}/command")
async def run_command(session_id: str, body: dict[str, Any], request: Request,
                      authorization: str | None = Header(None)) -> dict[str, Any]:
    """Run a bridge-native or shell command.

    ACP-native commands are not handled here: they produce streaming output and
    belong on the session's event stream, so the client sends them as a prompt.
    """
    _check_bearer(authorization, request)
    session = store.get_session(session_id)
    if session is None:
        raise HTTPException(404, "unknown session")
    can_run, reason = _runtime_access(session["cwd"], _session_source(session))
    if not can_run:
        raise HTTPException(403, reason or "session is read-only")
    try:
        return await _execute_command(session_id, session, body.get("name", ""),
                                      body.get("args"))
    except cmd_registry.CommandError as e:
        raise HTTPException(400, str(e)) from e


async def _execute_command(session_id: str, session, name: str,
                           args: str | None) -> dict[str, Any]:
    can_run, reason = _runtime_access(session["cwd"], _session_source(session))
    if not can_run:
        raise cmd_registry.CommandError(reason or "session is read-only")
    name = (name or "").lstrip("/").strip()
    canonical, kind = cmd_registry.resolve(name)

    if kind == "shell":
        return {"command": canonical, "kind": "shell",
                **await cmd_registry.run_shell(canonical, session["cwd"])}

    if kind != "bridge":
        raise cmd_registry.CommandError(
            f"/{name} is an agent command -- send it as a prompt, not a command call")

    if canonical == "cwd":
        return {"command": "cwd", "kind": "bridge", "text": session["cwd"]}

    if canonical == "help":
        proc = agents._procs.get(session_id)
        return {"command": "help", "kind": "bridge",
                "data": cmd_registry.describe(
                    acp_commands=proc.available_commands if proc and proc.alive else [])}

    if canonical == "rename":
        if not args:
            raise cmd_registry.CommandError("/rename needs a title")
        store.update_session(session_id, title=args)
        agents.emit(session_id, {"type": "session.titled", "title": args})
        return {"command": "rename", "kind": "bridge", "text": args}

    if canonical == "model":
        if not args:
            raise cmd_registry.CommandError("/model needs a model id")
        proc = agents._procs.get(session_id)
        live_proc = proc if proc is not None and proc.alive else None
        try:
            _validate_model_choice(args, live_proc)
        except HTTPException as e:
            raise cmd_registry.CommandError(str(e.detail)) from e

        target_effort = session["reasoning_effort"] or None
        effort_was_cleared = False
        if target_effort:
            try:
                _validate_effort_choice(target_effort, args, live_proc)
            except HTTPException:
                target_effort = None
                effort_was_cleared = True

        if live_proc is not None and live_proc.state is SessionState.RUNNING:
            raise cmd_registry.CommandError(
                "cannot change model while a turn is running")
        if live_proc is not None and (
                effort_was_cleared or target_effort != live_proc.reasoning_effort):
            try:
                await agents.restart(
                    session_id=session_id,
                    grok_session_id=live_proc.grok_session_id,
                    cwd=Path(session["cwd"]),
                    model=args,
                    reasoning_effort=target_effort,
                )
            except AgentBusy as e:
                raise cmd_registry.CommandError(str(e)) from e
        elif live_proc is not None and args != _session_model(session, live_proc):
            try:
                await live_proc.set_model(args)
            except AgentBusy as e:
                raise cmd_registry.CommandError(str(e)) from e

        fields = {"model": args}
        if effort_was_cleared:
            fields["reasoning_effort"] = None
        store.update_session(session_id, **fields)
        updated = store.get_session(session_id)
        if updated is not None:
            agents.emit(session_id, {
                "type": "config.changed",
                "config": {
                    "model": _session_model(updated, agents._procs.get(session_id)),
                    "reasoningEffort": updated["reasoning_effort"],
                    "reasoningEffortConfigured": updated["reasoning_effort"] is not None,
                },
            })
        return {"command": "model", "kind": "bridge", "text": args}

    if canonical == "effort":
        if not args:
            raise cmd_registry.CommandError("/effort needs a level")
        proc = agents._procs.get(session_id)
        try:
            _validate_effort_choice(
                args, _session_model(session, proc), proc if proc and proc.alive else None)
        except HTTPException as e:
            raise cmd_registry.CommandError(str(e.detail)) from e
        # No ACP channel exists for reasoning effort -- it is a spawn-time flag.
        # Restarting reloads the session, and context survives that.
        restarted = False
        if proc is not None and proc.alive:
            if proc.state is SessionState.RUNNING:
                raise cmd_registry.CommandError(
                    "cannot change reasoning effort while a turn is running")
            try:
                await agents.restart(session_id=session_id,
                                     grok_session_id=session["grok_session_id"],
                                     cwd=Path(session["cwd"]),
                                     model=_session_model(session, proc) or proc.model,
                                     reasoning_effort=args)
            except AgentBusy as e:
                raise cmd_registry.CommandError(str(e)) from e
            restarted = True
        store.update_session(session_id, reasoning_effort=args)
        updated = store.get_session(session_id)
        agents.emit(session_id, {
            "type": "config.changed",
            "config": {
                "model": _session_model(updated, agents._procs.get(session_id))
                if updated is not None else None,
                "reasoningEffort": args,
                "reasoningEffortConfigured": True,
            },
        })
        return {"command": "effort", "kind": "bridge", "text": args,
                "note": ("agent restarted; conversation context preserved"
                         if restarted else "will apply when the agent starts")}

    if canonical == "clear":
        # A new grok-side conversation in the same directory, keeping the
        # Bridge-facing session id so the client's URLs stay valid.
        import uuid as _uuid
        await agents.retire(session_id)
        new_grok_id = str(_uuid.uuid4())
        # The new conversation has no imported Grok transcript to load. Mark
        # it as Bridge-owned so the next prompt uses session/new rather than
        # trying session/load with the freshly minted id.
        store.update_session(session_id, grok_session_id=new_grok_id, source="bridge")
        agents.forget(session_id)
        agents.emit(session_id, {"type": "session.cleared"})
        return {"command": "clear", "kind": "bridge", "text": "started a new conversation"}

    raise cmd_registry.CommandError(f"unknown command: /{name}")


async def _v2_command(session_id: str, session, name: str, args: str | None) -> None:
    """Run a non-streaming command and publish its result on the event stream."""
    try:
        # A second device may have changed the session since this socket's
        # hello. Commands that inspect or mutate model/effort must use the
        # current row, not the hello snapshot.
        session = store.get_session(session_id) or session
        result = await _execute_command(session_id, session, name, args)
        agents.emit(session_id, {"type": "command.result", **result})
    except cmd_registry.CommandError as e:
        agents.emit(session_id, {"type": "command.error", "command": name,
                                 "message": str(e)})
    except Exception:
        log.exception("session %s: command /%s failed", session_id, name)
        agents.emit(session_id, {"type": "command.error", "command": name,
                                 "message": "The command failed on the Bridge."})

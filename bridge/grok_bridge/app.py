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

from . import config
from .acp import AgentBusy, AgentUnavailable
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
    _check_bearer(authorization, request)
    proc = subprocess.run([config.GROK_BIN, "models"], capture_output=True, text=True, timeout=15)
    models = []
    default_model = None
    for line in proc.stdout.splitlines():
        m = re.match(r"^\s*([*-])\s+(\S+)", line)
        if not m:
            continue
        marker, name = m.groups()
        is_default = marker == "*"
        if is_default:
            default_model = name
        models.append({
            "id": name,
            "default": is_default,
            "vision": _model_vision_capability(name),
        })
    return {"models": models, "default": default_model}


def _model_vision_capability(model_id: str) -> bool:
    # Unknown-model-default-false capability cache (plan v1.3 §1 "vision 能力判断").
    # xAI first-party models assumed vision-capable; local qwen/muse models
    # assumed not, until manually verified and flipped here.
    known_vision_models = {"grok-4.6", "grok-4.5"}
    return model_id in known_vision_models


@app.get("/v1/recent-dirs")
def recent_dirs(request: Request, authorization: str | None = Header(None)) -> dict[str, list[str]]:
    _check_bearer(authorization, request)
    return {"dirs": store.recent_dirs()}


@app.get("/v1/sessions")
def list_sessions(request: Request, authorization: str | None = Header(None)) -> dict[str, Any]:
    _check_bearer(authorization, request)
    sessions = []
    for row in store.list_sessions():
        sessions.append({
            "id": row["id"],
            "cwd": row["cwd"],
            "title": row["title"],
            "created_at": row["created_at"],
            "last_active_at": row["last_active_at"],
            "last_message_preview": store.last_message_preview(row["id"]),
        })
    return {"sessions": sessions}


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

    queue = agents.attach(session_id)
    send_lock = asyncio.Lock()
    writer: asyncio.Task | None = None

    async def send(payload: dict[str, Any]) -> None:
        async with send_lock:
            await ws.send_json(payload)

    try:
        # Subscribe first, then replay: an event produced between the replay
        # query and the subscription would otherwise fall through the gap.
        after_seq = int(hello.get("afterSeq") or 0)
        missed = store.events_after(session_id, after_seq)
        await send({
            "type": "hello.ack",
            "sessionId": session_id,
            "currentSeq": store.latest_seq(session_id),
            "sessionState": agents.state_of(session_id),
            "replayed": len(missed),
            "cwd": session["cwd"],
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
        )
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

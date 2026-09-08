"""FastAPI app: the Bridge's HTTP/WebSocket surface (plan v1.3 §1 "接口设计").

Auth: every /v1/* route except /v1/pair requires `Authorization: Bearer
<device_id>.<secret>` matching a row in the devices table. Cloudflare
Access (Service Token) sits in front of this in production (§2 of the
plan) -- that's a separate network-layer check done by the tunnel, not by
this app.
"""

from __future__ import annotations

import re
import subprocess
import time
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, Header, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse

from . import config
from .agent_manager import AgentManager
from .db import Store
from .security import InvalidCwd, validate_cwd

app = FastAPI(title="grok-remote-bridge")
store = Store()
agents = AgentManager()
START_TIME = time.time()


def _check_bearer(authorization: str | None) -> None:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    token = authorization.removeprefix("Bearer ").strip()
    device_id, _, secret = token.partition(".")
    if not device_id or not secret or not store.check_device_token(device_id, secret):
        raise HTTPException(401, "invalid device token")


@app.get("/health")
def health() -> dict[str, Any]:
    grok_reachable = Path(config.GROK_BIN).exists()
    return {
        "status": "ok",
        "grok_reachable": grok_reachable,
        "active_jobs": len(agents._running_procs),
        "uptime_s": round(time.time() - START_TIME, 1),
    }


@app.post("/v1/pair")
def pair(body: dict[str, str]) -> dict[str, str]:
    token = body.get("pairing_token", "")
    result = store.redeem_pairing_token(token, device_name=body.get("device_name", "iPhone"))
    if result is None:
        raise HTTPException(400, "invalid or already-used pairing token")
    device_id, secret = result
    return {"device_id": device_id, "secret": secret}


@app.delete("/v1/devices/{device_id}")
def revoke_device(device_id: str, authorization: str | None = Header(None)) -> dict[str, bool]:
    _check_bearer(authorization)
    return {"revoked": store.revoke_device(device_id)}


@app.get("/v1/models")
def list_models(authorization: str | None = Header(None)) -> dict[str, Any]:
    _check_bearer(authorization)
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
def recent_dirs(authorization: str | None = Header(None)) -> dict[str, list[str]]:
    _check_bearer(authorization)
    return {"dirs": store.recent_dirs()}


@app.get("/v1/sessions")
def list_sessions(authorization: str | None = Header(None)) -> dict[str, Any]:
    _check_bearer(authorization)
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
def create_session(body: dict[str, str], authorization: str | None = Header(None)) -> dict[str, str]:
    _check_bearer(authorization)
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
async def upload(file: UploadFile, authorization: str | None = Header(None)) -> dict[str, str]:
    _check_bearer(authorization)
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
    await ws.accept()
    try:
        first = await ws.receive_json()
    except Exception:
        await ws.close(code=4400)
        return

    token = first.get("token", "")
    device_id, _, secret = token.partition(".")
    if not device_id or not secret or not store.check_device_token(device_id, secret):
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

    cancel_requested = False

    async def _watch_for_cancel():
        nonlocal cancel_requested
        try:
            while True:
                msg = await ws.receive_json()
                if msg.get("type") == "cancel":
                    cancel_requested = True
                    await agents.cancel(session_id)
                    return
        except WebSocketDisconnect:
            cancel_requested = True
            await agents.cancel(session_id)

    import asyncio
    watcher = asyncio.create_task(_watch_for_cancel())

    final_status = "done"
    last_text = ""
    try:
        async for event in agents.run_turn(
            job_id=job_id,
            session_id=session_id,
            grok_session_id=session["grok_session_id"],
            cwd=Path(session["cwd"]),
            text=first.get("text", ""),
            model=first.get("model"),
            reasoning_effort=first.get("reasoningEffort"),
            attachments=first.get("attachments"),
        ):
            if event["type"] == "message.delta":
                last_text += event.get("data", "")
            if event["type"] == "cancelled":
                final_status = "cancelled"
            elif event["type"] == "message.error":
                final_status = "error"
            try:
                await ws.send_json(event)
            except Exception:
                break
    finally:
        watcher.cancel()
        store.finish_job(job_id, final_status)
        if final_status == "cancelled":
            # grok's session lock can get stuck after a SIGTERM'd turn (see
            # db.rotate_grok_session_id) -- rotate now so the *next* message
            # in this Bridge session doesn't hit "Session ID already in use".
            store.rotate_grok_session_id(session_id)
        if last_text:
            store.add_message_preview(session_id, "assistant", last_text)
        try:
            await ws.close()
        except Exception:
            pass

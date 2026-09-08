#!/usr/bin/env python3
"""Spike round 3 -- the last two open questions.

Q4 (rerun): with ~/.grok/config.toml permission_mode temporarily set to "default",
    does session/request_permission finally fire, and what are the REAL
    {optionId, name, kind} triples?              <-- GATES THE PermissionSheet FEATURE
Q5:        does the ACP path support image content blocks at all? Round 1 saw
    promptCapabilities.image == false, but that may have been the (non-vision)
    default model. Re-check on grok-4.6 and actually send an image block.

The caller is responsible for backing up / restoring config.toml.
"""
from __future__ import annotations

import asyncio
import base64
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acp_spike import Acp, handshake, show, banner, FINDINGS  # noqa: E402

# 1x1 red PNG
PNG_B64 = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmM"
           "IQAAAABJRU5ErkJggg==")


async def run_q4() -> dict:
    banner("Q4  permission_mode=default -> does session/request_permission fire?")
    cwd = tempfile.mkdtemp(prefix="acp-spike3-q4-")
    a = Acp()
    a.argv = [a.argv[0], "agent", "-m", "grok-4.5", "stdio"]
    await a.start()
    await handshake(a, "q4")
    new = await a.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=90)
    if not isinstance(new, dict) or "sessionId" not in new:
        show("Q4 session/new FAILED", new)
        await a.stop()
        return {"ok": False}
    sid = new["sessionId"]

    FINDINGS.pop("permission_fired", None)
    FINDINGS.pop("permission_options", None)
    a.tool_names.clear()
    await a.prompt(sid, "Run the shell command `echo spike3-approval-probe` using your "
                        "tools, then report its output.", timeout=240)
    fired = FINDINGS.get("permission_fired", False)
    opts = FINDINGS.get("permission_options")
    print(f"\n[Q4] tools={a.tool_names}")
    print(f"[Q4] request_permission fired = {fired}")
    if opts:
        print("[Q4] REAL option triples:")
        for o in opts:
            print("   ", json.dumps(o, ensure_ascii=False))
    await a.stop()
    if a.stderr_buf.strip():
        print(f"[q4 STDERR] {a.stderr_buf.decode(errors='replace')[:800]}")
    return {"ok": True, "fired": fired, "options": opts}


async def run_q5() -> dict:
    banner("Q5  image support on grok-4.6 (vision model)")
    cwd = tempfile.mkdtemp(prefix="acp-spike3-q5-")
    b = Acp()
    b.argv = [b.argv[0], "agent", "-m", "grok-4.6", "stdio"]
    await b.start()
    init = await handshake(b, "q5")
    caps = (init or {}).get("agentCapabilities", {}) if isinstance(init, dict) else {}
    pc = caps.get("promptCapabilities")
    print(f"\n[Q5] promptCapabilities on grok-4.6 = {json.dumps(pc, ensure_ascii=False)}")

    new = await b.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=90)
    sent_ok = None
    if isinstance(new, dict) and "sessionId" in new:
        sid = new["sessionId"]
        cur = (new.get("models") or {}).get("currentModelId")
        print(f"[Q5] currentModelId={cur}")
        b.text.clear()
        res = await b.call("session/prompt", {
            "sessionId": sid,
            "prompt": [
                {"type": "text", "text": "What single color fills this image? One word."},
                {"type": "image", "mimeType": "image/png", "data": PNG_B64},
            ],
        }, timeout=240)
        show("[Q5] image prompt result", res, limit=1200)
        txt = "".join(b.text)
        print(f"[Q5] reply: {txt[:300]}")
        sent_ok = isinstance(res, dict) and "error" not in res
    await b.stop()
    if b.stderr_buf.strip():
        print(f"[q5 STDERR] {b.stderr_buf.decode(errors='replace')[:800]}")
    return {"promptCapabilities": pc, "image_block_accepted": sent_ok}


async def main() -> int:
    q4 = await run_q4()
    q5 = await run_q5()
    banner("ROUND 3 VERDICT")
    print(f"Q4 request_permission fired    : {q4.get('fired')}   <-- GATES PermissionSheet")
    if q4.get("options"):
        print(f"Q4 option triples              : {json.dumps(q4['options'], ensure_ascii=False)}")
    print(f"Q5 promptCapabilities (4.6)    : {json.dumps(q5.get('promptCapabilities'), ensure_ascii=False)}")
    print(f"Q5 image block accepted        : {q5.get('image_block_accepted')}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

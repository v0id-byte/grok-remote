#!/usr/bin/env python3
"""Spike round 4 -- settle the image question with a real (96x96) image.

Round 3 sent a 1x1 PNG; grok replied "dropped as too small", so the test said
nothing about whether ACP image blocks work. Retry on grok-4.6 with a real image.
"""
from __future__ import annotations
import asyncio, json, os, sys, tempfile, pathlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acp_spike import Acp, handshake, show, banner  # noqa: E402

B64 = pathlib.Path("/tmp/spike-red96.b64").read_text().strip()


async def main() -> int:
    cwd = tempfile.mkdtemp(prefix="acp-spike4-")
    b = Acp()
    b.argv = [b.argv[0], "agent", "-m", "grok-4.6", "stdio"]
    await b.start()
    await handshake(b, "s4")
    new = await b.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=90)
    sid = new["sessionId"]
    banner("image block, 96x96 solid red, on grok-4.6")
    b.text.clear()
    res = await b.call("session/prompt", {
        "sessionId": sid,
        "prompt": [
            {"type": "text", "text": "Name the single solid color that fills this image. "
                                     "Answer with one word only."},
            {"type": "image", "mimeType": "image/png", "data": B64},
        ],
    }, timeout=240)
    txt = "".join(b.text)
    show("prompt result", res, limit=900)
    print(f"\nreply: {txt[:400]}")
    saw = "red" in txt.lower()
    print(f"\nVERDICT image actually perceived = {saw}")
    await b.stop()
    if b.stderr_buf.strip():
        print(f"[STDERR] {b.stderr_buf.decode(errors='replace')[:600]}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

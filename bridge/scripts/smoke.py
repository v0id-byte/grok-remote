#!/usr/bin/env python3
"""End-to-end smoke test against a running Bridge and a real grok agent.

Complements the fake-agent suite: those prove the protocol handling, this proves
the whole path actually works against grok 1.0.13 on this machine.

The cancellation check is the one that matters most -- it is the regression the
ACP rewrite exists to fix. The old per-turn `grok -p` had to mint a fresh grok
session id after every cancel, losing conversation continuity; here the session
must still be usable, and must still remember what came before the cancel.

    python3 bridge/scripts/smoke.py [--base http://127.0.0.1:8899]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import httpx2 as httpx
import websockets

REPO = Path(__file__).resolve().parents[2]
FAILURES: list[str] = []


def check(ok: bool, label: str, detail: str = "") -> None:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f"  [{detail}]" if detail else ""),
          flush=True)
    if not ok:
        FAILURES.append(label)


async def collect(ws, timeout=300.0) -> list[dict]:
    """Read one turn's events, up to and including its terminal event."""
    events = []
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout)
        event = json.loads(raw)
        events.append(event)
        if event["type"] in ("message.done", "message.error"):
            return events


async def turn(base_ws, token, session_id, text, model=None, cancel_after=None):
    async with websockets.connect(f"{base_ws}/v1/chat", max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"token": token, "sessionId": session_id,
                                  "text": text, "model": model, "attachments": []}))
        if cancel_after is not None:
            async def canceller():
                await asyncio.sleep(cancel_after)
                await ws.send(json.dumps({"type": "cancel"}))
            asyncio.create_task(canceller())
        return await collect(ws)


async def v2_resume(base_ws, token, session_id, model) -> None:
    """The claim /v2/chat exists to make good on.

    Start a turn, hang up mid-stream, reconnect saying how far we got, and get
    the rest. Under v1 a dropped socket cancelled the turn and the output was
    gone; iOS suspends backgrounded apps, so this is the normal case.
    """
    headers = {"Authorization": f"Bearer {token}"}
    seen_seq = 0
    saw_before = 0

    async with websockets.connect(f"{base_ws}/v2/chat", additional_headers=headers,
                                  max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "hello", "protocolVersion": 2,
                                  "sessionId": session_id, "afterSeq": 0}))
        ack = json.loads(await ws.recv())
        check(ack["type"] == "hello.ack", "v2 hello.ack received",
              f"currentSeq={ack.get('currentSeq')}")
        seen_seq = ack["currentSeq"]

        await ws.send(json.dumps({"type": "prompt", "model": model,
                                  "text": "Write a detailed 4000-word history of the "
                                          "bicycle. Do not use tools."}))
        # Read a little, then hang up hard, mid-turn.
        deadline = asyncio.get_event_loop().time() + 12
        while asyncio.get_event_loop().time() < deadline:
            event = json.loads(await asyncio.wait_for(ws.recv(), 60))
            seen_seq = max(seen_seq, event.get("seq") or 0)
            if event["type"] == "message.delta":
                saw_before += 1
                if saw_before >= 5:
                    break
    check(saw_before > 0, "v2 streamed events before the disconnect",
          f"{saw_before} deltas, seq={seen_seq}")

    # Socket is gone; the turn must keep running on the Mac.
    await asyncio.sleep(3)

    async with websockets.connect(f"{base_ws}/v2/chat", additional_headers=headers,
                                  max_size=32 * 1024 * 1024) as ws:
        await ws.send(json.dumps({"type": "hello", "protocolVersion": 2,
                                  "sessionId": session_id, "afterSeq": seen_seq}))
        ack = json.loads(await ws.recv())
        check(ack["replayed"] > 0, "reconnect replayed what was missed",
              f"replayed={ack['replayed']}")
        got_done = False
        seqs = []
        while True:
            event = json.loads(await asyncio.wait_for(ws.recv(), 300))
            seqs.append(event.get("seq"))
            if event["type"] in ("message.done", "message.error"):
                got_done = event["type"] == "message.done"
                break
        check(got_done, "the turn finished despite the disconnect")
        check(all(b > a for a, b in zip(seqs, seqs[1:])), "replayed seq is strictly ordered")
        check(min(seqs) > seen_seq, "no event was delivered twice",
              f"first replayed seq={min(seqs)} > {seen_seq}")


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8899")
    ap.add_argument("--model", default="grok-4.5")
    args = ap.parse_args()
    base_ws = args.base.replace("http://", "ws://").replace("https://", "wss://")

    # Must live under ALLOWED_ROOTS ($HOME by default) -- the Bridge refuses a
    # cwd outside it, which is itself worth knowing works.
    workspace = Path(tempfile.mkdtemp(prefix="grok-smoke-", dir=Path.home()))
    cwd = str(workspace)
    (workspace / "sentinel.txt").write_text("PINEAPPLE_7731\n")

    async with httpx.AsyncClient(base_url=args.base, timeout=60) as http:
        print("\n== health ==")
        health = (await http.get("/health")).json()
        check(health.get("status") == "ok", "bridge is up", json.dumps(health))
        check(health.get("grok_reachable") is True, "grok binary reachable")

        print("\n== pairing ==")
        pair_token = subprocess.run(
            [sys.executable, str(REPO / "bridge" / "main.py"), "pair-token"],
            capture_output=True, text=True, check=True).stdout.strip()
        r = await http.post("/v1/pair", json={"pairing_token": pair_token,
                                              "device_name": "smoke-test"})
        check(r.status_code == 200, "paired", f"HTTP {r.status_code}")
        if r.status_code != 200:
            return 1
        creds = r.json()
        token = f"{creds['device_id']}.{creds['secret']}"
        auth = {"Authorization": f"Bearer {token}"}

        print("\n== session ==")
        r = await http.post("/v1/sessions", json={"cwd": cwd}, headers=auth)
        check(r.status_code == 200, "session created", f"HTTP {r.status_code} {r.text[:120]}")
        if r.status_code != 200:
            return 1
        session_id = r.json()["id"]

        try:
            print("\n== turn 1: force a tool call ==")
            events = await turn(base_ws, token, session_id,
                                "Use your tools to read sentinel.txt in the current "
                                "directory and tell me the word it contains.", args.model)
            types = [e["type"] for e in events]
            text = "".join(e.get("data") or "" for e in events if e["type"] == "message.delta")
            check("tool.started" in types, "tool.started received")
            check("tool.finished" in types, "tool.finished received")
            check(types[-1] == "message.done", "turn ends with message.done", types[-1])
            check(types.count("message.done") == 1, "turn ends exactly once")
            check("PINEAPPLE_7731" in text, "agent read the workspace file")
            done = events[-1]
            check(bool(done.get("usage")), "usage present on the terminal event",
                  json.dumps(done.get("usage"))[:120])

            print("\n== turn 2: cancel mid-turn ==")
            # A long generation, not a shell sleep: grok backgrounds long shell
            # commands (_x.ai/task_backgrounded) and ends the turn immediately,
            # so a sleep never exercises cancellation at all.
            events = await turn(base_ws, token, session_id,
                                "Write an extremely detailed 12000-word history of the "
                                "bicycle, century by century. Do not use tools.",
                                args.model, cancel_after=5)
            done = next((e for e in events if e["type"] == "message.done"), {})
            check(done.get("stopReason") == "cancelled",
                  "cancelled turn reports stopReason=cancelled", str(done.get("stopReason")))

            print("\n== turn 3: session survives the cancel (the ACP payoff) ==")
            events = await turn(base_ws, token, session_id,
                                "Without using any tools: what word was in sentinel.txt? "
                                "Reply with just that word.", args.model)
            text = "".join(e.get("data") or "" for e in events if e["type"] == "message.delta")
            check(events[-1]["type"] == "message.done", "session still usable after cancel")
            check("PINEAPPLE_7731" in text,
                  "context survived the cancel (no session-id rotation)", text[:80])

            print("\n== sandbox is enforced ==")
            events_path = Path.home() / ".grok" / "sandbox-events.jsonl"
            applied = [json.loads(l) for l in events_path.read_text().splitlines() if l.strip()]
            ours = [e for e in applied if e.get("profile") == "grok-remote"]
            check(bool(ours) and ours[-1].get("enforced") is True,
                  "grok-remote profile applied and enforcing",
                  json.dumps(ours[-1])[:140] if ours else "no events")
            print("\n== v2: drop the socket mid-turn and resume ==")
            await v2_resume(base_ws, token, session_id, args.model)

            print("\n== a cwd outside the allowed roots is refused ==")
            r = await http.post("/v1/sessions", json={"cwd": "/etc"}, headers=auth)
            check(r.status_code == 403, "cwd outside allowed roots rejected",
                  f"HTTP {r.status_code}")
        finally:
            await http.delete(f"/v1/devices/{creds['device_id']}", headers=auth)
            shutil.rmtree(workspace, ignore_errors=True)

    print("\n" + ("ALL PASS" if not FAILURES else f"{len(FAILURES)} FAILED: {FAILURES}"))
    return 0 if not FAILURES else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

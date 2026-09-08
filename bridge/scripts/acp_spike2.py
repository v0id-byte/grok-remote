#!/usr/bin/env python3
"""Spike round 2 -- resolve what round 1 left open.

Round 1 outcomes that forced this: turn 1 died with a 401 (default model
qwen3.8-27b needs LLAMA_TRAIN_API_KEY, which the spike didn't have), so the
session/load context test was INCONCLUSIVE, not failed. And request_permission
never fired because ~/.grok/config.toml sets permission_mode = "always-approve".

Round 2 pins down, on a model that actually authenticates (grok-4.5):
  Q1  session/load across a process restart -- does context survive?   <-- GATES IDLE-REAP
  Q2  session/set_config_option's real param shape (round 1: needs `configId`)
  Q3  how reasoning effort is set
  Q4  with always-approve turned OFF, does session/request_permission fire,
      and what are the real option {optionId,name,kind} triples?        <-- GATES PHONE APPROVAL
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from acp_spike import Acp, handshake, show, banner, FINDINGS, SEEN  # noqa: E402

MODEL = "grok-4.5"
SENTINEL = "BANANA_SENTINEL_42"


async def main() -> int:
    cwd = tempfile.mkdtemp(prefix="acp-spike2-")
    with open(os.path.join(cwd, "hello.txt"), "w") as f:
        f.write(f"spike marker: {SENTINEL}\n")

    banner(f"round 2 -- forcing model={MODEL} at process start (avoids the 401)")
    a = Acp(["-m", MODEL])          # NOTE: -m lives on `grok agent`, before `stdio`
    a.argv = [a.argv[0], "agent", "-m", MODEL, "stdio"]
    await a.start()
    await handshake(a, "r2p1")

    new = await a.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=90)
    if not isinstance(new, dict) or "sessionId" not in new:
        show("SESSION/NEW FAILED", new)
        await a.stop()
        return 2
    sid = new["sessionId"]
    cur = (new.get("models") or {}).get("currentModelId")
    print(f"\n[r2] sessionId={sid}  currentModelId={cur}")

    banner("Q1a  turn 1 on a working model -- put the sentinel into conversation context")
    res = await a.prompt(sid, "Use your tools to read hello.txt in the current working "
                              "directory and tell me the sentinel word it contains.",
                         timeout=240)
    t1 = "".join(a.text)
    ok1 = SENTINEL in t1
    print(f"[Q1a] tools={a.tool_names}  sentinel_in_reply={ok1}")
    print(f"[Q1a] text: {t1[:300]}")
    if not ok1:
        show("Q1a PROMPT RESULT (turn 1 did not succeed)", res, limit=1500)

    banner("Q2/Q3  set_config_option real shape; how is reasoning effort set?")
    probes = [
        ("session/set_config_option", {"sessionId": sid, "configId": "grok-4.6"}),
        ("session/set_config_option", {"sessionId": sid, "configId": "model",
                                       "value": "grok-4.6"}),
        ("session/set_model", {"sessionId": sid, "modelId": MODEL}),
        ("session/set_model", {"sessionId": sid, "modelId": MODEL,
                               "_meta": {"reasoningEffort": "low"}}),
        ("session/set_mode", {"sessionId": sid, "modeId": "default"}),
    ]
    for method, params in probes:
        r = await a.call(method, params, timeout=45)
        label = f"{method} {json.dumps({k: v for k, v in params.items() if k != 'sessionId'})}"
        print(f"\n  PROBE {label}\n    -> {json.dumps(r, ensure_ascii=False)[:400]}")

    banner("Q4  turn always-approve OFF via the ACP command, then force a shell tool call")
    r = await a.prompt(sid, "/always-approve off", timeout=120)
    print(f"[Q4] '/always-approve off' -> {json.dumps(r, ensure_ascii=False)[:300]}")
    print(f"[Q4] agent said: {''.join(a.text)[:300]}")

    FINDINGS.pop("permission_fired", None)
    FINDINGS.pop("permission_options", None)
    a.tool_names.clear()
    res = await a.prompt(sid, "Run the shell command `echo spike-approval-probe` "
                              "using your tools and report the output.", timeout=240)
    print(f"\n[Q4] tools={a.tool_names}")
    print(f"[Q4] request_permission fired = {FINDINGS.get('permission_fired', False)}")
    if FINDINGS.get("permission_options"):
        print("[Q4] REAL option triples:")
        for o in FINDINGS["permission_options"]:
            print("   ", json.dumps(o, ensure_ascii=False))

    await a.stop()
    if a.stderr_buf.strip():
        print(f"\n[r2p1 STDERR]\n{a.stderr_buf.decode(errors='replace')[:1500]}")

    banner("Q1b  NEW PROCESS -> session/load -> is the sentinel still in context?")
    b = Acp()
    b.argv = [b.argv[0], "agent", "-m", MODEL, "stdio"]
    await b.start()
    await handshake(b, "r2p2")
    load = await b.call("session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": []},
                        timeout=150)
    load_ok = isinstance(load, dict) and "error" not in load and not load.get("__timeout__")
    print(f"\n[Q1b] session/load ok = {load_ok}")
    recalled = False
    if load_ok:
        await b.prompt(sid, "Without using any tools: what was the sentinel word in "
                            "hello.txt that I asked about earlier in this conversation? "
                            "Reply with just the word.", timeout=180)
        joined = "".join(b.text)
        recalled = SENTINEL in joined
        print(f"[Q1b] reply: {joined[:300]}")
    await b.stop()
    if b.stderr_buf.strip():
        print(f"\n[r2p2 STDERR]\n{b.stderr_buf.decode(errors='replace')[:1500]}")

    banner("ROUND 2 VERDICT")
    print(f"Q1  turn1 established context      : {ok1}")
    print(f"Q1  session/load ok                : {load_ok}")
    print(f"Q1  CONTEXT SURVIVED RESTART       : {recalled}   <-- GATES IDLE-REAP")
    print(f"Q4  request_permission fired       : {FINDINGS.get('permission_fired', False)}"
          f"   <-- GATES PHONE APPROVAL")
    print("\nsessionUpdate variants seen this run:")
    for k, v in sorted(SEEN.items(), key=lambda kv: -kv[1])[:18]:
        print(f"  {v:6d}  {k}")
    print(f"\ntemp cwd -> {cwd}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

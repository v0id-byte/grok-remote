#!/usr/bin/env python3
"""Step-0 ACP spike -- confirm `grok agent stdio` speaks the dialect the v2 design assumes.

Read-only w.r.t. the bridge codebase. Creates throwaway grok sessions under temp cwds.

Answers the eleven judgements in the plan's Phase 0.5. The four that actually gate
the architecture:

  #7/#8  does `agent stdio` honor --deny, and do Bash tools still run with
         terminal declined?          -> the 0.6 deny-boundary go/no-go
  #9     does session/load resume losslessly across a process restart?
         -> the whole resident-process + idle-reap design rests on this
  #10    does cancel leave the session reusable (in-process AND after reload)?
         -> decides whether rotate_grok_session_id can really be deleted

Usage:  python3 bridge/scripts/acp_spike.py
"""
from __future__ import annotations

import asyncio
import itertools
import json
import os
import sys
import tempfile

GROK = os.path.expanduser("~/.grok/bin/grok")

SEEN: dict[str, int] = {}
FINDINGS: dict[str, object] = {}
RAW: list[str] = []


def show(tag: str, obj, limit: int = 2500) -> None:
    txt = json.dumps(obj, indent=2, ensure_ascii=False)
    cut = " …[truncated]" if len(txt) > limit else ""
    print(f"\n{tag}\n{txt[:limit]}{cut}", flush=True)


def banner(s: str) -> None:
    print(f"\n\n{'=' * 72}\n== {s}\n{'=' * 72}", flush=True)


class Acp:
    """One `grok agent stdio` process, driven as an ACP client."""

    def __init__(self, extra_argv: list[str] | None = None):
        self.argv = [GROK, "agent", "stdio"] + (extra_argv or [])
        self.ids = itertools.count(1)
        self.pending: dict[int, asyncio.Future] = {}
        self.proc: asyncio.subprocess.Process | None = None
        self.wlock = asyncio.Lock()
        self.reader_task: asyncio.Task | None = None
        self.text: list[str] = []          # accumulated agent_message_chunk text
        self.tool_names: list[str] = []
        self.permission_options = None
        self.stderr_buf = b""

    async def start(self) -> None:
        print(f"\n$ {' '.join(self.argv)}", flush=True)
        self.proc = await asyncio.create_subprocess_exec(
            *self.argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        self.reader_task = asyncio.create_task(self._reader())

    async def _write(self, msg: dict) -> None:
        async with self.wlock:
            self.proc.stdin.write((json.dumps(msg) + "\n").encode())
            await self.proc.stdin.drain()

    async def call(self, method: str, params: dict, timeout: float = 240):
        mid = next(self.ids)
        fut = asyncio.get_running_loop().create_future()
        self.pending[mid] = fut
        await self._write({"jsonrpc": "2.0", "id": mid, "method": method, "params": params})
        try:
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            self.pending.pop(mid, None)
            return {"__timeout__": True, "method": method}

    async def _reader(self) -> None:
        async for raw in self.proc.stdout:
            line = raw.decode(errors="replace").strip()
            if not line:
                continue
            RAW.append(line)
            try:
                msg = json.loads(line)
            except Exception:
                print("NONJSON:", line[:300], flush=True)
                continue

            if "method" in msg and "id" in msg:
                # NOTE: the real bridge must create_task here (plan 1.2). The spike
                # answers inline only because it never blocks on a human.
                await self._on_request(msg)
            elif "method" in msg:
                self._on_notify(msg)
            elif "id" in msg:
                fut = self.pending.pop(msg["id"], None)
                if fut and not fut.done():
                    fut.set_result(msg.get("result") if "error" not in msg else msg)

    async def _on_request(self, msg: dict) -> None:
        m = msg["method"]
        params = msg.get("params") or {}
        show(f">>> AGENT->CLIENT REQUEST: {m}", msg, limit=3000)
        reply = {"jsonrpc": "2.0", "id": msg["id"], "result": {}}

        if m == "session/request_permission":
            opts = params.get("options") or []
            self.permission_options = opts
            FINDINGS["permission_fired"] = True
            FINDINGS["permission_options"] = opts
            print("!!! #4 request_permission FIRED. options =",
                  json.dumps(opts, ensure_ascii=False), flush=True)
            # opaque optionId: pick BY KIND, never hardcode the string (plan 3.5)
            chosen = next((o.get("optionId") for o in opts
                           if o.get("kind") == "allow_once"), None)
            if chosen is None and opts:
                chosen = opts[0].get("optionId")
            print(f"    -> answering with optionId={chosen!r} (selected by kind)", flush=True)
            reply["result"] = {"outcome": {"outcome": "selected", "optionId": chosen}}

        elif m == "fs/read_text_file":
            try:
                reply["result"] = {"content": open(params["path"]).read()}
            except Exception as e:
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32000, "message": str(e)}}
        elif m == "fs/write_text_file":
            try:
                with open(params["path"], "w") as f:
                    f.write(params.get("content", ""))
            except Exception as e:
                reply = {"jsonrpc": "2.0", "id": msg["id"],
                         "error": {"code": -32000, "message": str(e)}}
        elif m.startswith("terminal/"):
            FINDINGS["terminal_requested_despite_decline"] = m
            print(f"!!! #8 agent asked for {m} even though we declined `terminal`", flush=True)
            reply = {"jsonrpc": "2.0", "id": msg["id"],
                     "error": {"code": -32601, "message": "terminal capability not advertised"}}
        await self._write(reply)

    def _on_notify(self, msg: dict) -> None:
        params = msg.get("params") or {}
        upd = params.get("update") or {}
        tag = upd.get("sessionUpdate") if isinstance(upd, dict) else "?"
        key = f"{msg['method']} / {tag}"
        SEEN[key] = SEEN.get(key, 0) + 1

        if tag == "agent_message_chunk":
            c = upd.get("content") or {}
            if isinstance(c, dict) and c.get("text"):
                self.text.append(c["text"])
        elif tag == "tool_call":
            name = ((upd.get("_meta") or {}).get("x.ai/tool") or {}).get("name") or upd.get("title")
            self.tool_names.append(str(name))
        elif tag == "available_commands_update":
            FINDINGS["commands_fired"] = True
            cmds = upd.get("availableCommands") or upd.get("available_commands") or upd.get("commands")
            FINDINGS["commands_sample"] = cmds[:5] if isinstance(cmds, list) else cmds
            FINDINGS["commands_count"] = len(cmds) if isinstance(cmds, list) else None
            print(f"!!! #3 available_commands_update FIRED "
                  f"({FINDINGS['commands_count']} entries)", flush=True)
            show(f"--- {key}", msg, limit=6000)
            return
        elif tag == "config_option_update":
            FINDINGS.setdefault("config_option_updates", []).append(upd)
            show(f"--- {key}", msg, limit=2500)
            return

        if tag in ("current_mode_update", "plan", "turn_completed", "usage_update"):
            show(f"--- {key}", msg, limit=1800)
        elif SEEN[key] == 1:
            show(f"--- {key} (first occurrence)", msg, limit=900)

    async def prompt(self, sid: str, text: str, timeout: float = 300):
        self.text.clear()
        return await self.call("session/prompt", {
            "sessionId": sid,
            "prompt": [{"type": "text", "text": text}],
        }, timeout=timeout)

    async def stop(self) -> None:
        try:
            self.proc.stdin.close()
        except Exception:
            pass
        try:
            await asyncio.wait_for(self.proc.wait(), 10)
        except asyncio.TimeoutError:
            self.proc.terminate()
            await asyncio.sleep(0.5)
        if self.reader_task:
            self.reader_task.cancel()
        try:
            self.stderr_buf = await asyncio.wait_for(self.proc.stderr.read(), 5)
        except Exception:
            pass


async def handshake(acp: Acp, label: str):
    init = await acp.call("initialize", {
        "protocolVersion": 1,
        "clientCapabilities": {
            "fs": {"readTextFile": True, "writeTextFile": True},
            "terminal": False,
        },
    }, timeout=90)
    show(f"[{label}] INITIALIZE RESULT", init, limit=5000)
    return init


async def main() -> int:
    cwd = tempfile.mkdtemp(prefix="acp-spike-")
    with open(os.path.join(cwd, "hello.txt"), "w") as f:
        f.write("spike marker: BANANA_SENTINEL_42\n")

    # ---------------- process #1 ----------------
    # #7 ANSWERED BEFORE THE RUN: `grok agent stdio` rejects --deny
    #   ("error: unexpected argument '--deny' found"), and the parent `grok agent`
    #   has no --allow/--deny either. There is NO allow/deny policy engine on the
    #   ACP path. So the probe below is repurposed: with no rule possible, does the
    #   approval channel become the ONLY policy point?
    banner("#1/#8  initialize (terminal declined; no deny rules are possible)")
    a = Acp()
    await a.start()
    init = await handshake(a, "proc1")
    if isinstance(init, dict) and not init.get("__timeout__"):
        FINDINGS["protocolVersion"] = init.get("protocolVersion")
        FINDINGS["agentCapabilities"] = init.get("agentCapabilities") or init.get("capabilities")
        FINDINGS["authMethods"] = init.get("authMethods")
    else:
        print("!!! initialize FAILED/timed out -- design is refuted here", flush=True)
        await a.stop()
        return 2

    banner("#2/#11  session/new  (does it take cwd? does it advertise modes/models/configOptions?)")
    new = await a.call("session/new", {"cwd": cwd, "mcpServers": []}, timeout=90)
    show("SESSION/NEW RESULT", new, limit=5000)
    if not isinstance(new, dict) or "sessionId" not in new:
        print("!!! #2 FAILED: no sessionId -- cwd-per-session model is refuted", flush=True)
        await a.stop()
        return 2
    sid = new["sessionId"]
    FINDINGS["session_new_keys"] = sorted(new.keys())
    for k in ("modes", "models", "configOptions", "config_options"):
        if k in new:
            FINDINGS[f"session_new.{k}"] = new[k]

    banner("#3/#4/#5/#6  first prompt -- force a tool call inside the temp cwd")
    res = await a.prompt(sid, "Use your tools to read the file hello.txt in the current "
                              "working directory and tell me the sentinel word it contains.")
    show("PROMPT RESULT", res, limit=2500)
    FINDINGS["prompt_result_keys"] = sorted(res.keys()) if isinstance(res, dict) else str(type(res))
    FINDINGS["turn1_saw_sentinel"] = "BANANA_SENTINEL_42" in "".join(a.text)
    FINDINGS["turn1_tools"] = list(a.tool_names)
    print(f"\n[turn1] tools used: {a.tool_names}")
    print(f"[turn1] agent text ({len(''.join(a.text))} chars): {''.join(a.text)[:400]}")

    banner("#11  can we set model / effort? (config option vs legacy set_model)")
    for method, params in (
        ("session/set_config_option", {"sessionId": sid, "optionId": "model",
                                       "value": "grok-4.5"}),
        ("session/set_model", {"sessionId": sid, "modelId": "grok-4.5"}),
    ):
        r = await a.call(method, params, timeout=45)
        FINDINGS[f"probe::{method}"] = r
        show(f"PROBE {method}", r, limit=1200)

    banner("#7b  no deny rule exists -- does a shell command at least trigger approval?")
    a.tool_names.clear()
    res = await a.prompt(a_sid := sid,
                         "Run the shell command `rm -f /tmp/acp-spike-nonexistent-file` "
                         "using your tools. Report exactly what happened.", timeout=180)
    FINDINGS["deny_probe_text"] = "".join(a.text)[:800]
    FINDINGS["deny_probe_tools"] = list(a.tool_names)
    print(f"\n[deny probe] tools: {a.tool_names}")
    print(f"[deny probe] text: {''.join(a.text)[:600]}")

    banner("#10a  cancel mid-turn, then keep chatting in the SAME process")
    task = asyncio.create_task(a.prompt(
        sid, "Count slowly from 1 to 40, one number per line, thinking carefully "
             "about each one.", timeout=180))
    await asyncio.sleep(6)
    cancel_res = await a.call("session/cancel", {"sessionId": sid}, timeout=45)
    show("SESSION/CANCEL RESULT", cancel_res, limit=1200)
    try:
        show("CANCELLED PROMPT RESULT", await asyncio.wait_for(task, 90), limit=1500)
    except asyncio.TimeoutError:
        print("!!! cancelled prompt never returned a response", flush=True)
        FINDINGS["cancel_prompt_returned"] = False
    else:
        FINDINGS["cancel_prompt_returned"] = True

    res = await a.prompt(sid, "Reply with exactly: STILL_ALIVE", timeout=120)
    FINDINGS["reuse_after_cancel_same_proc"] = "STILL_ALIVE" in "".join(a.text)
    print(f"\n[after cancel, same proc] {''.join(a.text)[:300]}")

    await a.stop()
    if a.stderr_buf.strip():
        print(f"\n[proc1 STDERR]\n{a.stderr_buf.decode(errors='replace')[:2500]}", flush=True)

    # ---------------- process #2: the load test ----------------
    banner("#9/#10b  NEW PROCESS -> session/load -> does context survive?")
    b = Acp()
    await b.start()
    await handshake(b, "proc2")
    load = await b.call("session/load", {"sessionId": sid, "cwd": cwd, "mcpServers": []},
                        timeout=120)
    show("SESSION/LOAD RESULT", load, limit=4000)
    FINDINGS["session_load_ok"] = isinstance(load, dict) and not load.get("__timeout__") \
        and "error" not in load

    if FINDINGS["session_load_ok"]:
        res = await b.prompt(sid, "Without using any tools: what was the sentinel word "
                                  "in hello.txt that I asked you about earlier in this "
                                  "conversation? Reply with just the word.", timeout=150)
        joined = "".join(b.text)
        FINDINGS["load_recalled_context"] = "BANANA_SENTINEL_42" in joined
        print(f"\n[after load] {joined[:400]}")
    else:
        FINDINGS["load_recalled_context"] = False
        print("!!! #9 session/load FAILED -- idle-reap architecture is REFUTED", flush=True)

    await b.stop()
    if b.stderr_buf.strip():
        print(f"\n[proc2 STDERR]\n{b.stderr_buf.decode(errors='replace')[:2500]}", flush=True)

    # ---------------- verdict ----------------
    banner("SUMMARY")
    print(f"#1  protocolVersion negotiated : {FINDINGS.get('protocolVersion')!r}")
    print(f"#1  agentCapabilities          : "
          f"{json.dumps(FINDINGS.get('agentCapabilities'), ensure_ascii=False)[:400]}")
    print(f"#2  session/new keys           : {FINDINGS.get('session_new_keys')}")
    print(f"#3  available_commands_update  : {FINDINGS.get('commands_fired', False)}"
          f"  (count={FINDINGS.get('commands_count')})")
    print(f"#4  request_permission fired   : {FINDINGS.get('permission_fired', False)}")
    print(f"#4  permission options         : "
          f"{json.dumps(FINDINGS.get('permission_options'), ensure_ascii=False)[:500]}")
    print(f"#6  prompt result keys         : {FINDINGS.get('prompt_result_keys')}")
    print(f"#7  deny probe tools/text      : {FINDINGS.get('deny_probe_tools')} / "
          f"{str(FINDINGS.get('deny_probe_text'))[:220]!r}")
    print(f"#8  terminal/* requested?      : "
          f"{FINDINGS.get('terminal_requested_despite_decline', 'no (good)')}")
    print(f"#9  session/load ok            : {FINDINGS.get('session_load_ok')}")
    print(f"#9  context survived reload    : {FINDINGS.get('load_recalled_context')}   <-- GATES IDLE-REAP")
    print(f"#10 cancel -> reusable (same)  : {FINDINGS.get('reuse_after_cancel_same_proc')}")
    print(f"#11 set_config_option probe    : "
          f"{json.dumps(FINDINGS.get('probe::session/set_config_option'), ensure_ascii=False)[:300]}")
    print(f"#11 set_model probe            : "
          f"{json.dumps(FINDINGS.get('probe::session/set_model'), ensure_ascii=False)[:300]}")

    print("\nsessionUpdate variants seen (count / name):")
    for k, v in sorted(SEEN.items(), key=lambda kv: -kv[1]):
        print(f"  {v:6d}  {k}")

    frames = os.path.join(tempfile.gettempdir(), "acp_spike_frames.jsonl")
    with open(frames, "w") as f:
        f.write("\n".join(RAW))
    findings = os.path.join(tempfile.gettempdir(), "acp_spike_findings.json")
    with open(findings, "w") as f:
        json.dump(FINDINGS, f, indent=2, ensure_ascii=False, default=str)
    print(f"\n{len(RAW)} raw frames -> {frames}")
    print(f"findings          -> {findings}")
    print(f"temp cwd (inspect or delete) -> {cwd}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

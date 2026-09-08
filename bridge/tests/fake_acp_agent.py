#!/usr/bin/env python3
"""A deterministic stand-in for `grok agent stdio`.

Real-agent smoke tests cannot reproduce protocol bugs reliably -- output depends
on what the model decides to do, and the interesting failures (a handler
stalling the reader, a crash mid-turn, a malformed line) are not reachable on
demand. This speaks the same ACP dialect the Phase 0 spike captured, on command.

Scenario is chosen with FAKE_ACP_SCENARIO. Invoked exactly like the real binary
(`... agent [-m X] [--reasoning-effort Y] stdio`); extra argv is ignored.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

SCENARIO = os.environ.get("FAKE_ACP_SCENARIO", "basic")
PROTOCOL_VERSION = int(os.environ.get("FAKE_ACP_PROTOCOL_VERSION", "1"))
SESSION_ID = "fake-session-0001"
CANCELLED = threading.Event()
_WRITE_LOCK = threading.Lock()


def send(obj) -> None:
    with _WRITE_LOCK:
        sys.stdout.write(json.dumps(obj) + "\n")
        sys.stdout.flush()


def notify(session_id: str, update: dict, method: str = "session/update") -> None:
    send({"jsonrpc": "2.0", "method": method,
          "params": {"sessionId": session_id, "update": update}})


def chunk(session_id: str, text: str, thought: bool = False) -> None:
    notify(session_id, {
        "sessionUpdate": "agent_thought_chunk" if thought else "agent_message_chunk",
        "content": {"type": "text", "text": text},
    })


def handle_prompt(msg: dict) -> None:
    sid = msg["params"]["sessionId"]
    req_id = msg["id"]

    if SCENARIO == "crash_mid_turn":
        chunk(sid, "starting")
        sys.stderr.write("fatal: simulated agent crash with /Users/secret/path\n")
        sys.stderr.flush()
        os._exit(3)

    if SCENARIO == "malformed_line":
        sys.stdout.write("this is not json\n")
        sys.stdout.flush()
        chunk(sid, "recovered")
        send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        return

    if SCENARIO == "tool_call":
        notify(sid, {
            "sessionUpdate": "tool_call", "toolCallId": "call-1",
            "title": "run_terminal_command",
            "rawInput": {"command": "echo hi"},
            "_meta": {"x.ai/tool": {"name": "run_terminal_command", "kind": "execute",
                                    "label": "Run Command", "read_only": False}},
        })
        notify(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "call-1",
                     "status": "in_progress", "content": []})
        notify(sid, {"sessionUpdate": "tool_call_update", "toolCallId": "call-1",
                     "status": "completed", "content": [],
                     "rawOutput": {"exit_code": 0, "truncated": False}})
        chunk(sid, "done")
        send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        return

    if SCENARIO == "reader_not_blocked":
        # Ask the client to read a file, then IMMEDIATELY -- without waiting for
        # its answer -- send more notifications and the terminal response. A
        # client whose reader awaited its own request handler could not deliver
        # these until the handler finished; that is the bug this catches.
        send({"jsonrpc": "2.0", "id": 9001, "method": "fs/read_text_file",
              "params": {"path": os.environ["FAKE_ACP_READ_PATH"]}})
        chunk(sid, "sent-while-request-outstanding")
        send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        return

    if SCENARIO == "fs_roundtrip":
        send({"jsonrpc": "2.0", "id": 9002, "method": "fs/read_text_file",
              "params": {"path": os.environ["FAKE_ACP_READ_PATH"]}})
        send({"jsonrpc": "2.0", "id": 9003, "method": "fs/write_text_file",
              "params": {"path": os.environ.get("FAKE_ACP_WRITE_PATH", "out.txt"),
                         "content": "written by agent"}})
        # Answers arrive as responses to those ids; echo whatever we get back.
        for _ in range(2):
            line = sys.stdin.readline()
            if not line:
                break
            reply = json.loads(line)
            chunk(sid, "FSRESULT:" + json.dumps(reply.get("result", reply.get("error"))))
        send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        return

    if SCENARIO == "slow_turn":
        # Runs on its own thread so the main loop keeps reading stdin -- a real
        # agent accepts session/cancel *while* a turn is in flight, and a fake
        # that blocks its reader could not exercise that at all.
        def run() -> None:
            for _ in range(600):
                time.sleep(0.05)
                if CANCELLED.is_set():
                    send({"jsonrpc": "2.0", "id": req_id,
                          "result": {"stopReason": "cancelled"}})
                    return
            send({"jsonrpc": "2.0", "id": req_id, "result": {"stopReason": "end_turn"}})
        threading.Thread(target=run, daemon=True).start()
        return

    # basic
    chunk(sid, "Hel", thought=True)
    chunk(sid, "Hello ")
    chunk(sid, "world")
    notify(sid, {"sessionUpdate": "available_commands_update", "availableCommands": [
        {"name": "compact", "description": "Compress history", "input": {"hint": "opt"}},
        {"name": "context", "description": "Show usage", "input": None},
    ]})
    notify(sid, {"sessionUpdate": "turn_completed", "stop_reason": "end_turn",
                 "elapsed_ms": 5}, method="_x.ai/session_notification")
    send({"jsonrpc": "2.0", "id": req_id, "result": {
        "stopReason": "end_turn",
        "_meta": {"usage": {"inputTokens": 10, "outputTokens": 3, "totalTokens": 13,
                            "costUsdTicks": 42}},
    }})


def main() -> int:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue

        method = msg.get("method")
        if method == "initialize":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "protocolVersion": PROTOCOL_VERSION,
                "agentCapabilities": {"loadSession": True,
                                      "promptCapabilities": {"image": False}},
            }})
        elif method == "session/new":
            send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                "sessionId": SESSION_ID,
                "models": {"currentModelId": "fake-model",
                           "availableModels": [{"modelId": "fake-model", "name": "Fake"}]},
            }})
        elif method == "session/load":
            if SCENARIO == "load_unsupported":
                send({"jsonrpc": "2.0", "id": msg["id"],
                      "error": {"code": -32601, "message": "Method not found"}})
            else:
                send({"jsonrpc": "2.0", "id": msg["id"], "result": {
                    "sessionId": msg["params"].get("sessionId", SESSION_ID),
                    "models": {"currentModelId": "fake-model"},
                }})
        elif method == "session/prompt":
            handle_prompt(msg)
        elif method == "session/cancel":
            # A notification in ACP: no id, no response. The real agent ignores
            # the request form entirely, so the fake must not accept it either --
            # otherwise the tests would pass against a cancel that does nothing.
            CANCELLED.set()
        elif method == "session/set_model":
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "result": {"_meta": {"model": {"Ok": msg["params"]["modelId"]}}}})
        elif "id" in msg and method is None:
            continue          # a response to one of our own requests
        elif "id" in msg:
            send({"jsonrpc": "2.0", "id": msg["id"],
                  "error": {"code": -32601, "message": f"unknown method {method}"}})
    return 0


if __name__ == "__main__":
    sys.exit(main())

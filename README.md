# Grok Remote

Drive the `grok` CLI on your Mac from your iPhone. A small FastAPI **Bridge**
runs a resident `grok agent stdio` process per session and speaks the
[Agent Client Protocol](https://agentclientprotocol.com) (ACP) to it; a SwiftUI
**iOS client** connects over a Cloudflare Tunnel and gives you a real remote
coding surface — streamed replies, tool cards, Markdown, a project picker with
live git status, a command palette, and full chat-history recovery.

```
  iPhone (SwiftUI)  ──wss──►  Cloudflare Tunnel  ──►  Bridge (FastAPI, 127.0.0.1:8899)
                                                          │  JSON-RPC / stdio
                                                          ▼
                                                grok agent stdio  (one per session)
```

## Why it is shaped this way

- **Resident ACP session, not `grok -p` per turn.** The Bridge keeps one
  `grok agent stdio` process alive per session and drives it with `session/new`,
  `session/load`, `session/prompt`, `session/cancel`. `session/load` restores a
  session's full context across a process restart, which is what makes idle
  reaping and effort-switch-by-restart safe.
- **The OS sandbox is the security boundary.** ACP on grok exposes no
  allow/deny engine and never delegates permission prompts to the client, so
  there is no phone-side approval sheet. Instead the Bridge spawns every agent
  under a custom Seatbelt profile (`GROK_SANDBOX=grok-remote`) that denies
  `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.grok/auth`, keychains and `~/.config`, and
  confines writes to the session cwd + `/tmp`. grok's sandbox is fail-open, so
  the Bridge **fail-closes**: if the profile is not confirmed active it refuses
  to start an agent.
- **One credential, bound to its origin.** There is no Cloudflare Access layer;
  authentication is a single per-device bearer token issued by pairing. The
  token is stored in the Keychain bound to the server origin — change the host
  and the old token is dropped, not replayed against the new address.
- **Durable, resumable transport.** `/v2/chat` is a session-scoped WebSocket
  backed by a per-session event journal. A phone that drops mid-turn (iOS
  suspends backgrounded apps routinely) reconnects and replays exactly what it
  missed via `afterSeq`; a fresh entry tails, because the transcript is loaded
  separately from grok's own history.

## Repository layout

```
bridge/                 FastAPI Bridge (Python, uv-managed)
  grok_bridge/
    acp.py              resident ACP client: process, reader/demux, fs capability
    protocol.py         ACP session/update  ->  Bridge event schema
    session_manager.py  agent pool, idle reaping, event fan-out + journal
    sandbox.py          ensure_profile() + fail-closed verify_enforced()
    grok_disk.py        adapter over grok's private on-disk format (history, models)
    workspace.py        repo discovery + lazy git status (worktree-aware)
    commands.py         command registry: acp / bridge / shell kinds
    db.py               SQLite: devices, sessions, session_events, pairing tokens
    app.py              HTTP + WebSocket endpoints
  tests/                unit + fake-ACP-agent integration (see below)
  scripts/
    smoke.py            end-to-end check against a live Bridge
    acp_spike*.py       the Phase 0 dialect spikes (kept as evidence)
ios/
  GrokRemote/           SwiftUI app (Design system, Views, Markdown renderer)
  scripts/verify.sh     design-token lint + simulator build gate
install.sh              launchd + tunnel installer, prints a pairing QR
```

## Running the Bridge

```bash
cd bridge
uv sync
uv run uvicorn grok_bridge.app:app --host 127.0.0.1 --port 8899
```

`GET /health` reports `status`, whether the `grok` binary is reachable, and the
auth-failure counter. The installer wires this up as a KeepAlive launchd service
plus a dedicated Cloudflare Tunnel:

```bash
./install.sh grok-remote.example.com
```

It prints a one-time pairing QR (10-minute TTL) for the iOS app to scan.

## API surface

| Method | Path | Purpose |
|---|---|---|
| POST | `/v1/pair` | redeem a pairing token → device bearer (404 when none pending) |
| DELETE | `/v1/devices/{id}` | revoke a device (self only) |
| GET | `/v1/sessions` | rich session list (title, model, branch, counts) |
| POST | `/v1/sessions` | create a session for a cwd |
| PATCH/DELETE | `/v1/sessions/{id}` | rename / delete |
| GET | `/v1/sessions/{id}/messages` | paginated transcript from grok's history |
| GET | `/v1/sessions/{id}/commands` | merged command list (acp / bridge-native) |
| POST | `/v1/sessions/{id}/command` | run a bridge/shell command |
| GET | `/v1/repos`, `POST /v1/repos/status` | repo list, then lazy git status |
| GET | `/v1/fs/list` | directory browse (denied paths flagged, not hidden) |
| GET | `/v1/models`, `/v1/recent-dirs` | model catalogue, recently used dirs |
| WS | `/v2/chat` | resumable session stream (hello / afterSeq replay) |
| WS | `/v1/chat` | legacy per-turn socket (kept until the app fully migrates) |

`/v2/chat` frames: `hello` → `hello.ack` (+ replay), then `prompt` / `cancel` /
`command` / `ping` inbound and the journalled event stream outbound
(`message.delta` / `message.thought` / `tool.*` / `message.usage` /
`message.done` / `commands.available` / …).

## Testing

Three layers (plan v2 §5.1):

```bash
cd bridge
uv run pytest                    # unit + fake-ACP-agent integration (68 tests)
uv run python scripts/smoke.py   # end-to-end against a running Bridge
```

- **unit** — path/cwd validation (incl. symlink escape), the ACP→event mapping
  against real captured frames, history parsing (partial final line,
  `<user_query>` unwrap, env-preamble drop), auth, and DB migration.
- **fake-ACP-agent integration** (`tests/fake_acp_agent.py`) — the reader never
  deadlocks, the state machine refuses correctly, journal replay, crash
  recovery, `session/load`, cancel-as-notification.
- **real smoke** — pairing, a turn that triggers a tool, model switch, cancel
  (`message.done{stopReason:"cancelled"}`), and `/v2/chat` resume.

iOS:

```bash
ios/scripts/verify.sh            # design-token lint, then a simulator build
```

## Notes / limitations

- **No background push yet.** In the foreground the socket is live; on return
  from background the app reconnects and replays via `afterSeq`. A locked-phone
  "a turn needs your attention now" notification would require APNs (an Apple
  developer account + `.p8`); until that is configured the app does not pretend
  to notify in the background.
- **The sandbox confines subprocesses, not the agent's own HTTP.** Network
  restriction covers child processes; the agent's own model/API calls are not
  sandboxed, so this does not claim to prevent exfiltration.

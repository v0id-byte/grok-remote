# Grok Remote

Grok Remote is an independent macOS Bridge and iOS client for driving the
Grok CLI on a Mac from an iPhone. The Bridge keeps a resident ACP session per
conversation, while the phone receives streamed messages, tool activity,
Markdown, history, repository status, and model controls.

This project is not affiliated with or endorsed by xAI, Grok, Cloudflare, or
Apple. You need your own Grok CLI installation and account.

## Quick start

The supported first release is macOS. The installer uses Homebrew to install
uv, cloudflared, and qrencode when they are missing. It does not install or
authenticate the Grok CLI for you.

For a stable public address, use a hostname in a Cloudflare-managed zone:

~~~~bash
git clone https://github.com/v0id-byte/grok-remote.git
cd grok-remote
./install.sh install --hostname grok-remote.example.com
~~~~

For a temporary demonstration URL:

~~~~bash
./install.sh install --quick-tunnel
~~~~

An auditable remote bootstrap is also available after the public repository is
published:

~~~~bash
curl -fsSL https://raw.githubusercontent.com/v0id-byte/grok-remote/main/install.sh \
  -o /tmp/grok-remote-install.sh
bash /tmp/grok-remote-install.sh install \
  --hostname grok-remote.example.com
~~~~

The installer will:

1. Check macOS and the Grok CLI.
2. Clone or reuse the source checkout.
3. Install the Bridge environment with uv.
4. Start the Bridge as a KeepAlive launchd service on 127.0.0.1:8899.
5. Log in to Cloudflare if necessary, create or reuse the named Tunnel, and
   create the DNS route.
6. Start the Tunnel as a separate launchd service.
7. Verify local and public /health endpoints.
8. Print a one-time QR pairing payload for the iOS app.

The QR payload contains the Bridge URL and a pairing token that expires after
ten minutes. The token is exchanged once for an iPhone device credential and
is not written to the repository.

## Installer lifecycle

~~~~bash
./install.sh install --hostname grok-remote.example.com
./install.sh install --quick-tunnel
./install.sh doctor
./install.sh doctor --json
./install.sh status
./install.sh update
~~~~

Installation is idempotent for the two Grok Remote launchd labels. It does not
stop or rewrite unrelated Cloudflare tunnels. Runtime state is kept outside
the checkout:

- source: ~/grok-remote
- database and uploads: ~/Library/Application Support/GrokRemote
- logs: ~/Library/Logs/GrokRemote
- launchd agents: ~/Library/LaunchAgents
- named Tunnel configuration: ~/.cloudflared/grok-remote-config.yml

The installer copies an existing Bridge database to the new state directory
when one is present. It never deletes the old database.

The update command refuses to operate on a dirty checkout, refreshes the source
and Python environment, restarts the managed services, and checks local health
before reporting success.

## Cloudflare modes

The default named Tunnel flow opens the Cloudflare browser login when the
local certificate is absent, creates or reuses the Tunnel named
grok-remote, writes an ingress mapping to the local Bridge, and creates the
DNS route for the supplied hostname.

Quick Tunnel mode is for demos only. Its hostname is temporary and can change
after a restart, so it should not be used for a long-lived iOS pairing.

The Tunnel is only a transport layer. This project deliberately does not put
a Cloudflare Access service token in the iOS app. The Bridge's short-lived
pairing token and per-device bearer token are the authentication boundary.

## Running the Bridge manually

~~~~bash
cd bridge
uv sync
uv run grok-remote-bridge serve --host 127.0.0.1 --port 8899
~~~~

The Bridge CLI also provides:

~~~~bash
uv run grok-remote-bridge pair-token
uv run grok-remote-bridge doctor
uv run grok-remote-bridge status
~~~~

Environment variables:

- GROK_BRIDGE_DIR: runtime database and upload directory.
- GROK_BIN: path to the Grok executable.
- GROK_BRIDGE_ALLOWED_ROOTS: colon-separated workspace roots.
- GROK_BRIDGE_ALLOW_UNSANDBOXED=1: local development override only; never use
  this for a public Tunnel.

## Architecture

~~~~text
iPhone (SwiftUI)
    | HTTPS + WebSocket through Cloudflare Tunnel
    v
Bridge (FastAPI, bound to 127.0.0.1:8899)
    | one resident process per session
    v
grok agent stdio (ACP)
~~~~

The Bridge owns the resumable transport and event journal. The /v2/chat
endpoint replays missed events after an iOS reconnect, while Grok's own
on-disk history remains the source for transcript discovery.

Important endpoints:

| Method | Path | Purpose |
| --- | --- | --- |
| GET | /health | local health and Grok reachability |
| POST | /v1/pair | redeem a one-time pairing token |
| GET | /v1/sessions | Bridge and discovered Grok sessions |
| GET | /v1/sessions/{id}/messages | transcript history |
| GET | /v1/repos | workspace repositories |
| WS | /v2/chat | resumable session stream |

## Security boundaries and limitations

- The Bridge listens on loopback; Cloudflare carries traffic to it.
- Every remote request except pairing requires a per-device bearer credential.
- Pairing tokens expire, are single-use, and are rate limited.
- Agent processes use the grok-remote Seatbelt profile and the Bridge
  fail-closes when enforcement is not confirmed.
- Sensitive paths such as SSH keys, cloud credentials, keychains, and
  configuration directories are denied by the Bridge policy.
- The agent's own model/API HTTP traffic is not confined by the child-process
  network setting. This project does not claim to prevent model-side
  exfiltration.
- Grok must read its own authentication file, so that credential is an
  explicitly documented residual risk.
- ACP does not provide a phone-side approval dialog. The Bridge is intended
  for a single trusted owner and its configured workspace boundary.
- Background APNs notifications are not implemented. The app reconnects and
  replays missed events when it returns to the foreground.

Do not expose the Bridge with a raw port forward. Use HTTPS through the
managed Tunnel and keep pairing codes private.

## iOS client

The client targets iOS 17 and is provided as a SwiftUI Xcode project.

Source build:

1. Open ios/GrokRemote.xcodeproj in Xcode.
2. Select your Apple development team for local signing.
3. Select an iPhone or an iOS Simulator.
4. Build and run.
5. Scan the QR printed by the Mac installer.

The repository does not contain an Apple signing identity. A developer who
wants to distribute builds should create an App Store Connect app record for
the bundle identifier, archive with their own team, upload the build, and
invite testers through TestFlight. A future release can automate that upload
after signing credentials and release ownership are deliberately configured.

## Development and verification

Bridge tests and package build:

~~~~bash
cd bridge
uv sync --locked
uv run pytest -q
uv build
~~~~

Installer static checks:

~~~~bash
bash -n install.sh
./install.sh install --quick-tunnel --dry-run --source-dir "$PWD"
~~~~

iOS checks:

~~~~bash
ios/scripts/verify.sh lint
ios/scripts/verify.sh build
~~~~

The iOS build uses a generic Simulator destination by default. Set
GROK_REMOTE_SIMULATOR_DESTINATION when a particular local destination is
needed.

## License

The project code, installer, and documentation are licensed under the Apache
License 2.0. See LICENSE and NOTICE.

The license covers this repository's original work; it does not grant rights
to proprietary Grok/xAI services, Cloudflare services, Apple platforms, or
their trademarks.

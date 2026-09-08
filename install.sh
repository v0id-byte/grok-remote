#!/usr/bin/env bash
# One-shot installer for grok-remote-bridge (plan v1.3 §5).
#
# What this does automatically:
#   1. Checks grok / cloudflared / uv are present
#   2. Installs the Bridge as a launchd service (KeepAlive)
#   3. Creates a dedicated named Cloudflare Tunnel + DNS route for this
#      service only (its own config file, isolated from whatever other
#      tunnels already run on this Mac -- doesn't touch them)
#   4. Reminds you to disable macOS auto-sleep on power (can't be scripted
#      reliably; System Settings > Battery/Lock Screen)
#   5. Prints a one-time pairing QR code for the iOS app
#
# Authentication is the Bridge's own per-device token, nothing at the edge:
# there is deliberately no Cloudflare Access application or Service Token, so
# no edge secret is ever written into the app. The tunnel just carries traffic;
# the token in the pairing QR is the only credential. See plan v2 §0.1.
#
# Usage: ./install.sh <hostname, e.g. grok-remote.void1211.com>

set -euo pipefail

HOSTNAME="${1:?Usage: ./install.sh <hostname>, e.g. ./install.sh grok-remote.void1211.com}"
TUNNEL_NAME="grok-remote"
BRIDGE_DIR="$HOME/grok-remote-bridge"
BRIDGE_PORT=8899
PLIST="$HOME/Library/LaunchAgents/com.v0id.grok-remote-bridge.plist"
TUNNEL_CONFIG="$HOME/.cloudflared/${TUNNEL_NAME}-config.yml"
TUNNEL_PLIST="$HOME/Library/LaunchAgents/com.v0id.grok-remote-tunnel.plist"

echo "==> Checking prerequisites"
command -v grok >/dev/null || { echo "grok CLI not found (expected at ~/.grok/bin/grok on PATH)"; exit 1; }
command -v cloudflared >/dev/null || { echo "cloudflared not found (brew install cloudflared)"; exit 1; }
command -v uv >/dev/null || { echo "uv not found (https://docs.astral.sh/uv/)"; exit 1; }

echo "==> Installing Bridge Python dependencies"
cd "$BRIDGE_DIR/bridge"
uv sync

BRIDGE_PYTHON="$BRIDGE_DIR/bridge/.venv/bin/python"

echo "==> Writing launchd service for the Bridge daemon"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.v0id.grok-remote-bridge</string>
    <key>ProgramArguments</key>
    <array>
        <string>$BRIDGE_PYTHON</string>
        <string>$BRIDGE_DIR/bridge/main.py</string>
    </array>
    <key>WorkingDirectory</key><string>$BRIDGE_DIR/bridge</string>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$BRIDGE_DIR/bridge.log</string>
    <key>StandardErrorPath</key><string>$BRIDGE_DIR/bridge.err.log</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>LLAMA_TRAIN_API_KEY</key><string>${LLAMA_TRAIN_API_KEY:-}</string>
    </dict>
</dict>
</plist>
EOF
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load "$PLIST"
echo "    Bridge daemon loaded (launchctl list | grep grok-remote-bridge to check)"

echo "==> Creating dedicated Cloudflare Tunnel '$TUNNEL_NAME' for $HOSTNAME"
if ! cloudflared tunnel list 2>/dev/null | grep -q " $TUNNEL_NAME "; then
    cloudflared tunnel create "$TUNNEL_NAME"
else
    echo "    Tunnel '$TUNNEL_NAME' already exists, reusing it"
fi
TUNNEL_UUID=$(cloudflared tunnel list 2>/dev/null | awk -v n="$TUNNEL_NAME" '$2==n{print $1}')

cat > "$TUNNEL_CONFIG" <<EOF
tunnel: $TUNNEL_UUID
credentials-file: $HOME/.cloudflared/${TUNNEL_UUID}.json
ingress:
  - hostname: $HOSTNAME
    service: http://localhost:$BRIDGE_PORT
  - service: http_status:404
EOF

cloudflared tunnel route dns "$TUNNEL_NAME" "$HOSTNAME" 2>&1 || echo "    (DNS route may already exist, continuing)"

echo "==> Writing launchd service for the tunnel (isolated from your other tunnels)"
cat > "$TUNNEL_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key><string>com.v0id.grok-remote-tunnel</string>
    <key>ProgramArguments</key>
    <array>
        <string>$(command -v cloudflared)</string>
        <string>tunnel</string>
        <string>--config</string>
        <string>$TUNNEL_CONFIG</string>
        <string>run</string>
    </array>
    <key>RunAtLoad</key><true/>
    <key>KeepAlive</key><true/>
    <key>StandardOutPath</key><string>$BRIDGE_DIR/tunnel.log</string>
    <key>StandardErrorPath</key><string>$BRIDGE_DIR/tunnel.err.log</string>
</dict>
</plist>
EOF
launchctl unload "$TUNNEL_PLIST" 2>/dev/null || true
launchctl load "$TUNNEL_PLIST"
echo "    Tunnel daemon loaded"

echo
echo "==> Reminder: prevent this Mac from sleeping while you're out"
echo "    System Settings > Battery / Lock Screen > disable auto-sleep on power."
echo "    (Not scripted here on purpose -- see plan v1.3's launchd/caffeinate note.)"

echo
echo "==> Generating one-time pairing QR code"
PAIRING_TOKEN=$("$BRIDGE_PYTHON" "$BRIDGE_DIR/bridge/main.py" pair-token)
PAIRING_PAYLOAD="{\"url\":\"https://$HOSTNAME\",\"pairing_token\":\"$PAIRING_TOKEN\"}"
if command -v qrencode >/dev/null; then
    qrencode -t ANSIUTF8 "$PAIRING_PAYLOAD"
else
    echo "    (brew install qrencode for a scannable QR code -- printing the raw payload instead)"
fi
echo "    $PAIRING_PAYLOAD"
echo
echo "    (QR camera scanning isn't wired up in the app yet -- v1 pairing screen"
echo "     takes these two values by hand:)"
echo "    Bridge 地址:  https://$HOSTNAME"
echo "    配对码:       $PAIRING_TOKEN"
echo
echo "Done. Open the GrokRemote app and enter the values above to pair."

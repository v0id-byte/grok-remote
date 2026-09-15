#!/usr/bin/env bash
# Grok Remote bootstrap and lifecycle installer.
set -euo pipefail

REPO_URL="https://github.com/v0id-byte/grok-remote.git"
SOURCE_FALLBACK="$HOME/grok-remote"
STATE_DIR="$HOME/Library/Application Support/GrokRemote"
LOG_DIR="$HOME/Library/Logs/GrokRemote"
LAUNCH_DIR="$HOME/Library/LaunchAgents"
BRIDGE_LABEL="com.v0id.grok-remote-bridge"
TUNNEL_LABEL="com.v0id.grok-remote-tunnel"
BRIDGE_PORT=8899
TUNNEL_NAME="grok-remote"
ACTION="install"
HOSTNAME=""
SOURCE_DIR=""
GROK_BIN=""
QUICK=0
YES=0
DRY_RUN=0
BRIDGE_PYTHON=""
CLOUDFLARED=""
PUBLIC_URL=""

if [ "$#" -gt 0 ]; then ACTION="$1"; shift; fi

usage() {
    cat <<'EOF'
Usage:
  ./install.sh install [--hostname HOST] [--quick-tunnel] [--source-dir DIR]
                       [--tunnel-name NAME] [--yes] [--dry-run]
  ./install.sh doctor [--json]
  ./install.sh status [--json]
  ./install.sh update
EOF
}

die() { echo "ERROR: $*" >&2; exit 1; }
note() { echo "==> $*"; }
has() { command -v "$1" >/dev/null 2>&1; }

confirm() {
    [ "$YES" = 1 ] && return 0
    [ -t 0 ] || die "confirmation required: $*. Re-run with --yes"
    printf "%s [y/N] " "$*"
    read -r answer
    [ "$answer" = y ] || [ "$answer" = Y ]
}

xml_escape() {
    printf '%s' "$1" |
        sed -e 's/&/\&amp;/g' -e 's/</\&lt;/g' -e 's/>/\&gt;/g' \
            -e 's/"/\&quot;/g' -e "s/'/\&apos;/g"
}

parse_install_args() {
    while [ "$#" -gt 0 ]; do
        case "$1" in
            --hostname) [ "$#" -ge 2 ] || die "--hostname needs a value"; HOSTNAME="$2"; shift 2 ;;
            --quick-tunnel) QUICK=1; shift ;;
            --source-dir) [ "$#" -ge 2 ] || die "--source-dir needs a value"; SOURCE_DIR="$2"; shift 2 ;;
            --tunnel-name) [ "$#" -ge 2 ] || die "--tunnel-name needs a value"; TUNNEL_NAME="$2"; shift 2 ;;
            --yes) YES=1; shift ;;
            --dry-run) DRY_RUN=1; shift ;;
            -h|--help) usage; exit 0 ;;
            *) die "unknown install option: $1" ;;
        esac
    done
    if [ "$QUICK" = 0 ] && [ -z "$HOSTNAME" ]; then
        [ -t 0 ] || die "named Tunnel installation requires --hostname"
        printf "Stable public hostname: "
        read -r HOSTNAME
    fi
    if [ "$QUICK" = 0 ]; then
        [[ "$HOSTNAME" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*[A-Za-z0-9]$ ]] ||
            die "invalid hostname: $HOSTNAME"
    fi
    [[ "$TUNNEL_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9_-]*$ ]] ||
        die "invalid tunnel name: $TUNNEL_NAME"
}

check_macos() {
    [ "$(uname -s)" = Darwin ] || die "macOS is required for this release"
    export PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"
}

resolve_source() {
    if [ -n "$SOURCE_DIR" ]; then
        SOURCE_DIR="$(cd "$SOURCE_DIR" 2>/dev/null && pwd)" ||
            die "source directory does not exist"
    elif [ -f "$(dirname "$0")/bridge/pyproject.toml" ]; then
        SOURCE_DIR="$(cd "$(dirname "$0")" && pwd)"
    elif [ -f "$SOURCE_FALLBACK/bridge/pyproject.toml" ]; then
        SOURCE_DIR="$SOURCE_FALLBACK"
    else
        has git || die "git is required to clone the repository"
        note "Cloning $REPO_URL into $SOURCE_FALLBACK"
        git clone --branch main "$REPO_URL" "$SOURCE_FALLBACK"
        SOURCE_DIR="$SOURCE_FALLBACK"
    fi
    [ -f "$SOURCE_DIR/bridge/pyproject.toml" ] ||
        die "not a Grok Remote checkout: $SOURCE_DIR"
}

resolve_brew() {
    BREW="$(command -v brew 2>/dev/null || true)"
    if [ -z "$BREW" ] && [ -x /opt/homebrew/bin/brew ]; then
        BREW=/opt/homebrew/bin/brew
    fi
    if [ -z "$BREW" ] && [ -x /usr/local/bin/brew ]; then
        BREW=/usr/local/bin/brew
    fi
}

ensure_tools() {
    resolve_brew
    if [ -z "$BREW" ]; then
        confirm "Homebrew is missing. Install it from brew.sh?" ||
            die "Homebrew is required for managed dependencies"
        has curl || die "curl is required to bootstrap Homebrew"
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
        resolve_brew
    fi
    [ -n "$BREW" ] || die "Homebrew was not found after installation"
    for formula in uv cloudflared qrencode; do
        if ! has "$formula"; then
            note "Installing $formula"
            "$BREW" install "$formula"
        fi
    done
    CLOUDFLARED="$(command -v cloudflared)"
}

resolve_grok() {
    GROK_BIN="$HOME/.grok/bin/grok"
    [ -x "$GROK_BIN" ] || GROK_BIN="$(command -v grok 2>/dev/null || true)"
    [ -n "$GROK_BIN" ] && [ -x "$GROK_BIN" ] ||
        die "Grok CLI was not found; install and authenticate it first"
}

prepare_state() {
    mkdir -p "$STATE_DIR/uploads" "$LOG_DIR" "$LAUNCH_DIR" "$HOME/.cloudflared"
    chmod 700 "$STATE_DIR" "$STATE_DIR/uploads" "$HOME/.cloudflared"
    if [ ! -e "$STATE_DIR/bridge.db" ]; then
        for old_db in "$SOURCE_DIR/bridge/bridge.db" \
                      "$SOURCE_DIR/bridge.db" \
                      "$HOME/grok-remote-bridge/bridge.db" \
                      "$HOME/grok-remote-bridge/bridge/bridge.db"; do
            if [ -f "$old_db" ]; then
                note "Migrating existing Bridge database"
                cp -p "$old_db" "$STATE_DIR/bridge.db"
                chmod 600 "$STATE_DIR/bridge.db"
                break
            fi
        done
    fi
}

sync_bridge() {
    cd "$SOURCE_DIR/bridge"
    uv sync --locked
    BRIDGE_PYTHON="$SOURCE_DIR/bridge/.venv/bin/python"
    [ -x "$BRIDGE_PYTHON" ] || die "uv did not create the Bridge environment"
}

write_bridge_plist() {
    local python cwd state grok logs home
    python="$(xml_escape "$BRIDGE_PYTHON")"
    cwd="$(xml_escape "$SOURCE_DIR/bridge")"
    state="$(xml_escape "$STATE_DIR")"
    grok="$(xml_escape "$GROK_BIN")"
    logs="$(xml_escape "$LOG_DIR")"
    home="$(xml_escape "$HOME")"
    umask 077
    cat > "$LAUNCH_DIR/$BRIDGE_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
<key>Label</key><string>$BRIDGE_LABEL</string>
<key>ProgramArguments</key><array>
<string>$python</string><string>-m</string><string>grok_bridge.cli</string><string>serve</string>
<string>--host</string><string>127.0.0.1</string><string>--port</string><string>$BRIDGE_PORT</string>
</array>
<key>WorkingDirectory</key><string>$cwd</string>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>
<key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>$logs/bridge.log</string>
<key>StandardErrorPath</key><string>$logs/bridge.err.log</string>
<key>EnvironmentVariables</key><dict>
<key>GROK_BRIDGE_DIR</key><string>$state</string>
<key>GROK_BRIDGE_ALLOWED_ROOTS</key><string>$home</string>
<key>GROK_BIN</key><string>$grok</string>
<key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:/usr/bin:/bin</string>
</dict>
</dict></plist>
EOF
    chmod 600 "$LAUNCH_DIR/$BRIDGE_LABEL.plist"
}

reload_agent() {
    local label="$1"
    local user_id
    user_id="$(id -u)"
    launchctl bootout "gui/$user_id/$label" 2>/dev/null || true
    launchctl bootstrap "gui/$user_id" "$LAUNCH_DIR/$label.plist"
}

wait_http() {
    local url="$1"
    local tries=30
    [ "$#" -lt 2 ] || tries="$2"
    local i
    for i in $(seq 1 "$tries"); do
        curl --fail --silent --max-time 3 "$url" >/dev/null 2>&1 && return 0
        sleep 1
    done
    return 1
}

tunnel_id() {
    "$CLOUDFLARED" tunnel list --name "$TUNNEL_NAME" --output json 2>/dev/null |
        "$BRIDGE_PYTHON" -c '
import json, sys
try:
    raw = json.load(sys.stdin)
except Exception:
    raise SystemExit(0)
items = raw if isinstance(raw, list) else raw.get("result", raw.get("tunnels", []))
for item in items:
    if item.get("name") == sys.argv[1]:
        print(item.get("id") or item.get("uuid") or "")
        break
' "$TUNNEL_NAME"
}

write_tunnel_config() {
    local id="$1"
    local config="$HOME/.cloudflared/$TUNNEL_NAME-config.yml"
    local tmp="$config.tmp"
    local credentials="$HOME/.cloudflared/$id.json"
    [ -f "$credentials" ] || die "Tunnel credentials missing: $credentials"
    [ ! -f "$config" ] || cp -p "$config" "$config.bak"
    umask 077
    cat > "$tmp" <<EOF
tunnel: $id
credentials-file: $credentials
ingress:
  - hostname: $HOSTNAME
    service: http://127.0.0.1:$BRIDGE_PORT
  - service: http_status:404
EOF
    mv "$tmp" "$config"
    chmod 600 "$config"
    "$CLOUDFLARED" tunnel --config "$config" ingress validate
}

write_tunnel_plist() {
    local mode="$1"
    local cloudflared logs config
    cloudflared="$(xml_escape "$CLOUDFLARED")"
    logs="$(xml_escape "$LOG_DIR")"
    config="$(xml_escape "$HOME/.cloudflared/$TUNNEL_NAME-config.yml")"
    umask 077
    if [ "$mode" = named ]; then
        cat > "$LAUNCH_DIR/$TUNNEL_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>$TUNNEL_LABEL</string>
<key>ProgramArguments</key><array><string>$cloudflared</string><string>tunnel</string><string>--no-autoupdate</string><string>--config</string><string>$config</string><string>run</string><string>$TUNNEL_NAME</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>$logs/tunnel.log</string><key>StandardErrorPath</key><string>$logs/tunnel.err.log</string>
</dict></plist>
EOF
    else
        cat > "$LAUNCH_DIR/$TUNNEL_LABEL.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict><key>Label</key><string>$TUNNEL_LABEL</string>
<key>ProgramArguments</key><array><string>$cloudflared</string><string>tunnel</string><string>--no-autoupdate</string><string>--url</string><string>http://127.0.0.1:$BRIDGE_PORT</string></array>
<key>RunAtLoad</key><true/><key>KeepAlive</key><true/><key>ThrottleInterval</key><integer>10</integer>
<key>StandardOutPath</key><string>$logs/tunnel.log</string><key>StandardErrorPath</key><string>$logs/tunnel.err.log</string>
</dict></plist>
EOF
    fi
    chmod 600 "$LAUNCH_DIR/$TUNNEL_LABEL.plist"
}

install_tunnel() {
    if [ "$QUICK" = 1 ]; then
        write_tunnel_plist quick
        reload_agent "$TUNNEL_LABEL"
        for i in $(seq 1 30); do
            PUBLIC_URL="$(grep -Eo 'https://[A-Za-z0-9.-]+trycloudflare.com' \
                "$LOG_DIR/tunnel.log" "$LOG_DIR/tunnel.err.log" 2>/dev/null | tail -1 || true)"
            [ -n "$PUBLIC_URL" ] && return 0
            sleep 1
        done
        die "Quick Tunnel did not publish a URL; inspect $LOG_DIR/tunnel.err.log"
    fi

    [ -f "$HOME/.cloudflared/cert.pem" ] ||
        "$CLOUDFLARED" tunnel login
    local id
    id="$(tunnel_id)"
    if [ -z "$id" ]; then
        "$CLOUDFLARED" tunnel create "$TUNNEL_NAME"
        id="$(tunnel_id)"
    fi
    [ -n "$id" ] || die "could not resolve the Tunnel ID"
    write_tunnel_config "$id"
    if ! "$CLOUDFLARED" tunnel route dns "$TUNNEL_NAME" "$HOSTNAME"; then
        note "DNS route already exists or needs manual review; public health will verify it"
    fi
    write_tunnel_plist named
    reload_agent "$TUNNEL_LABEL"
    PUBLIC_URL="https://$HOSTNAME"
}

pair() {
    wait_http "http://127.0.0.1:$BRIDGE_PORT/health" 30 ||
        die "Bridge did not become healthy; inspect $LOG_DIR/bridge.err.log"
    wait_http "$PUBLIC_URL/health" 45 ||
        die "public Tunnel did not reach the Bridge; inspect $LOG_DIR/tunnel.err.log"
    local token payload
    token="$(GROK_BRIDGE_DIR="$STATE_DIR" GROK_BIN="$GROK_BIN" \
        "$BRIDGE_PYTHON" -m grok_bridge.cli pair-token)"
    payload="$("$BRIDGE_PYTHON" -c 'import json,sys; print(json.dumps({"url":sys.argv[1],"pairing_token":sys.argv[2]},separators=(",",":")))' "$PUBLIC_URL" "$token")"
    echo
    note "One-time iOS pairing code (expires after 10 minutes)"
    qrencode -t ANSIUTF8 "$payload" 2>/dev/null || true
    echo "$payload"
    echo "Public Bridge: $PUBLIC_URL"
}

install_command() {
    check_macos
    resolve_source
    if [ "$DRY_RUN" = 1 ]; then
        echo "Would install from: $SOURCE_DIR"
        echo "Would store state in: $STATE_DIR"
        [ "$QUICK" = 1 ] && echo "Would use a temporary Quick Tunnel" ||
            echo "Would expose: https://$HOSTNAME"
        return 0
    fi
    resolve_grok
    ensure_tools
    sync_bridge
    prepare_state
    write_bridge_plist
    reload_agent "$BRIDGE_LABEL"
    install_tunnel
    {
        printf 'source_dir=%s\n' "$SOURCE_DIR"
        printf 'hostname=%s\n' "$HOSTNAME"
        printf 'tunnel_name=%s\n' "$TUNNEL_NAME"
        printf 'public_url=%s\n' "$PUBLIC_URL"
    } > "$STATE_DIR/install.conf"
    chmod 600 "$STATE_DIR/install.conf"
    pair
    note "Installation complete"
}

status_command() {
    check_macos
    if [ "$#" -ge 1 ] && [ "$1" = --json ]; then
        local installed_source="$SOURCE_FALLBACK"
        if [ -f "$STATE_DIR/install.conf" ]; then
            installed_source="$(sed -n 's/^source_dir=//p' "$STATE_DIR/install.conf")"
        fi
        if [ -x "$installed_source/bridge/.venv/bin/python" ]; then
            GROK_BRIDGE_DIR="$STATE_DIR" \
                "$installed_source/bridge/.venv/bin/python" -m grok_bridge.cli status --json || true
        else
            echo '{"reachable":false,"error":"Bridge environment is not installed"}'
        fi
        return 0
    fi
    local user_id
    user_id="$(id -u)"
    launchctl print "gui/$user_id/$BRIDGE_LABEL" 2>/dev/null | sed -n '1,10p' || echo "Bridge: not loaded"
    launchctl print "gui/$user_id/$TUNNEL_LABEL" 2>/dev/null | sed -n '1,10p' || echo "Tunnel: not loaded"
    [ ! -f "$STATE_DIR/install.conf" ] || sed -n 's/^/  /p' "$STATE_DIR/install.conf"
    curl --fail --silent "http://127.0.0.1:$BRIDGE_PORT/health" || echo "Bridge health: unavailable"
    echo
}

doctor_command() {
    check_macos
    resolve_source
    local python="$SOURCE_DIR/bridge/.venv/bin/python"
    [ -x "$python" ] || die "Bridge environment missing; run install first"
    if [ -n "$GROK_BIN" ]; then
        GROK_BRIDGE_DIR="$STATE_DIR" GROK_BIN="$GROK_BIN" \
            "$python" -m grok_bridge.cli doctor "$@"
    else
        GROK_BRIDGE_DIR="$STATE_DIR" \
            "$python" -m grok_bridge.cli doctor "$@"
    fi
}

update_command() {
    check_macos
    resolve_source
    [ -z "$(git -C "$SOURCE_DIR" status --porcelain)" ] ||
        die "refusing to update a checkout with local changes: $SOURCE_DIR"
    git -C "$SOURCE_DIR" pull --ff-only
    cd "$SOURCE_DIR/bridge"
    uv sync --locked
    local user_id
    user_id="$(id -u)"
    launchctl kickstart -k "gui/$user_id/$BRIDGE_LABEL"
    launchctl kickstart -k "gui/$user_id/$TUNNEL_LABEL" 2>/dev/null || true
    wait_http "http://127.0.0.1:$BRIDGE_PORT/health" 30 ||
        die "Bridge did not recover after update"
    note "Update complete"
}

case "$ACTION" in
    install) parse_install_args "$@"; install_command ;;
    doctor) doctor_command "$@" ;;
    status) status_command "$@" ;;
    update) update_command ;;
    -h|--help) usage ;;
    *) die "unknown command: $ACTION" ;;
esac

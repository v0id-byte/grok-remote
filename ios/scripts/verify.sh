#!/usr/bin/env bash
#
# Acceptance gate for the iOS client (plan v2 §5.1).
#
#   verify.sh lint    design-token discipline (grep gates)
#   verify.sh build   xcodebuild for the simulator
#   verify.sh         both, in that order
#
# The lint gates come straight from the Pianotuner design system: the point of
# porting the tokens is undone the moment a view reaches for a raw colour, a
# rounded system font, an emoji, or a hand-typed spacing number. These check the
# view layer only -- DesignSystem.swift is where the literals are *allowed* to
# live, so it is deliberately excluded.

set -euo pipefail
cd "$(dirname "$0")/.."

SCHEME="GrokRemote"
SIM="${GROK_REMOTE_SIMULATOR_DESTINATION:-generic/platform=iOS Simulator}"
# View code, minus the design system itself (which defines the tokens).
LINT_PATHS=(GrokRemote/Views GrokRemote/Markdown)

fail() { echo "FAIL: $1" >&2; exit 1; }

lint() {
  echo "→ lint (design tokens)"

  if grep -rnE 'Color\.(black|white|gray)' "${LINT_PATHS[@]}"; then
    fail "raw Color.black/.white/.gray -- use DS.Color tokens"
  fi

  if grep -rnE 'design:[[:space:]]*\.rounded' "${LINT_PATHS[@]}"; then
    fail "design: .rounded -- Pianotuner uses the system font, mono only for digits"
  fi

  # SF Symbols named "...fill" are banned: the system is monochrome + .light.
  if grep -rnE 'systemName:[[:space:]]*"[a-z.]+\.fill"' "${LINT_PATHS[@]}"; then
    fail "a .fill SF Symbol -- icons are monochrome outlines"
  fi

  # Emoji anywhere in the view layer. BSD grep has no -P, so lean on perl,
  # which is always present on macOS.
  if find "${LINT_PATHS[@]}" -name '*.swift' -print0 \
     | xargs -0 perl -ne 'exit 1 if /[\x{1F000}-\x{1FAFF}\x{2600}-\x{27BF}\x{2B00}-\x{2BFF}]/' ; then
    :
  else
    fail "emoji in the view layer"
  fi

  echo "  ok"
}

build() {
  echo "→ build ($SCHEME)"
  xcodebuild -project GrokRemote.xcodeproj -scheme "$SCHEME" \
    -destination "$SIM" -derivedDataPath build \
    CODE_SIGNING_ALLOWED=NO build \
    >/tmp/grokremote-build.log 2>&1 \
    || {
      grep -iE 'error|fatal|failed' /tmp/grokremote-build.log | tail -120 || true
      tail -30 /tmp/grokremote-build.log
      fail "xcodebuild"
    }
  echo "  BUILD SUCCEEDED"
}

case "${1:-all}" in
  lint)  lint ;;
  build) build ;;
  all)   lint; build ;;
  *)     echo "usage: verify.sh [lint|build|all]" >&2; exit 2 ;;
esac
echo "PASS"

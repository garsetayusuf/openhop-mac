#!/bin/bash
# openhop for Mac — clean removal. Run on your Mac:  bash uninstall-macos.sh
# Your OpenCode data/sessions/auth are untouched. WARP is left installed.
set -u

BRAND="openhop"
DEST="$HOME/Library/Application Support/$BRAND"
BIN_DIR="$HOME/.local/bin"
PLIST_DIR="$HOME/Library/LaunchAgents"

ok(){ echo "  ✓ $*"; }
echo ""
echo "  removing openhop for Mac..."
echo ""

for job in guard keeper; do
  launchctl bootout "gui/$UID/com.openhop.$job" >/dev/null 2>&1 && ok "com.openhop.$job stopped" || true
  rm -f "$PLIST_DIR/com.openhop.$job.plist"
done

# Restore the opencode entry to the real binary if it still exists.
if [ -x "$HOME/.opencode/bin/opencode" ]; then
  printf '#!/usr/bin/env bash\nexec "%s" "$@"\n' "$HOME/.opencode/bin/opencode" > "$BIN_DIR/opencode"
  chmod +x "$BIN_DIR/opencode"
  ok "opencode entry -> back to stock binary"
fi

rm -f "$BIN_DIR/openhop-hop" "$BIN_DIR/openhop-doctor"
rm -rf "$DEST" && ok "scripts + guard state removed"
defaults delete com.openhop.controlcenter openhop.setupCompleted >/dev/null 2>&1 || true
defaults delete com.openhop.controlcenter.test openhop.setupCompleted >/dev/null 2>&1 || true
defaults delete com.openhop.controlcenter openhop.setupIdentity >/dev/null 2>&1 || true
defaults delete com.openhop.controlcenter.test openhop.setupIdentity >/dev/null 2>&1 || true
ok "first-run setup flag cleared"

if command -v warp-cli >/dev/null 2>&1; then
  warp-cli --accept-tos disconnect >/dev/null 2>&1 && ok "warp tunnel disconnected (package left installed)" || true
fi

echo ""
ok "gone. OpenCode itself + your sessions are untouched."
echo "Delete /Applications/OpenHop.app by hand if you built it (README step 7)."
echo ""

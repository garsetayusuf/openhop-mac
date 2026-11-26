#!/bin/bash
# zen guard — macOS. Watches OpenCode's log for the free-usage wall,
# gets a new WARP address, and sends 'continue' to the chats cut off.
# Runs as a launchd agent (com.openhop.guard). launchd replaces systemd;
# launchctl replaces systemctl. No iptables kill-switch on macOS.
# How it runs: src/zen_guard.py main() takes no CLI arguments — it reads
# its paths from src/ouc_common.py, which this port made darwin-aware
# (state under ~/Library/Application Support/openhop, OpenCode log under
# ~/Library/Application Support/opencode/log/). This script only sets the
# environment and launches it with the venv Python from that src/ folder
# so its sibling imports (ouc_common, auto_resume, ...) resolve.
set -u
DEST="$HOME/Library/Application Support/openhop"
GUARD_LOG="$DEST/guard/guard.log"
mkdir -p "$DEST/guard"
WARP="${OUC_WARP_BIN:-}"
if [ -z "$WARP" ]; then
  command -v warp-cli >/dev/null 2>&1 && WARP="$(command -v warp-cli)"
fi
[ -n "$WARP" ] || { echo "warp-cli not found" >> "$GUARD_LOG"; exit 1; }
export OUC_WARP_BIN="$WARP"
export OUC_MACOS=1
if [ -f "$DEST/src/zen_guard.py" ] && [ -x "$DEST/venv/bin/python" ]; then
  cd "$DEST/src" || exit 1
  exec "$DEST/venv/bin/python" zen_guard.py >>"$GUARD_LOG" 2>&1
else
  echo "zen_guard.py or venv python not found under $DEST — reinstall with install-macos.sh" >> "$GUARD_LOG"
  exit 1
fi

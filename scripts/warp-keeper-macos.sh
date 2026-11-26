#!/bin/bash
# warp keeper — macOS. Keeps the Cloudflare WARP tunnel connected.
# Runs as a launchd agent (com.openhop.keeper), every 45 s tick.
# Linux version used systemd + iptables; macOS has neither: no
# kill-switch is installed, proxy mode fails closed instead.
set -u
WARP="${OUC_WARP_BIN:-}"
if [ -z "$WARP" ]; then
  command -v warp-cli >/dev/null 2>&1 && WARP="$(command -v warp-cli)"
fi
[ -n "$WARP" ] || { echo "warp-cli not found"; exit 1; }
w(){ "$WARP" --accept-tos "$@" >/dev/null 2>&1 && return 0; "$WARP" "$@" >/dev/null 2>&1; }
wo(){ "$WARP" --accept-tos "$@" 2>/dev/null && return 0; "$WARP" "$@" 2>/dev/null; }
MODE="${OUC_WARP_MODE:-proxy}"
while true; do
  if ! wo status | head -1 | grep -q Connected; then
    if [ "$MODE" = "warp" ]; then w mode warp; else w mode proxy; w proxy port 40000; fi
    w connect
  fi
  sleep 45
done

#!/bin/bash
# doctor — macOS. Tells you what's alive and what needs attention.
# launchctl replaces systemctl; pf/iptables checks are dropped
# (no kill-switch is installed on macOS by design).
set -u
BRAND="openhop"
DEST="$HOME/Library/Application Support/$BRAND"
WARP="${OUC_WARP_BIN:-}"
if [ -z "$WARP" ]; then command -v warp-cli >/dev/null 2>&1 && WARP="$(command -v warp-cli)"; fi
wo(){ "$WARP" --accept-tos "$@" 2>/dev/null && return 0; "$WARP" "$@" 2>/dev/null; }
ok(){ echo "  ✓ $*"; }
warn(){ echo "  ! $*"; }
bad(){ echo "  ✗ $*"; }
echo ""; echo "  ============================="; echo "  openhop — doctor (macOS)"; echo "  ============================="; echo ""
launchctl list com.openhop.keeper >/dev/null 2>&1 && ok "keeper launchd job (com.openhop.keeper)" || bad "keeper down — launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.openhop.keeper.plist"
launchctl list com.openhop.guard >/dev/null 2>&1 && ok "guard launchd job (com.openhop.guard)" || bad "guard down — launchctl bootstrap gui/$UID ~/Library/LaunchAgents/com.openhop.guard.plist"
if [ -n "$WARP" ]; then
  ST=$(wo status | head -1); echo "$ST" | grep -q Connected && ok "warp tunnel: connected ($ST)" || bad "warp not connected — open the WARP app once, then re-run this doctor"
else bad "warp-cli not found — install Cloudflare WARP (brew install --cask cloudflare-warp)"; fi
ok "egress mode: only OpenCode (local proxy 127.0.0.1:40000)"
STATE="$DEST/guard/state.json"
if [ -f "$STATE" ]; then
  INFO=$(python3 -c 'import json,sys,time
try:
 d=json.load(open(sys.argv[1])); h=d.get("hops",0); bl=len([x for x in d.get("blacklist",[]) if x]); pause=float(d.get("backoff_until",0) or 0)
 msg="guard state: %s rotations, %s used-up /64s" % (h, bl)
 if pause>time.time(): msg += " (paused a few minutes: addresses used up)"
 print(msg)
except Exception as e: print("guard state unreadable")' "$STATE" 2>/dev/null)
  ok "$INFO"
else warn "guard state file not found yet — no rotation has happened"; fi
V6=""; WARP_STATUS=""
for attempt in 1 2 3; do
  CF_TRACE=$(timeout 12 curl -x http://127.0.0.1:40000 -s -m 10 https://cloudflare.com/cdn-cgi/trace 2>/dev/null || true)
  V6=$(echo "$CF_TRACE" | grep "^ip=" | head -1 | cut -d= -f2)
  WARP_STATUS=$(echo "$CF_TRACE" | grep "^warp=" | head -1 | cut -d= -f2)
  [ -n "$V6" ] && break
  [ "$attempt" -lt 3 ] && sleep 2
done
if [ "$WARP_STATUS" = "on" ] || [ "$WARP_STATUS" = "plus" ]; then ok "egress is cloudflare (warp verified): $V6";
elif [ -n "$V6" ]; then case "$V6" in 2606:*|2a09:*|104.28.*|8.29.*) ok "egress is cloudflare (warp): $V6" ;; *) bad "egress is NOT warp ($V6)" ;; esac;
else bad "no egress through warp — tunnel down, check the keeper"; fi
if [ -f "$HOME/.local/bin/opencode" ] && grep -q "zen-lane-macos" "$HOME/.local/bin/opencode" 2>/dev/null; then ok "opencode entry -> macOS lane"; else warn "opencode entry doesn't point at the macOS lane — reinstall or check ~/.local/bin/opencode"; fi
for c in openhop openhop-hop openhop-doctor; do
  [ -e "$HOME/.local/bin/$c" ] && ok "command: $c" || warn "missing command: $c (re-run install-macos.sh)"
done
echo ""; echo "  all good = work. something red = run the command next to it."; echo ""

#!/bin/bash
# zen hop — macOS. Manual identity rotation: delete the WARP registration,
# register fresh, reconnect. You land on a new IPv6 /64 (= new quota bucket).
# launchd replaces systemd here: the keeper is paused with launchctl,
# not systemctl. No iptables kill-switch on macOS.
set -u
WARP="${OUC_WARP_BIN:-}"
if [ -z "$WARP" ]; then
  command -v warp-cli >/dev/null 2>&1 && WARP="$(command -v warp-cli)"
fi
[ -n "$WARP" ] || { echo "warp-cli not found"; exit 1; }
w(){ "$WARP" --accept-tos "$@" >/dev/null 2>&1 && return 0; "$WARP" "$@" >/dev/null 2>&1; }
wo(){ "$WARP" --accept-tos "$@" 2>/dev/null && return 0; "$WARP" "$@" 2>/dev/null; }
restore(){ launchctl kickstart -k "gui/$UID/com.openhop.keeper" >/dev/null 2>&1 || true; }
trap restore EXIT
MODE="${OUC_WARP_MODE:-proxy}"
cur_v6(){
  if [ "$MODE" = "warp" ]; then timeout 10 curl -s -m 8 https://api64.ipify.org 2>/dev/null;
  else timeout 10 curl -x socks5h://127.0.0.1:40000 -s -m 8 https://api64.ipify.org 2>/dev/null; fi
}
OLD_V6=$(cur_v6); echo "old /64: ${OLD_V6:-?}"
launchctl bootout "gui/$UID/com.openhop.keeper" >/dev/null 2>&1 || true
echo "-- rotating (keeper paused) --"
w disconnect
w registration delete
NEW=0
for i in 1 2 3 4 5; do w registration new && { NEW=1; break; }; echo "  attempt $i failed"; sleep 8; done
[ "$NEW" = 1 ] || { echo "-- registration failed — keeper will keep retrying --"; exit 1; }
if [ "$MODE" = "warp" ]; then w mode warp; else w mode proxy; w proxy port 40000; fi
w connect
for i in $(seq 1 20); do sleep 3; wo status | head -1 | grep -q Connected && break; done
NEW_V6=$(cur_v6); echo "new /64: ${NEW_V6:-?}"
if [ "$NEW_V6" != "$OLD_V6" ] && [ -n "$NEW_V6" ]; then
  for db in "$HOME/Library/Application Support/ai.opencode.desktop/drafts.sqlite" "$HOME/.config/ai.opencode.desktop/drafts.sqlite" "$HOME/.local/share/opencode/drafts.sqlite"; do
    [ -f "$db" ] && sqlite3 "$db" "DELETE FROM state WHERE key IN ('free-limit', 'go-upsell') OR key LIKE '%free%limit%' OR key LIKE '%quota%exceeded%' OR key LIKE '%upsell%';" >/dev/null 2>&1 || true
  done
  echo "== FRESH /64 — type 'continue' in your chat (or re-send your message) =="
  exit 0
fi
echo "== cloudflare handed back the same block — run me again =="
exit 1

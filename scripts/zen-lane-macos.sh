#!/bin/bash
# zen lane — macOS. The per-launch trick, macOS edition.
# Linux used unshare -rm to bind-mount a fake /etc/hosts (pin opencode.ai
# to one random Cloudflare edge) and /etc/machine-id per process.
# macOS has NO per-process mount namespace, so those two steps are
# deliberately dropped here (see README section 9). What remains and
# does work on macOS: point OpenCode at WARP's local proxy
# (http://127.0.0.1:40000, or the openhop proxy on 127.0.0.1:41987),
# so only OpenCode goes through WARP. If the proxy is down, OpenCode
# fails to connect instead of leaking your real IP.
set -u
REAL="${OUC_REAL_BIN:-$HOME/.opencode/bin/opencode}"
if [ ! -x "$REAL" ]; then
  for c in "$HOME/.local/bin/opencode" /opt/homebrew/bin/opencode /usr/local/bin/opencode; do
    [ -x "$c" ] && [ "$c" != "$0" ] && REAL="$c" && break
  done
fi
[ -x "$REAL" ] || { echo "opencode binary not found — set OUC_REAL_BIN or reinstall"; exit 1; }
if [ "${OUC_WARP_MODE:-proxy}" != "warp" ]; then
  PROXY_TARGET="http://127.0.0.1:40000"
  if (echo > /dev/tcp/127.0.0.1/41987) 2>/dev/null; then PROXY_TARGET="http://127.0.0.1:41987"; fi
  CFG="$HOME/Library/Application Support/openhop/lane/egress_mode.json"
  if [ -f "$CFG" ]; then
    MODE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("mode","warp"))' "$CFG" 2>/dev/null || echo "warp")
    if [ "$MODE" = "custom" ]; then
      CUSTOM=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("custom_proxy",""))' "$CFG" 2>/dev/null || true)
      CTYPE=$(python3 -c 'import json,sys;print(json.load(open(sys.argv[1])).get("custom_type","socks5"))' "$CFG" 2>/dev/null || echo "socks5")
      if [ -n "$CUSTOM" ]; then
        case "$CTYPE" in http*) PROXY_TARGET="http://$CUSTOM" ;; *) PROXY_TARGET="socks5://$CUSTOM" ;; esac
      fi
    fi
  fi
  export HTTPS_PROXY="$PROXY_TARGET" HTTP_PROXY="$PROXY_TARGET" https_proxy="$PROXY_TARGET" http_proxy="$PROXY_TARGET"
  export NO_PROXY="127.0.0.1,localhost,::1" no_proxy="127.0.0.1,localhost,::1"
  unset ALL_PROXY all_proxy
fi
exec "$REAL" "$@"

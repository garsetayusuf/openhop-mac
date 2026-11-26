#!/bin/bash
# openhop for Mac — installer (Apple silicon / macOS 13+, arm64)
# Run on your Mac, from the unzipped openhop-mac folder:  bash install-macos.sh
# See README.md step 3. Do NOT run this on Linux or Windows.
set -u

BRAND="openhop"
DEST="$HOME/Library/Application Support/$BRAND"
BIN_DIR="$HOME/.local/bin"
PLIST_DIR="$HOME/Library/LaunchAgents"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)"
SOURCE_VERSION="$(grep -m1 '^VERSION = ' "$SRC/src/ouc_common.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/' || true)"

say(){ echo "  $*"; }
ok(){ echo "  ✓ $*"; }
bad(){ echo "  ✗ $*"; }
die(){ bad "$*"; exit 1; }

echo ""
echo "  ==================================================="
echo "  openhop for Mac (Apple silicon / macOS 13+, arm64)"
echo "  based on openhop ${SOURCE_VERSION:-unknown} (commit 5e8111d) — MIT"
echo "  ==================================================="
if [ -f "$DEST/installed-version" ] && [ -d "$DEST/venv" ] && [ -x "$BIN_DIR/openhop" ]; then
  say "already installed: $(cat "$DEST/installed-version" 2>/dev/null || echo unknown)"
else
  say "no existing installation detected"
fi
echo ""

# ---------------------------------------------------------------- platform
[ "$(uname -s)" = "Darwin" ] || die "this installer is macOS only"
[ "$(uname -m)" = "arm64" ] || die "not arm64 — run this in a native (non-Rosetta) Terminal"
command -v python3 >/dev/null 2>&1 || die "python3 required — install it first: brew install python@3.12"
command -v curl >/dev/null 2>&1 || die "curl required"
[ -d "$SRC/src" ] && [ -d "$SRC/scripts" ] || die "run me from the unzipped openhop-mac folder (src/ and scripts/ not found)"
SW_MAJOR=$(sw_vers -productVersion | cut -d. -f1)
[ "${SW_MAJOR:-0}" -ge 13 ] 2>/dev/null || die "macOS 13 or newer is required (MenuBarExtra)"

say "Third-party prerequisites:"
for tool in python3 curl opencode warp-cli; do
  p="$(command -v "$tool" 2>/dev/null || true)"
  if [ -z "$p" ] && [ "$tool" = opencode ] && [ -x "$HOME/.opencode/bin/opencode" ]; then p="$HOME/.opencode/bin/opencode"; fi
  if [ -z "$p" ] && [ "$tool" = warp-cli ] && [ -x /usr/local/bin/warp-cli ]; then p=/usr/local/bin/warp-cli; fi
  if [ -n "$p" ]; then say "$tool: $p"; else bad "$tool: MISSING"; fi
done
echo ""

# ---------------------------------------------------------------- opencode CLI
OC_REAL=""
if [ -x "$HOME/.opencode/bin/opencode" ]; then
  OC_REAL="$HOME/.opencode/bin/opencode"
elif command -v opencode >/dev/null 2>&1; then
  OC_REAL="$(command -v opencode)"
fi
if [ -z "$OC_REAL" ]; then
  die "opencode CLI not found — install it from https://opencode.ai (or: brew install --cask opencode-desktop), then re-run"
else
  ok "opencode found: $OC_REAL"
fi

# ---------------------------------------------------------------- warp-cli (macOS WARP app)
WARP=""
if command -v warp-cli >/dev/null 2>&1; then
  WARP="$(command -v warp-cli)"
fi
[ -n "$WARP" ] || die "warp-cli not found — install Cloudflare WARP first: brew install --cask cloudflare-warp (then open the app once and accept its terms)"
ok "warp-cli found: $WARP"

# ---------------------------------------------------------------- venv + deps
mkdir -p "$DEST" "$BIN_DIR" "$PLIST_DIR" "$DEST/logs"
if [ ! -x "$DEST/venv/bin/python" ]; then
  say "creating venv at $DEST/venv"
  python3 -m venv "$DEST/venv" || die "venv creation failed"
fi
"$DEST/venv/bin/python" -m pip install --quiet --upgrade pip 2>/dev/null || true
"$DEST/venv/bin/python" -m pip install --quiet -r "$SRC/requirements-macos.txt" || die "pip install failed — see requirements-macos.txt"
ok "python deps installed in $DEST/venv"

# ---------------------------------------------------------------- copy source + scripts
rm -rf "$DEST/src" "$DEST/scripts" "$DEST/launchd"
cp -R "$SRC/src" "$DEST/src"
cp -R "$SRC/scripts" "$DEST/scripts"
cp -R "$SRC/launchd" "$DEST/launchd" 2>/dev/null || true
cp "$SRC/LICENSE" "$DEST/LICENSE" 2>/dev/null || true
chmod +x "$DEST/scripts/"*-macos.sh 2>/dev/null || true
ok "source copied to $DEST"

# ---------------------------------------------------------------- warp: register + isolated proxy mode
say "registering warp + isolated proxy mode (port 40000)"
"$WARP" --accept-tos registration new >/dev/null 2>&1 || true
if "$WARP" --accept-tos mode proxy >/dev/null 2>&1; then
  "$WARP" --accept-tos proxy port 40000 >/dev/null 2>&1 || true
  ok "warp mode: proxy (port 40000) — only OpenCode goes through WARP"
else
  die "warp-cli rejected 'mode proxy' — refusing to switch the whole PC through WARP. Check WARP settings and re-run."
fi
"$WARP" --accept-tos connect >/dev/null 2>&1 || true
sleep 3

# ---------------------------------------------------------------- launchd jobs (keeper + guard)
for job in keeper guard; do
  PLIST_SRC="$SRC/launchd/com.openhop.$job.plist"
  PLIST_DST="$PLIST_DIR/com.openhop.$job.plist"
  [ -f "$PLIST_SRC" ] || die "missing $PLIST_SRC"
  # Substitute the real home directory and install paths into the plist.
  sed -e "s|__HOME__|$HOME|g" -e "s|__DEST__|$DEST|g" -e "s|__WARP__|$WARP|g" "$PLIST_SRC" > "$PLIST_DST"
  plutil -lint "$PLIST_DST" >/dev/null || die "plist failed validation: $PLIST_DST"
  launchctl bootout "gui/$UID/com.openhop.$job" >/dev/null 2>&1 || true
  loaded=0
  for attempt in 1 2 3; do
    if launchctl bootstrap "gui/$UID" "$PLIST_DST" >/dev/null 2>&1; then loaded=1; break; fi
    [ "$attempt" -lt 3 ] && sleep 2
  done
  [ "$loaded" = 1 ] || die "launchctl bootstrap failed for com.openhop.$job"
  ok "launchd job loaded: com.openhop.$job"
done

# ---------------------------------------------------------------- shims in ~/.local/bin
printf '#!/usr/bin/env bash\nexport OUC_REAL_BIN="%s"\nexec bash "%s/scripts/zen-lane-macos.sh" "$@"\n' "$OC_REAL" "$DEST" > "$BIN_DIR/opencode"
chmod +x "$BIN_DIR/opencode"
rm -f "$BIN_DIR/openhop-macos-hop" "$BIN_DIR/openhop-macos-doctor"
ln -sf "$DEST/scripts/zen-hop-macos.sh" "$BIN_DIR/openhop-hop"
ln -sf "$DEST/scripts/doctor-macos.sh" "$BIN_DIR/openhop-doctor"
cat > "$BIN_DIR/openhop" <<'SHIM'
#!/usr/bin/env bash
# OpenHop CLI — installed by install-macos.sh / the DMG stepper.
cmd="${1:-open}"; [ "$#" -gt 0 ] && shift || true
case "$cmd" in
  doctor) exec "$HOME/.local/bin/openhop-doctor" "$@" ;;
  hop|rotate) exec "$HOME/.local/bin/openhop-hop" "$@" ;;
  status)
    echo "OpenHop status"
    /usr/local/bin/warp-cli status 2>/dev/null | head -2 || true
    for job in keeper guard; do
      launchctl print "gui/$(id -u)/com.openhop.$job" >/dev/null 2>&1 \
        && echo "$job: running" || echo "$job: stopped"
    done
    line=""
    for try in 1 2; do
      line=$("$HOME/.local/bin/openhop-doctor" 2>/dev/null | grep -E 'egress is|no egress' | tail -1 || true)
      echo "$line" | grep -q 'egress is' && break
      [ "$try" = 1 ] && sleep 3
    done
    echo "$line"
    ;;
  start|stop|connect|disconnect)
    open -a OpenHop
    item="Connect"; [ "$cmd" = stop ] || [ "$cmd" = disconnect ] && item="Disconnect"
    osascript -e "tell application \"System Events\" to tell process \"OpenHop\" to click menu item \"$item\" of menu 1 of menu bar item 1 of menu bar 2" >/dev/null 2>&1 || true
    ;;
  quit)
    osascript -e 'tell application "System Events" to tell process "OpenHop" to click menu bar item 1 of menu bar 2' >/dev/null 2>&1 || true
    osascript -e 'tell application "System Events" to tell process "OpenHop" to click menu item "Quit OpenHop" of menu 1 of menu bar item 1 of menu bar 2' >/dev/null 2>&1 || true
    ;;
  open|"") exec open -a OpenHop ;;
  *) echo "usage: openhop [open|doctor|hop|status|start|stop|quit]"; exit 2 ;;
esac
SHIM
chmod +x "$BIN_DIR/openhop"
ok "opencode entry -> lane (proxy mode, port 40000)"
ok "commands: openhop (doctor/hop/status/start/stop/quit), openhop-hop, openhop-doctor"

# ---------------------------------------------------------------- verify egress through the proxy
say "checking that OpenCode's traffic leaves through WARP..."
CF_TRACE=$(timeout 12 curl -x http://127.0.0.1:40000 -s -m 10 https://cloudflare.com/cdn-cgi/trace 2>/dev/null || true)
V6=$(echo "$CF_TRACE" | grep "^ip=" | cut -d= -f2)
WARP_STATUS=$(echo "$CF_TRACE" | grep "^warp=" | cut -d= -f2)
if [ "$WARP_STATUS" = "on" ] || [ "$WARP_STATUS" = "plus" ]; then
  ok "egress is cloudflare (warp verified): $V6"
elif [ -n "$V6" ]; then
  say "egress IP: $V6 — warp flag not 'on'; give the keeper a minute, then run openhop-doctor"
else
  say "egress is not warp yet — keeper is still setting up; give it a minute, then run openhop-doctor"
fi

case ":$PATH:" in
  *":$BIN_DIR:"*) ok "PATH has ~/.local/bin" ;;
  *) say "add this to ~/.zprofile, then reopen Terminal:"
     say "    export PATH=\"\$HOME/.local/bin:\$PATH\"" ;;
esac

printf '%s\n' "${SOURCE_VERSION:-unknown}" > "$DEST/installed-version"
ok "install marker: $DEST/installed-version ($SOURCE_VERSION)"

echo ""
ok "DONE. Close and reopen Terminal, then just run:  opencode"
say "Free limit while you work? The guard gets a new IP and sends 'continue' by itself."
say "Health check any time:  openhop-doctor   -   fresh identity now:  openhop-hop"
echo ""

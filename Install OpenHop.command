#!/bin/bash
# Guided installer shipped inside OpenHop.dmg.
# Every step asks before changing anything; skipping a step never aborts the rest.
set -u

DIR="$(cd "$(dirname "$0")" && pwd)"
PAYLOAD="$DIR/payload"
DEST="$HOME/Library/Application Support/openhop"
SRC_VERSION="$(grep -m1 '^VERSION = ' "$PAYLOAD/src/ouc_common.py" 2>/dev/null | sed 's/.*"\(.*\)".*/\1/' || true)"
INSTALLED_VERSION="$(cat "$DEST/installed-version" 2>/dev/null || true)"
say(){ printf '  %s\n' "$*"; }
step(){ printf '\n  [%s] %s\n' "$1" "$2"; }
confirm(){
  label="$1"; default="${2:-n}"
  if [ "$default" = y ]; then
    read -r -p "  $label [Y/n] " a || true
  else
    read -r -p "  $label [y/N] " a || true
  fi
  [ -z "$a" ] && a="$default"
  case "$a" in y|Y|yes|YES|Yes) return 0 ;; *) return 1 ;; esac
}
skipped=""

echo ""
echo "  ============================================="
echo "  OpenHop for macOS — guided install"
echo "  ============================================="

step 1/5 "Check OpenCode"
OC=""
[ -x "$HOME/.opencode/bin/opencode" ] && OC="$HOME/.opencode/bin/opencode"
if [ -z "$OC" ] && command -v opencode >/dev/null 2>&1; then OC="$(command -v opencode)"; fi
if [ -n "$OC" ]; then
  say "found: $OC — nothing to do"
elif confirm "Install OpenCode with the official installer?" n; then
  if curl -fsSL https://opencode.ai/install | bash; then
    [ -x "$HOME/.opencode/bin/opencode" ] && OC="$HOME/.opencode/bin/opencode"
    [ -n "$OC" ] && say "installed: $OC" || { say "installer finished but the CLI was not found"; skipped="$skipped OpenCode"; }
  else
    say "install failed — get it from https://opencode.ai"
    skipped="$skipped OpenCode"
  fi
else
  say "skipped — install OpenCode later from https://opencode.ai"
  skipped="$skipped OpenCode"
fi

step 2/5 "Check Cloudflare WARP"
WARP="$(command -v warp-cli 2>/dev/null || true)"
if [ -z "$WARP" ] && [ -x /usr/local/bin/warp-cli ]; then WARP=/usr/local/bin/warp-cli; fi
if [ -n "$WARP" ]; then
  say "found: $WARP — nothing to do"
elif confirm "Install Cloudflare WARP with Homebrew?" n; then
  if command -v brew >/dev/null 2>&1; then
    if brew install --cask cloudflare-warp; then
      say "installed — open the WARP app once to accept its terms"
    else
      say "brew install failed"; skipped="$skipped WARP"
    fi
  else
    say "Homebrew not found — install it from https://brew.sh, then run this again"
    skipped="$skipped WARP"
  fi
else
  say "skipped — install WARP later: brew install --cask cloudflare-warp"
  skipped="$skipped WARP"
fi

step 3/5 "Install the control center"
if [ ! -d "$DIR/OpenHop.app" ]; then
  say "OpenHop.app is missing from this image"; skipped="$skipped app"
elif [ -d /Applications/OpenHop.app ] && [ -n "$SRC_VERSION" ] && [ "$INSTALLED_VERSION" = "$SRC_VERSION" ]; then
  say "already installed: OpenHop $INSTALLED_VERSION"
  if confirm "Reinstall the app anyway?" n; then
    rm -rf /Applications/OpenHop.app
    if cp -R "$DIR/OpenHop.app" /Applications/OpenHop.app; then say "reinstalled: /Applications/OpenHop.app"
    else say "copy failed"; skipped="$skipped app"; fi
  else
    say "skipped — existing app kept"
  fi
elif confirm "Copy OpenHop.app to /Applications?" y; then
  rm -rf /Applications/OpenHop.app
  if cp -R "$DIR/OpenHop.app" /Applications/OpenHop.app; then say "installed: /Applications/OpenHop.app"
  else say "copy failed"; skipped="$skipped app"; fi
else
  say "skipped — the app will not be installed"
  skipped="$skipped app"
fi

step 4/5 "Install runtime, CLI and background jobs"
RUNTIME_OK=0
[ -n "$INSTALLED_VERSION" ] && [ "$INSTALLED_VERSION" = "$SRC_VERSION" ] \
  && [ -x "$HOME/.local/bin/openhop" ] && [ -x "$DEST/venv/bin/python" ] \
  && launchctl print "gui/$(id -u)/com.openhop.keeper" >/dev/null 2>&1 \
  && launchctl print "gui/$(id -u)/com.openhop.guard" >/dev/null 2>&1 && RUNTIME_OK=1
if [ ! -f "$PAYLOAD/install-macos.sh" ]; then
  say "missing payload/install-macos.sh"; skipped="$skipped runtime"
elif [ "$RUNTIME_OK" = 1 ]; then
  say "already installed: runtime $INSTALLED_VERSION with jobs loaded"
  if confirm "Reinstall runtime anyway?" n; then
    if bash "$PAYLOAD/install-macos.sh"; then say "runtime reinstalled"
    else say "runtime install failed — see the messages above"; skipped="$skipped runtime"; fi
  else
    say "skipped — existing runtime kept"
  fi
elif confirm "Install runtime, shims and launchd jobs?" y; then
  if bash "$PAYLOAD/install-macos.sh"; then say "runtime installed"
  else say "runtime install failed — see the messages above"; skipped="$skipped runtime"; fi
else
  say "skipped — no CLI or background jobs installed"
  skipped="$skipped runtime"
fi

step 5/5 "Health check"
export PATH="$HOME/.local/bin:$PATH"
if command -v openhop-doctor >/dev/null 2>&1; then
  openhop-doctor || true
else
  say "openhop-doctor not installed — run the runtime step to get it"
fi

echo ""
if [ -n "$skipped" ]; then
  say "Skipped:$skipped — finish those before relying on OpenHop."
else
  say "Done. Open OpenHop from Applications (right-click -> Open on first launch)."
fi
echo ""

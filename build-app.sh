#!/bin/bash
# Build the native OpenHop Control Center (AppKit, Swift) on macOS arm64.
# Run from the openhop-mac folder AFTER install-macos.sh:  bash build-app.sh
# Produces dist/OpenHop.app, ad-hoc signs it, and installs it to /Applications/OpenHop.app.
# The Python backend and CLI shims are invoked by the app; they are never rebuilt here.
set -u

SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)"
DEST="$HOME/Library/Application Support/openhop"
APP="$SRC/dist/OpenHop.app"
CONTENTS="$APP/Contents"

say(){ echo "  $*"; }
ok(){ echo "  ✓ $*"; }
die(){ echo "  ✗ $*"; exit 1; }

[ "$(uname -s)" = "Darwin" ] || die "macOS only"
[ "$(uname -m)" = "arm64" ] || die "not arm64 — use a native (non-Rosetta) Terminal"
[ -d "$DEST/venv" ] || die "venv not found at $DEST/venv — run bash install-macos.sh first"
[ -f "$SRC/macos/ControlCenter.swift" ] || die "missing macos/ControlCenter.swift — run me from the openhop-mac folder"
[ -f "$SRC/macos/Info.plist" ] || die "missing macos/Info.plist"
[ -f "$SRC/assets/brand/openhop.icns" ] || die "missing assets/brand/openhop.icns"
command -v swiftc >/dev/null 2>&1 || die "swiftc not found — install the Command Line Tools"
command -v codesign >/dev/null 2>&1 || die "codesign not found"

# Clean, from-scratch bundle.
rm -rf "$APP" || die "could not remove old $APP"
mkdir -p "$CONTENTS/MacOS" "$CONTENTS/Resources" || die "could not create app bundle directories"

say "compiling native SwiftUI app…"
cd "$SRC" || die "cannot cd to $SRC"
swiftc -swift-version 5 -Onone -parse-as-library -target arm64-apple-macos13.0 \
  -o "$CONTENTS/MacOS/OpenHop" \
  "$SRC/macos/LifecycleCoordinator.swift" "$SRC/macos/DashboardModel.swift" "$SRC/macos/UpdateManager.swift" "$SRC/macos/ControlCenter.swift" \
  || die "Swift compile failed — see errors above"
[ -x "$CONTENTS/MacOS/OpenHop" ] || die "compiled binary missing"
ok "compiled: $CONTENTS/MacOS/OpenHop"

cp "$SRC/macos/Info.plist" "$CONTENTS/Info.plist" || die "could not copy Info.plist"
cp "$SRC/assets/brand/openhop.icns" "$CONTENTS/Resources/openhop.icns" || die "could not copy icon"

# Embed the install payload so the .pkg postinstall can run the full setup.
PAYLOAD="$CONTENTS/Resources/payload"
mkdir -p "$PAYLOAD" || die "could not create payload directory"
cp "$SRC/install-macos.sh" "$SRC/requirements-macos.txt" "$SRC/LICENSE" "$PAYLOAD/" || die "could not copy payload files"
cp -R "$SRC/src" "$SRC/scripts" "$SRC/launchd" "$PAYLOAD/" || die "could not copy payload folders"

say "signing app (ad-hoc, no sudo)…"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || die "codesign failed"
codesign -dv --verbose=4 "$APP" >/dev/null 2>&1 || die "codesign -dv --verbose=4 verification failed"
BUNDLE_ID="$(/usr/libexec/PlistBuddy -c 'Print :CFBundleIdentifier' "$CONTENTS/Info.plist" 2>/dev/null || true)"
plutil -lint "$CONTENTS/Info.plist" >/dev/null || die "Info.plist lint failed"
say "bundle identifier: ${BUNDLE_ID:-unknown}"
codesign -dv --verbose=4 "$APP" 2>&1 | grep -E 'Identifier=|Signature=|Identifier=' | sed 's/^/  /' || true
ok "codesign -dv --verbose=4 verification passed"
ok "built: $APP"

say "installing to /Applications/OpenHop.app…"
if rm -rf /Applications/OpenHop.app 2>/dev/null && cp -R "$APP" /Applications/OpenHop.app 2>/dev/null; then
  ok "installed: /Applications/OpenHop.app"
else
  say "could not replace /Applications/OpenHop.app (root-owned or in use) — use the .pkg or the in-app installer"
fi
echo ""
echo "  First launch: right-click OpenHop.app, choose Open, then Open again"
echo "  (ad-hoc signed apps may still be blocked by Gatekeeper)."
echo ""

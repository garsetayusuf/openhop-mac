#!/bin/bash
# Build a separate OpenHop Test app for UI debugging. Never installed to /Applications.
set -eu
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)"
APP="$SRC/dist/OpenHop Test.app"
C="$APP/Contents"

rm -rf "$APP"
mkdir -p "$C/MacOS" "$C/Resources" "$C/Resources/payload"

swiftc -swift-version 5 -Onone -parse-as-library -target arm64-apple-macos13.0 \
  -o "$C/MacOS/OpenHop Test" \
  "$SRC/macos/LifecycleCoordinator.swift" "$SRC/macos/DashboardModel.swift" \
  "$SRC/macos/UpdateManager.swift" "$SRC/macos/ControlCenter.swift"

cp "$SRC/macos/Info.plist" "$C/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleIdentifier com.openhop.controlcenter.test" "$C/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleExecutable 'OpenHop Test'" "$C/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleName OpenHop Test" "$C/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleDisplayName OpenHop Test" "$C/Info.plist"
cp "$SRC/assets/brand/openhop.icns" "$C/Resources/openhop.icns"
cp "$SRC/install-macos.sh" "$SRC/requirements-macos.txt" "$SRC/LICENSE" "$C/Resources/payload/"
cp -R "$SRC/src" "$SRC/scripts" "$SRC/launchd" "$C/Resources/payload/"

codesign --force --deep --sign - "$APP" >/dev/null 2>&1
echo "built: $APP"

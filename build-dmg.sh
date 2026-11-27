#!/bin/bash
# Build a standard drag-to-Applications DMG containing only OpenHop.app.
# The Finder layout is baked in via assets/dmg/DS_Store so CI produces the
# same arranged window as a local build — no Finder automation required.
set -eu
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/openhop-dmg.XXXXXX")"
RAW="$SRC/dist/OpenHop-rw.dmg"
DMG="$SRC/dist/OpenHop.dmg"

bash "$SRC/build-app.sh"

cp -R "$SRC/dist/OpenHop.app" "$STAGE/OpenHop.app"
ln -s /Applications "$STAGE/Applications"
cp "$SRC/assets/dmg/background.png" "$STAGE/.background.png"
cp "$SRC/assets/dmg/DS_Store" "$STAGE/.DS_Store"
cp "$SRC/assets/brand/openhop.icns" "$STAGE/.VolumeIcon.icns"

mkdir -p "$SRC/dist"
rm -f "$RAW" "$DMG"
hdiutil create -volname OpenHop -srcfolder "$STAGE" -ov -format UDRW "$RAW"

echo "  enabling volume icon…"
MNT="$(mktemp -d "${TMPDIR:-/tmp}/openhop-mnt.XXXXXX")"
hdiutil attach "$RAW" -nobrowse -quiet -mountpoint "$MNT"
SetFile -a C "$MNT" || true
hdiutil detach "$MNT" -quiet

hdiutil convert "$RAW" -format UDZO -o "$DMG" -ov
hdiutil verify "$DMG"
rm -f "$RAW"
echo "built: $DMG"

#!/bin/bash
# Build a standard drag-to-Applications DMG containing only OpenHop.app.
set -eu
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)"
STAGE="$(mktemp -d "${TMPDIR:-/tmp}/openhop-dmg.XXXXXX")"
RAW="$SRC/dist/OpenHop-rw.dmg"
DMG="$SRC/dist/OpenHop.dmg"

bash "$SRC/build-app.sh"

cp -R "$SRC/dist/OpenHop.app" "$STAGE/OpenHop.app"
ln -s /Applications "$STAGE/Applications"
cp "$SRC/assets/dmg/background.png" "$STAGE/.background.png"
cp "$SRC/assets/brand/openhop.icns" "$STAGE/.VolumeIcon.icns"

mkdir -p "$SRC/dist"
rm -f "$RAW" "$DMG"
hdiutil create -volname OpenHop -srcfolder "$STAGE" -ov -format UDRW "$RAW"

echo "  arranging Finder window…"
MNT="$(mktemp -d "${TMPDIR:-/tmp}/openhop-mnt.XXXXXX")"
hdiutil attach "$RAW" -nobrowse -quiet -mountpoint "$MNT"
if ! osascript <<EOF
tell application "Finder"
  tell folder (POSIX file "$MNT")
    open
    delay 2
    set current view of container window to icon view
    set toolbar visible of container window to false
    set statusbar visible of container window to false
    set bounds of container window to {160, 140, 760, 510}
    set vo to icon view options of container window
    set arrangement of vo to not arranged
    set icon size of vo to 96
    set background picture of vo to POSIX file "$MNT/.background.png"
    set position of item "OpenHop.app" of container window to {130, 185}
    set position of item "Applications" of container window to {470, 185}
    close
    open
    delay 1
    update without registering applications
    delay 1
    close
  end tell
end tell
EOF
then
  echo "  Finder arrangement skipped — DMG will still be valid"
fi
SetFile -a C "$MNT" || true
hdiutil detach "$MNT" -quiet

hdiutil convert "$RAW" -format UDZO -o "$DMG" -ov
hdiutil verify "$DMG"
rm -f "$RAW"
echo "built: $DMG"

#!/bin/bash
set -euo pipefail
VER="${1:?usage: bash release.sh 3.8.2 [release notes]}"
NOTES="${2:-OpenHop for macOS v$VER}"
cd "$(dirname "$0")"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" macos/Info.plist
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $(echo "$VER" | tr -d '.')" macos/Info.plist
bash build-dmg.sh
git add macos/Info.plist
git commit -m "Release v$VER"
git tag "v$VER"
git push
git push --tags
gh release create "v$VER" dist/OpenHop.dmg --title "v$VER" --notes "$NOTES"

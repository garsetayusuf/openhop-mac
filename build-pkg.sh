#!/bin/bash
# Build a native macOS Installer package with Introduction/Read Me/License/Install steps.
set -eu
SRC="$(cd "$(dirname "${BASH_SOURCE[0]:-}")" 2>/dev/null && pwd)"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/openhop-pkg.XXXXXX")"
ROOT="$WORK/root"
RES="$WORK/resources"
mkdir -p "$ROOT/Applications" "$RES"

bash "$SRC/build-app.sh"
cp -R "$SRC/dist/OpenHop.app" "$ROOT/Applications/OpenHop.app"
cp "$SRC/README.md" "$RES/README.txt"
cp "$SRC/LICENSE" "$RES/LICENSE.txt"

pkgbuild --root "$ROOT" \
  --identifier com.openhop.controlcenter.pkg \
  --version 1.0 \
  --install-location / \
  --scripts "$SRC/pkg-scripts" \
  "$WORK/component.pkg"

cat > "$WORK/distribution.xml" <<'XML'
<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>OpenHop Control Center</title>
  <readme file="README.txt" mime-type="text/plain"/>
  <license file="LICENSE.txt" mime-type="text/plain"/>
  <options customize="never" require-scripts="false" rootVolumeOnly="true"/>
  <choices-outline>
    <line choice="default"/>
  </choices-outline>
  <choice id="default" title="OpenHop" description="Native control center, CLI and background jobs">
    <pkg-ref id="com.openhop.controlcenter.pkg"/>
  </choice>
  <pkg-ref id="com.openhop.controlcenter.pkg" version="1.0" onConclusion="none">component.pkg</pkg-ref>
</installer-gui-script>
XML

mkdir -p "$SRC/dist"
rm -f "$SRC/dist/OpenHop.pkg"
productbuild --distribution "$WORK/distribution.xml" --resources "$RES" \
  --package-path "$WORK" "$SRC/dist/OpenHop.pkg"

installer -pkginfo -pkg "$SRC/dist/OpenHop.pkg" | head -12 || true
echo "built: $SRC/dist/OpenHop.pkg"

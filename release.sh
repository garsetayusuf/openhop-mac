#!/bin/bash
set -euo pipefail
VER="${1:?usage: bash release.sh 3.8.2 [release notes]}"
NOTES="${2:-OpenHop for macOS v$VER}"
SIGN_DIR="$HOME/Library/Application Support/openhop/signing"
command -v node >/dev/null 2>&1 || { echo "node is required for signing"; exit 1; }

if [ "${1:-}" = "--init-keys" ]; then
  mkdir -p "$SIGN_DIR"
  if [ -f "$SIGN_DIR/private.pem" ] && [ "${2:-}" != "--force" ]; then
    echo "keys already exist; use: bash release.sh --init-keys --force"
    exit 1
  fi
  node -e 'const c=require("crypto"),fs=require("fs");const d=process.argv[1];const k=c.generateKeyPairSync("ed25519");fs.writeFileSync(d+"/private.pem",k.privateKey.export({type:"pkcs8",format:"pem"}));fs.writeFileSync(d+"/public.base64",k.publicKey.export({type:"spki",format:"der"}).subarray(-32).toString("base64"));' "$SIGN_DIR"
  chmod 600 "$SIGN_DIR/private.pem"
  echo "Public key (paste into UpdateManager.publicKeyBase64): $(cat "$SIGN_DIR/public.base64")"
  exit 0
fi
cd "$(dirname "$0")"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString $VER" macos/Info.plist
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion $(echo "$VER" | tr -d '.')" macos/Info.plist
bash build-dmg.sh
[ -f "$SIGN_DIR/private.pem" ] || { echo "no signing key — run: bash release.sh --init-keys"; exit 1; }
SHA=$(shasum -a 256 dist/OpenHop.dmg | awk '{print $1}')
MSG="OpenHop|$VER|$SHA"
node -e 'const c=require("crypto"),fs=require("fs");const m=Buffer.from(process.argv[2]);fs.writeFileSync(process.argv[3],c.sign(null,m,fs.readFileSync(process.argv[1])));' "$SIGN_DIR/private.pem" "$MSG" dist/signature.bin
SIG=$(base64 -i dist/signature.bin | tr -d '\n')
printf 'version=%s\nsha256=%s\nsignature=%s\n' "$VER" "$SHA" "$SIG" > dist/OpenHop.dmg.sha256
echo "  signed manifest: dist/OpenHop.dmg.sha256"
git add macos/Info.plist
git commit -m "Release v$VER"
git tag "v$VER"
git push
git push --tags
gh release create "v$VER" dist/OpenHop.dmg dist/OpenHop.dmg.sha256 --title "v$VER" --notes "$NOTES"

"""Verify a downloaded OpenHop DMG against its signed sha256 manifest."""
from __future__ import annotations

import base64
import hashlib
import sys
from pathlib import Path

from ed25519_verify import verify


def main(dmg: str, manifest_path: str, pubkey_b64: str, expected_version: str | None = None) -> int:
    values: dict[str, str] = {}
    for line in Path(manifest_path).read_text(encoding="utf-8").splitlines():
        if "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip()
    version = values.get("version", "")
    if expected_version is not None and version != expected_version:
        print("FAIL: version mismatch")
        return 1
    expected = values.get("sha256", "").lower()
    signature_text = values.get("signature", "")
    if not version or len(expected) != 64 or not signature_text:
        print("FAIL: manifest is incomplete")
        return 1
    actual = hashlib.sha256(Path(dmg).read_bytes()).hexdigest()
    if actual != expected:
        print("FAIL: SHA-256 mismatch")
        return 1
    try:
        public_key = base64.b64decode(pubkey_b64, validate=True)
        signature = base64.b64decode(signature_text, validate=True)
    except Exception:
        print("FAIL: invalid base64 in manifest or public key")
        return 1
    message = f"OpenHop|{version}|{actual}".encode("utf-8")
    if not verify(public_key, message, signature):
        print("FAIL: signature verification failed")
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    if len(sys.argv) not in (4, 5):
        print("FAIL: usage: verify_update.py DMG MANIFEST PUBKEY_B64 [EXPECTED_VERSION]")
        raise SystemExit(1)
    expected_version = sys.argv[4] if len(sys.argv) == 5 else None
    raise SystemExit(main(sys.argv[1], sys.argv[2], sys.argv[3], expected_version))

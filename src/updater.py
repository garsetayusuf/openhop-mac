"""Automatic updates for the openhop exe.

How it works:
1. Every few hours the app reads https://openhop.cfd/latest.json ({version, url, sha256, size}).
2. If that exe is different from the one running (by SHA-256, so a version label alone never causes a loop),
   it's downloaded to %LOCALAPPDATA%\\openhop\\update\\ and checked against the SHA-256.
3. It's installed when the user clicks "Update now", or on its own the next time openhop starts
   (for example at login): the running exe is renamed to <name>.old (Windows allows that for a running exe),
   the new one is put in its place and started, and the old process exits. <name>.old is deleted next start.
4. The new exe finds a short note left by the old one, runs a handful of checks (right version, right file,
   proxy, workers, connection) and reports how it went, in the window and in a notification.

Only the frozen exe updates itself; running from source just reports that a new version exists.
Downloads come only from openhop.cfd over HTTPS. Any failure leaves the current exe as it was.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

from ouc_common import DEST_DIR, VERSION, L, append_log

LATEST_URL = "https://openhop.cfd/latest.json"
ALLOWED_PREFIX = "https://openhop.cfd/"
# Updates must be signed with the openhop dev team's release key (Ed25519; the private keys never leave the dev PC,
# see tools/make_update_keys.mjs). So even someone who takes over the website or the Cloudflare account
# can't push their own exe to everyone. Primary key first; the backup is kept offline in case it's lost.
UPDATE_KEYS = (
    "3a67ea1eabd571b735257fd089881c380f5007784d13efe86e48f0bee4fa860c",  # primary
    "2ca14a87daaba558945f3fe91e1b55b7264766d400ee644b5ba27963d3768f5a",  # backup (offline)
)


def signed_message(version: str, sha: str, size: int, url: str) -> bytes:
    """Exactly what the release key signs (the website build must produce the same bytes)."""
    return f"openhop-update|1|{version}|{sha}|{size}|{url}".encode("utf-8")


def signature_ok(info: dict) -> bool:
    from ed25519_verify import verify
    try:
        sig = bytes.fromhex(str(info.get("sig", "")))
        msg = signed_message(str(info.get("version", "")), str(info.get("sha256", "")).lower(),
                             int(info.get("size", 0) or 0), str(info.get("url", "")))
    except (ValueError, TypeError):
        return False
    return any(verify(bytes.fromhex(k), msg, sig) for k in UPDATE_KEYS)


UPDATE_DIR = DEST_DIR / "update"
STAGED_EXE = UPDATE_DIR / "openhop-new.exe"
STAGED_META = UPDATE_DIR / "staged.json"
JUST_UPDATED = UPDATE_DIR / "just_updated.json"
LAST_CHECK = UPDATE_DIR / "last_check.json"
LOG_FILE = DEST_DIR / "logs" / "update.log"
CHECK_EVERY = 6 * 3600
FIRST_CHECK_AFTER = 45

_lock = threading.Lock()
_state: dict = {"checked": 0, "latest": "", "available": False, "ready": False, "error": "", "busy": False}
_own_sha: str = ""


def _log(msg: str) -> None:
    append_log(LOG_FILE, msg)


def frozen() -> bool:
    return bool(getattr(sys, "frozen", False)) and sys.executable.lower().endswith(".exe")


def _vtuple(v: str) -> tuple[int, ...]:
    out = []
    for part in str(v).strip().lstrip("v").split("."):
        num = "".join(ch for ch in part if ch.isdigit())
        out.append(int(num) if num else 0)
    return tuple(out)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def own_sha() -> str:
    global _own_sha
    if not _own_sha and frozen():
        try:
            _own_sha = _sha256(Path(sys.executable))
        except Exception:
            _own_sha = ""
    return _own_sha


def _opener():
    # Straight to the internet: the update check must work even when WARP or the local proxy is down.
    return urllib.request.build_opener(urllib.request.ProxyHandler({}))


def _get(url: str, timeout: float):
    req = urllib.request.Request(url, headers={"User-Agent": f"openhop/{VERSION}", "Cache-Control": "no-cache"})
    return _opener().open(req, timeout=timeout)


def _read_staged() -> dict:
    try:
        meta = json.loads(STAGED_META.read_text(encoding="utf-8"))
        if STAGED_EXE.is_file() and STAGED_EXE.stat().st_size == int(meta.get("size", -1)):
            return meta
    except Exception:
        pass
    return {}


def state() -> dict:
    """For the UI: {available, ready, latest, current, can_install, error}."""
    with _lock:
        s = dict(_state)
    staged = _read_staged()
    if staged and staged.get("sha256") != own_sha():
        s["ready"] = True
        s["available"] = True
        s["latest"] = s.get("latest") or staged.get("version", "")
    s["current"] = VERSION
    s["can_install"] = frozen()
    s["post"] = post_update_report()
    return s


def check(download: bool = True) -> dict:
    """Look for a new version; download and verify it if there is one. Safe to call from any thread."""
    with _lock:
        if _state["busy"]:
            return dict(_state)
        _state["busy"] = True
    try:
        with _get(LATEST_URL, 15) as resp:
            info = json.loads(resp.read(64 * 1024).decode("utf-8"))
        latest = str(info.get("version", ""))
        url = str(info.get("url", ""))
        sha = str(info.get("sha256", "")).lower()
        size = int(info.get("size", 0) or 0)
        if not url.startswith(ALLOWED_PREFIX) or len(sha) != 64 or size <= 0:
            raise ValueError("latest.json looks wrong")
        if not signature_ok(info):
            # Never download it: either the site was tampered with, or the release wasn't signed.
            raise ValueError("the update on the website isn't signed with openhop's key, so it was ignored")
        mine = own_sha()
        if mine:
            newer = sha != mine and _vtuple(latest) >= _vtuple(VERSION)
        else:
            newer = _vtuple(latest) > _vtuple(VERSION)
        with _lock:
            _state.update({"checked": int(time.time()), "latest": latest, "available": newer, "error": ""})
        if not newer:
            _clean_staged()
            with _lock:
                _state["ready"] = False
            return state()
        if download and frozen():
            staged = _read_staged()
            if staged.get("sha256") != sha:
                _download(url, sha, size, latest)
            with _lock:
                _state["ready"] = True
        return state()
    except Exception as exc:
        with _lock:
            _state.update({"checked": int(time.time()), "error": str(exc)[:200]})
        _log(f"check failed: {exc}")
        return state()
    finally:
        with _lock:
            _state["busy"] = False


def _download(url: str, sha: str, size: int, version: str) -> None:
    UPDATE_DIR.mkdir(parents=True, exist_ok=True)
    part = UPDATE_DIR / "openhop-new.exe.part"
    h = hashlib.sha256()
    got = 0
    with _get(url, 30) as resp, part.open("wb") as fh:
        while True:
            block = resp.read(1024 * 1024)
            if not block:
                break
            got += len(block)
            if got > size:
                raise ValueError("download is bigger than expected")
            h.update(block)
            fh.write(block)
    if got != size or h.hexdigest() != sha:
        part.unlink(missing_ok=True)
        raise ValueError("download didn't match its checksum")
    STAGED_META.unlink(missing_ok=True)
    os.replace(part, STAGED_EXE)
    STAGED_META.write_text(json.dumps({"version": version, "sha256": sha, "size": size,
                                       "at": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")
    _log(f"downloaded {version} ({size} bytes)")


def _clean_staged() -> None:
    for p in (STAGED_EXE, STAGED_META, UPDATE_DIR / "openhop-new.exe.part"):
        try:
            p.unlink(missing_ok=True)
        except Exception:
            pass


def _remember_just_updated(staged: dict) -> None:
    """Leave a note for the new exe: 'you were started by an update, tell the user how it went'."""
    try:
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        JUST_UPDATED.write_text(json.dumps({
            "from": VERSION,
            "to": str(staged.get("version", "")),
            "sha256": str(staged.get("sha256", "")),
            "at": int(time.time()),
        }), encoding="utf-8")
    except Exception as exc:
        _log(f"could not write the update marker: {exc}")


def just_updated_pending(max_age: int = 15 * 60) -> bool:
    """True when this process was started by an update a moment ago (doesn't consume the marker)."""
    try:
        data = json.loads(JUST_UPDATED.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return False
        if str(data.get("to", "")) != VERSION:
            return False
        return 0 <= int(time.time()) - int(data.get("at", 0)) <= max_age
    except Exception:
        return False


def take_just_updated(max_age: int = 15 * 60) -> dict:
    """One-shot version of just_updated_pending(): returns {from, to, sha256, at} or {}."""
    if not just_updated_pending(max_age):
        try:
            JUST_UPDATED.unlink(missing_ok=True)
        except Exception:
            pass
        return {}
    try:
        data = json.loads(JUST_UPDATED.read_text(encoding="utf-8"))
        JUST_UPDATED.unlink(missing_ok=True)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def post_update_report() -> dict:
    """The last set of checks run after an update (empty if there never was one)."""
    try:
        data = json.loads(LAST_CHECK.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_post_update_report(report: dict) -> None:
    try:
        UPDATE_DIR.mkdir(parents=True, exist_ok=True)
        LAST_CHECK.write_text(json.dumps(report), encoding="utf-8")
    except Exception as exc:
        _log(f"could not save the post-update checks: {exc}")


def cleanup_old() -> None:
    """Delete the previous exe left by an update (it can't be deleted while it was still running)."""
    if not frozen():
        return
    old = Path(sys.executable + ".old")
    try:
        old.unlink(missing_ok=True)
    except Exception:
        pass
    staged = _read_staged()
    if staged and staged.get("sha256") == own_sha():
        _clean_staged()  # this exe *is* the staged one: done


def install(args: list[str] | None = None) -> tuple[bool, str]:
    """Swap the running exe for the downloaded one and start it. The caller exits right after success."""
    if not frozen():
        return False, L("Updates install themselves only in the openhop exe.",
                        "Las actualizaciones solo se instalan solas en el exe de openhop.")
    staged = _read_staged()
    if not staged or staged.get("sha256") == own_sha():
        return False, L("There's no downloaded update to install.", "No hay ninguna actualización descargada que instalar.")
    exe = Path(sys.executable)
    old = Path(str(exe) + ".old")
    try:
        if _sha256(STAGED_EXE) != staged.get("sha256"):
            _clean_staged()
            return False, L("The downloaded update was damaged; openhop will download it again.",
                            "La actualización descargada estaba dañada; openhop la volverá a descargar.")
        try:
            old.unlink(missing_ok=True)
        except Exception:
            old = Path(f"{exe}.{int(time.time())}.old")
        os.replace(exe, old)
        try:
            shutil.copy2(STAGED_EXE, exe)
            if _sha256(exe) != staged.get("sha256"):
                raise OSError("copied exe doesn't match")
        except Exception:
            try:
                exe.unlink(missing_ok=True)
            except Exception:
                pass
            os.replace(old, exe)
            raise
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        _remember_just_updated(staged)
        subprocess.Popen([str(exe), *(args or [])], creationflags=flags, close_fds=True, cwd=str(exe.parent))
        _log(f"installed {staged.get('version')} over {VERSION} at {exe}")
        return True, L(f"Updating to openhop {staged.get('version')}…", f"Actualizando a openhop {staged.get('version')}…")
    except PermissionError:
        _log(f"install failed: no permission to replace {exe}")
        return False, L("openhop can't replace its own file in this folder. Download the new version from "
                        "openhop.cfd instead.",
                        "openhop no puede reemplazar su propio archivo en esta carpeta. Descarga la versión nueva "
                        "desde openhop.cfd.")
    except Exception as exc:
        _log(f"install failed: {exc}")
        return False, L("Couldn't install the update; openhop keeps running the current version.",
                        "No se pudo instalar la actualización; openhop sigue con la versión actual.")


def install_on_start(args: list[str]) -> bool:
    """At startup: if a verified update is waiting, install it and return True (the caller exits)."""
    try:
        if not frozen() or os.environ.get("OPENHOP_NO_AUTOUPDATE"):
            return False
        staged = _read_staged()
        if not staged or staged.get("sha256") == own_sha():
            return False
        if _vtuple(staged.get("version", "0")) < _vtuple(VERSION):
            _clean_staged()  # older than what's running (user installed something newer by hand)
            return False
        return install(args)[0]
    except Exception as exc:
        _log(f"install on start failed: {exc}")
        return False


def start_background(on_ready=None, enabled=lambda: True) -> None:
    """Check now-ish and then every few hours; call on_ready(state) once when an update is downloaded."""
    def loop() -> None:
        time.sleep(FIRST_CHECK_AFTER)
        told = ""
        while True:
            try:
                if enabled():
                    s = check(download=True)
                    if s.get("ready") and on_ready and told != s.get("latest"):
                        told = s.get("latest", "")
                        on_ready(s)
            except Exception as exc:
                _log(f"background check error: {exc}")
            time.sleep(CHECK_EVERY)

    threading.Thread(target=loop, daemon=True, name="openhop-updater").start()


if __name__ == "__main__":
    print(json.dumps(check(download="--download" in sys.argv), indent=2))

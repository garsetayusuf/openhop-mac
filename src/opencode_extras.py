"""Small local helper for the extras openhop adds inside OpenCode Desktop (see desktop_brand / opencode_extras_js).

It runs inside the keeper (always on) and listens on 127.0.0.1 only:
- voice typing: downloads whisper.cpp + a speech model once (on request, with progress), then turns the audio
  OpenCode records into text. Everything stays on the PC; nothing is sent anywhere.
- site icons: fetches a website's icon for the project-icon box (OpenCode itself can't: those servers don't allow
  pages to download from them).
- Explorar: browses openhop.cfd's catalogue of shared text add-ons and installs one into an OpenCode project or for
  every project (explore.py; it only writes skill / command / AGENTS.md files, in folders OpenCode knows).

Only OpenCode's own page (origin oc://renderer) and the openhop app (a token file only this user can read) may
call it, so a website open in a browser can't use it.
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import secrets
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from ouc_common import DEST_DIR, append_log

PORT = int(os.environ.get("OUC_EXTRAS_PORT", "41993"))
ALLOWED_ORIGINS = {"oc://renderer"}
LOG_FILE = DEST_DIR / "logs" / "opencode_extras.log"
TOKEN_FILE = DEST_DIR / "extras_token"
VOICE_DIR = DEST_DIR / "voice"

# Pinned downloads (checked by SHA-256 before use).
WHISPER_ZIP = {
    "url": "https://github.com/ggml-org/whisper.cpp/releases/download/b5454/whisper-bin-x64.zip",
    "sha256": "6ba69e3482d7826214f90a6a9c84ca07782aec1e1d0c6a7c30c994fd5d816ccb",
    "size": 8928640,
}
MODELS = {
    "small": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small-q5_1.bin",
        "sha256": "ae85e4a935d7a567bd102fe55afc16bb595bdb618e11b2fc7591bc08120411bb",
        "size": 190085487,
    },
    "turbo": {
        "url": "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin",
        "sha256": "394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
        "size": 574041195,
    },
}
MODEL = os.environ.get("OUC_VOICE_MODEL", "small")
MAX_AUDIO = 40 * 1024 * 1024  # ~20 min of 16 kHz 16-bit mono
# What Whisper "hears" in silence or noise (it was trained on subtitled videos). Dropped from the result.
_HALLUCINATIONS = re.compile(
    r"(subt[ií]tulos (realizados|creados) por|amara\.org|gracias por ver|thanks? for watching|"
    r"suscr[ií]bete|subscribe to|^\W*$)", re.I)

_state_lock = threading.Lock()
_state: dict = {"downloading": False, "done": 0, "total": 0, "error": "", "stage": ""}
_transcribe_lock = threading.Lock()


def _log(msg: str) -> None:
    append_log(LOG_FILE, msg)


# ---------------------------------------------------------------------------------------------- token (openhop app)
def token() -> str:
    try:
        t = TOKEN_FILE.read_text(encoding="utf-8").strip()
        if len(t) >= 32:
            return t
    except Exception:
        pass
    t = secrets.token_hex(24)
    TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_FILE.write_text(t, encoding="utf-8")
    return t


# ---------------------------------------------------------------------------------------------- voice: install
def _bin_dir() -> Path:
    return VOICE_DIR / "bin"


def _model_path() -> Path:
    return VOICE_DIR / f"model-{MODEL}.bin"


def voice_installed() -> bool:
    return (_bin_dir() / "whisper-cli.exe").is_file() and _model_path().is_file()


def voice_status() -> dict:
    with _state_lock:
        st = dict(_state)
    need = WHISPER_ZIP["size"] + MODELS[MODEL]["size"]
    return {"ok": True, "installed": voice_installed(), "downloading": st["downloading"], "done": st["done"],
            "total": st["total"] or need, "size_mb": round(need / 1e6), "error": st["error"], "stage": st["stage"]}


def _download(url: str, dest: Path, sha256: str, size: int, base_done: int) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    h = hashlib.sha256()
    req = urllib.request.Request(url, headers={"User-Agent": "openhop"})
    with urllib.request.urlopen(req, timeout=60) as r, part.open("wb") as f:
        got = 0
        while True:
            chunk = r.read(1 << 20)
            if not chunk:
                break
            f.write(chunk)
            h.update(chunk)
            got += len(chunk)
            with _state_lock:
                _state["done"] = base_done + got
    if got != size or h.hexdigest() != sha256:
        part.unlink(missing_ok=True)
        raise ValueError(f"{dest.name}: the download didn't match (size {got})")
    os.replace(part, dest)


def _install_worker() -> None:
    try:
        VOICE_DIR.mkdir(parents=True, exist_ok=True)
        z, m = WHISPER_ZIP, MODELS[MODEL]
        with _state_lock:
            _state.update(done=0, total=z["size"] + m["size"], error="", stage="program")
        if not (_bin_dir() / "whisper-cli.exe").is_file():
            zpath = VOICE_DIR / "whisper-bin-x64.zip"
            _download(z["url"], zpath, z["sha256"], z["size"], 0)
            tmp = VOICE_DIR / "bin.new"
            shutil.rmtree(tmp, ignore_errors=True)
            tmp.mkdir()
            with zipfile.ZipFile(zpath) as zf:
                for info in zf.infolist():
                    name = Path(info.filename).name
                    if name == "whisper-cli.exe" or (name.endswith(".dll") and name != "SDL2.dll"):
                        (tmp / name).write_bytes(zf.read(info))
            if not (tmp / "whisper-cli.exe").is_file():
                raise ValueError("whisper-cli.exe missing from the download")
            shutil.rmtree(_bin_dir(), ignore_errors=True)
            os.replace(tmp, _bin_dir())
            zpath.unlink(missing_ok=True)
        with _state_lock:
            _state.update(done=z["size"], stage="model")
        if not _model_path().is_file():
            _download(m["url"], _model_path(), m["sha256"], m["size"], z["size"])
        with _state_lock:
            _state.update(done=z["size"] + m["size"], stage="ready")
        _log(f"voice typing installed ({MODEL})")
    except Exception as exc:
        _log(f"voice install failed: {exc}")
        with _state_lock:
            _state["error"] = str(exc)[:200]
    finally:
        with _state_lock:
            _state["downloading"] = False


def voice_install() -> dict:
    with _state_lock:
        if _state["downloading"]:
            return voice_status()
        if voice_installed():
            return voice_status()
        _state.update(downloading=True, error="", done=0, stage="start")
    threading.Thread(target=_install_worker, daemon=True).start()
    return voice_status()


def voice_remove() -> dict:
    with _state_lock:
        if _state["downloading"]:
            return {"ok": False, "error": "busy"}
    shutil.rmtree(VOICE_DIR, ignore_errors=True)
    return voice_status()


# ---------------------------------------------------------------------------------------------- voice: transcribe
def _short(p: Path) -> str:
    """8.3 path, so whisper.cpp opens files under a user folder with accents (e.g. C:\\Users\\José)."""
    try:
        import ctypes
        buf = ctypes.create_unicode_buffer(1024)
        if ctypes.windll.kernel32.GetShortPathNameW(str(p), buf, 1024):
            return buf.value
    except Exception:
        pass
    return str(p)


def transcribe(wav: bytes, lang: str = "auto") -> dict:
    if not voice_installed():
        return {"ok": False, "error": "not_installed"}
    if not wav.startswith(b"RIFF") or len(wav) > MAX_AUDIO:
        return {"ok": False, "error": "bad_audio"}
    lang = lang if re.fullmatch(r"[a-z]{2}|auto", lang or "") else "auto"
    threads = max(1, min(8, (os.cpu_count() or 4) - 1))
    with _transcribe_lock:
        fd, tmp = tempfile.mkstemp(suffix=".wav", dir=str(VOICE_DIR))
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(wav)
            t0 = time.time()
            cmd = [str(_bin_dir() / "whisper-cli.exe"), "-m", _short(_model_path()), "-f", _short(Path(tmp)),
                   "-l", lang, "-nt", "-np", "-t", str(threads)]
            r = subprocess.run(cmd, cwd=str(_bin_dir()), capture_output=True, timeout=600,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            out = r.stdout.decode("utf-8", errors="replace")
            if r.returncode != 0:
                _log(f"whisper failed ({r.returncode}): {r.stderr.decode('utf-8', 'replace')[-400:]}")
                return {"ok": False, "error": "failed"}
            lines = [ln.strip() for ln in out.splitlines() if ln.strip()]
            lines = [ln for ln in lines if not _HALLUCINATIONS.search(ln) and not re.fullmatch(r"[\[(].*[\])]", ln)]
            text = " ".join(lines).strip()
            _log(f"transcribed {len(wav) // 32000}s of audio in {time.time() - t0:.1f}s ({len(text)} chars)")
            return {"ok": True, "text": text}
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "timeout"}
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------------------------- site icons
def _public_host(host: str) -> bool:
    if not re.fullmatch(r"[a-z0-9.-]{1,253}", host) or "." not in host or host.endswith(".local"):
        return False
    try:
        ipaddress.ip_address(host)
        return False  # no bare IPs
    except ValueError:
        return host not in ("localhost",)


def site_icon(u: str) -> tuple[bytes, str] | None:
    u = (u or "").strip()
    if not u:
        return None
    if "://" not in u:
        u = "https://" + u
    host = (urllib.parse.urlsplit(u).hostname or "").lower()
    if not _public_host(host):
        return None
    tries = [
        f"https://{host}/apple-touch-icon.png",
        f"https://www.google.com/s2/favicons?domain={urllib.parse.quote(host)}&sz=128",
        f"https://icons.duckduckgo.com/ip3/{urllib.parse.quote(host)}.ico",
    ]
    for url in tries:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 openhop"})
            with urllib.request.urlopen(req, timeout=8) as r:
                ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                data = r.read(1_000_001)
            if r.status == 200 and ctype.startswith("image/") and 64 < len(data) <= 1_000_000:
                return data, ctype
        except Exception:
            continue
    return None


# ---------------------------------------------------------------------------------------------- HTTP server
class _Handler(BaseHTTPRequestHandler):
    server_version = "openhop-extras"

    def log_message(self, fmt, *args):  # quiet
        pass

    def _origin_ok(self) -> bool:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            return True
        tok = self.headers.get("X-Openhop-Token") or ""
        return bool(tok) and secrets.compare_digest(tok, token())

    def _cors(self) -> None:
        origin = self.headers.get("Origin")
        if origin in ALLOWED_ORIGINS:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")

    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, data: dict) -> None:
        self._send(code, json.dumps(data).encode("utf-8"))

    def do_OPTIONS(self):  # noqa: N802
        if self.headers.get("Origin") not in ALLOWED_ORIGINS:
            self.send_response(403)
            self.end_headers()
            return
        self.send_response(204)
        self._cors()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Openhop")
        self.send_header("Access-Control-Max-Age", "600")
        self.end_headers()

    def do_GET(self):  # noqa: N802
        if not self._origin_ok():
            return self._json(403, {"ok": False, "error": "forbidden"})
        path, _, qs = self.path.partition("?")
        q = urllib.parse.parse_qs(qs)
        if path == "/ping":
            return self._json(200, {"ok": True, "app": "openhop"})
        if path == "/voice/status":
            return self._json(200, voice_status())
        if path == "/icon":
            got = site_icon((q.get("u") or [""])[0])
            if not got:
                return self._json(404, {"ok": False, "error": "not_found"})
            return self._send(200, got[0], got[1])
        if path.startswith("/explore/"):
            import explore
            one = lambda k: (q.get(k) or [""])[0]  # noqa: E731
            if path == "/explore/list":
                return self._json(200, explore.browse(one("kind"), one("q"), one("sort"), int(one("page") or 0)))
            if path == "/explore/item":
                return self._json(200, explore.item(one("id")))
            if path == "/explore/mine":
                return self._json(200, explore.mine())
            if path == "/explore/installed":
                return self._json(200, explore.installed())
            if path == "/explore/account":
                return self._json(200, explore.account())
            if path == "/explore/popular":
                return self._json(200, explore.popular())
        return self._json(404, {"ok": False, "error": "not_found"})

    def do_POST(self):  # noqa: N802
        if not self._origin_ok():
            return self._json(403, {"ok": False, "error": "forbidden"})
        path, _, qs = self.path.partition("?")
        q = urllib.parse.parse_qs(qs)
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_AUDIO:
            return self._json(413, {"ok": False, "error": "too_big"})
        body = self.rfile.read(length) if length else b""
        if path == "/voice/install":
            return self._json(200, voice_install())
        if path == "/voice/remove":
            return self._json(200, voice_remove())
        if path == "/voice/transcribe":
            return self._json(200, transcribe(body, (q.get("lang") or ["auto"])[0]))
        if path.startswith("/explore/"):
            import explore
            if len(body) > 64 * 1024:
                return self._json(413, {"ok": False, "error": "too_big"})
            try:
                data = json.loads(body.decode("utf-8") or "{}")
            except Exception:
                data = None
            if not isinstance(data, dict):
                return self._json(400, {"ok": False, "error": "bad_request"})
            s = lambda k: data.get(k) if isinstance(data.get(k), str) else ""  # noqa: E731
            if path == "/explore/install":
                return self._json(200, explore.install(s("id"), s("scope"), s("project"), data.get("overwrite") is True))
            if path == "/explore/uninstall":
                return self._json(200, explore.uninstall(s("id"), s("scope"), s("project")))
            if path == "/explore/submit":
                return self._json(200, explore.submit(data))
            if path == "/explore/delete":
                return self._json(200, explore.delete(s("id")))
            if path == "/explore/report":
                return self._json(200, explore.report(s("id"), s("reason")))
        return self._json(404, {"ok": False, "error": "not_found"})


class _Server(ThreadingHTTPServer):
    # Exclusive bind on Windows (no SO_REUSEADDR: another program must not be able to take the port over).
    allow_reuse_address = False
    allow_reuse_port = False
    daemon_threads = True

    def server_bind(self):
        import socket
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve_forever() -> None:
    """Run the helper (keeper thread). Retries the bind quietly if the port is busy."""
    token()
    logged = False
    while True:
        try:
            srv = _Server(("127.0.0.1", PORT), _Handler)
            _log(f"extras helper listening on 127.0.0.1:{PORT}")
            srv.serve_forever()
        except Exception as exc:
            if not logged:
                _log(f"extras helper couldn't start on port {PORT}: {exc} (retrying)")
                logged = True
        time.sleep(30)


def call(path: str, method: str = "GET", timeout: float = 5) -> dict:
    """Used by the openhop app to talk to the keeper's helper."""
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", method=method, data=b"" if method == "POST" else None,
                                 headers={"X-Openhop-Token": token()})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        serve_forever()
    print(json.dumps(voice_status()))

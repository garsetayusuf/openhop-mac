"""openhop — Control Center GUI.  https://openhop.cfd

Based on the original "Opencode Unlimited" by Avatar (ShadowySphinX).
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path

# Support frozen PyInstaller bundle as well as direct execution from windows/
def _bundle_dir() -> Path:
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)  # type: ignore[attr-defined]
    return Path(__file__).resolve().parent


_BUNDLE = _bundle_dir()
if str(_BUNDLE) not in sys.path:
    sys.path.insert(0, str(_BUNDLE))

# Startup log: when openhop "opens and closes by itself" on someone's PC, this file says how far it got.
# If it doesn't exist at all, openhop's own code never ran (antivirus or Windows blocked the exe).
_CLI_FLAGS = ("--worker", "--lane", "--hop", "--doctor", "--firewall")
_GUI_RUN = not (sys.argv[1:2] and sys.argv[1] in _CLI_FLAGS)
_STARTUP_LOG = (Path.home() / "Library" / "Application Support" / "openhop" / "logs" / "startup.log") if sys.platform == "darwin" else Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "openhop" / "logs" / "startup.log"
_FAULT_FILE = None


def _startup_log(msg: str) -> None:
    if not _GUI_RUN:
        return
    try:
        _STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        if _STARTUP_LOG.is_file() and _STARTUP_LOG.stat().st_size > 256 * 1024:
            _STARTUP_LOG.write_text("", encoding="utf-8")
        with open(_STARTUP_LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} [{os.getpid()}] {msg}\n")
    except Exception:
        pass


if _GUI_RUN:
    _startup_log(f"starting ({sys.executable}, args={sys.argv[1:]})")
    try:  # hard crashes (inside WebView2/.NET) leave a Python stack here instead of vanishing
        import faulthandler
        _STARTUP_LOG.parent.mkdir(parents=True, exist_ok=True)
        _FAULT_FILE = open(_STARTUP_LOG.with_name("crash.log"), "a", encoding="utf-8")
        faulthandler.enable(file=_FAULT_FILE)
    except Exception:
        pass

try:
    import psutil
    import webview
except Exception as _imp_exc:
    _startup_log(f"could not load a part of openhop: {_imp_exc!r}")
    raise

from ouc_common import (
    acquire_mutex,
    opencode_db_path,
    is_mutex_held,
    append_log,
    apply_warp_mode,
    current_warp_scope,
    ensure_warp_protocol,
    preferred_warp_protocol,
    BIN_DIR,
    CREATE_NO_WINDOW,
    DEST_DIR,
    MUTEX_GUARD,
    MUTEX_KEEPER,
    OLD_DEST_DIR,
    OLD_TASK_GUARD,
    OLD_TASK_KEEPER,
    VERSION,
    is_own_path,
    L,
    LANGUAGES,
    ui_lang,
    GUARD_LOG_FILE,
    GUARD_STATE_FILE,
    KEEPER_LOG_FILE,
    LANE_DIR,
    OPENCODE_LOG_FILE,
    SCRIPTS_DIR,
    SERVICE_PROXY_PORT,
    TASK_GUARD,
    TASK_KEEPER,
    ensure_dirs,
    fetch_opencode_aaaa_pool,
    find_opencode_cli,
    find_opencode_desktop,
    find_warp_cli,
    get_egress_v6,
    get_warp_local_v6,
    hop_once,
    is_admin,
    is_cloudflare_v6,
    is_keeper_paused,
    keeper_pause_ctx,
    normalize_v6_64,
    run_hidden,
    run_warp,
    warp_status,
)
from ouc_firewall import install_killswitch, is_killswitch_installed, remove_killswitch

# Also how the app finds its own window: keep the UI <title> identical (the version is filled in there too).
WINDOW_TITLE = f"openhop {VERSION} · openhop.cfd"
WEBSITE = "https://openhop.cfd"
# Real end-to-end check: an actual request through the local proxy. Processes and ports can look fine
# while nothing gets in or out, which is what made the panel say "Everything is working" too often.
PROBE_URL = "https://opencode.ai/"
PROBE_DIRECT_URL = "https://cloudflare.com/cdn-cgi/trace"  # tiny, no-store; only to tell "offline" from "proxy broken"
PROBE_EVERY = 120.0
PROBE_TIMEOUT = 8.0
HEARTBEAT_STALE = 240.0
# Shim names used by Opencode Unlimited (before 3.0); deleted when found.
_OLD_SHIMS = ("opencode-unlimited-hop.cmd", "opencode-unlimited-doctor.cmd",
              "opencode-unlimited-keeper.cmd", "opencode-unlimited-guard.cmd")
DETACHED_PROCESS = 0x00000008
_WORKER_FILES = (
    "ouc_common.py",
    "ouc_firewall.py",
    "zen_proxy.py",
    "zen_lane.py",
    "warp_keeper.py",
    "zen_guard.py",
    "zen_hop.py",
    "doctor.py",
    "tor_manager.py",
    "proxy_chain.py",
    "vpn_detect.py",    # proxy + guard: VPN apps the user has (Norton, Proton VPN, ...)
    "wg_manager.py",    # keeper + guard + proxy: WireGuard files through wireproxy (Proton VPN Free, ...)
    "turbo.py",         # guard: openhop Turbo, the shared list of exits already used up today
    "chat_handoff.py",  # guard: a chat at the free limit continues in a new chat
    "ip_probe.py",      # guard: tests each new IP in its own hidden chat before sending 'continue'
    "clean_endpoint.py",
    "auto_resume.py",
    "desktop_brand.py",
    "opencode_extras.py",     # keeper: local helper for the extras inside OpenCode (voice typing, site icons)
    "opencode_extras_js.py",  # the script desktop_brand adds to OpenCode's window
    "explore.py",       # keeper helper: Explorar (installs shared text add-ons into OpenCode)
    "rank.py",          # explore.py publishes with the ranking key
    "notices.py",       # Zen Guard reads the maintenance mode from it
)
_SERVICE_ENV_KEYS = (
    "HTTPS_PROXY",
    "HTTP_PROXY",
    "https_proxy",
    "http_proxy",
    "NO_PROXY",
    "no_proxy",
)


def _enable_dark_titlebar() -> None:
    """Style the frameless window: dark mode, Windows 11 rounded corners, taskbar minimize."""
    try:
        hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if not hwnd:
            return
        try:
            GWL_STYLE, WS_MINIMIZEBOX, WS_SYSMENU = -16, 0x00020000, 0x00080000
            style = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_STYLE)
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_STYLE, style | WS_MINIMIZEBOX | WS_SYSMENU)
            corner = ctypes.c_int(2)  # DWMWCP_ROUND
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd), ctypes.c_uint(33), ctypes.byref(corner), ctypes.sizeof(corner)
            )
        except Exception:
            pass
        val = ctypes.c_int(1)
        for attr in (20, 19):
            ctypes.windll.dwmapi.DwmSetWindowAttribute(
                ctypes.c_void_p(hwnd),
                ctypes.c_uint(attr),
                ctypes.byref(val),
                ctypes.sizeof(val),
            )
    except Exception:
        pass


def _is_mutex_held(name: str) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(0x00100000, False, f"Local\\{name}")
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def free_service_port(port: int = 42914) -> int:
    """Kill any process holding the OpenCode managed service port to prevent IPC EADDRINUSE errors."""
    killed = 0
    try:
        import psutil
        for conn in psutil.net_connections(kind="tcp"):
            if conn.laddr and conn.laddr.port == port and conn.pid:
                try:
                    p = psutil.Process(conn.pid)
                    p.kill()
                    killed += 1
                except Exception:
                    pass
    except Exception:
        pass

    if sys.platform == "win32":
        try:
            out = subprocess.check_output(f'netstat -ano | findstr ":{port} "', shell=True, text=True, errors="ignore")
            for line in out.splitlines():
                parts = line.strip().split()
                if len(parts) >= 5 and "LISTENING" in parts:
                    pid = parts[-1]
                    if pid.isdigit() and int(pid) != os.getpid():
                        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True, creationflags=CREATE_NO_WINDOW)
                        killed += 1
        except Exception:
            pass
    return killed


def clear_opencode_free_limit() -> int:
    """Delete client-side free-limit and go-upsell lockout rows from drafts.sqlite."""
    candidates = [
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ai.opencode.desktop" / "drafts.sqlite",
        Path.home() / ".config" / "ai.opencode.desktop" / "drafts.sqlite",
    ]
    deleted = 0
    import sqlite3
    for db_path in candidates:
        if not db_path.is_file():
            continue
        try:
            conn = sqlite3.connect(str(db_path), timeout=3.0)
            cur = conn.cursor()
            cur.execute("DELETE FROM state WHERE key IN ('free-limit', 'go-upsell')")
            deleted += cur.rowcount
            cur.execute("DELETE FROM state WHERE key LIKE '%free%limit%'")
            deleted += cur.rowcount
            conn.commit()
            conn.close()
        except Exception:
            pass
    return deleted


def _task_exists(task_name: str) -> bool:
    try:
        res = run_hidden(["schtasks", "/Query", "/TN", task_name], timeout=5.0)
        return res.returncode == 0
    except Exception:
        return False


def _port_open(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return True
    except OSError:
        return False


def _tail_lines(path: Path, count: int = 75) -> list[str]:
    if not path.is_file():
        return []
    try:
        size = path.stat().st_size
        read_bytes = min(size, 131_072)
        with path.open("rb") as fh:
            if size > read_bytes:
                fh.seek(size - read_bytes)
            text = fh.read(read_bytes).decode("utf-8", errors="replace")
        lines = text.splitlines()
        return lines[-count:]
    except Exception:
        return []


def _read_db_stats() -> dict[str, int]:
    db_path = opencode_db_path()
    out = {"projects": 0, "sessions": 0, "messages": 0}
    if not db_path.is_file():
        return out
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
        try:
            for key, table in (("projects", "project"), ("sessions", "session_v2"), ("messages", "session_message")):
                row = conn.execute(f"SELECT count(*) FROM {table}").fetchone()
                if row:
                    out[key] = int(row[0])
        finally:
            conn.close()
    except Exception:
        pass
    return out


# Token usage card. `total` counts every token the model processed: input + output + cache reads +
# cache writes. Reasoning tokens are already reported inside `output` by most providers, so they are
# shown on their own but never added to the total (that would count them twice). Child/subagent
# sessions (parent_id set) are included: their tokens were really processed too, and excluding them
# would make the total smaller than what OpenCode actually did.
TOKEN_STATS_CACHE_SECONDS = 60.0


def _read_token_stats(db_path: Path | str | None = None) -> dict:
    """Read-only token totals from OpenCode's own database. Never raises: a missing, locked or
    old-schema database just returns ok=False, so the app always keeps working."""
    empty = {"ok": False, "total": 0, "input": 0, "output": 0, "cache_read": 0, "cache_write": 0,
             "reasoning": 0, "cost": 0.0, "sessions": 0, "projects": 0, "updated": 0}
    path = Path(db_path) if db_path else opencode_db_path()
    if not path.is_file():
        return empty
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=1.0)
        try:
            conn.execute("PRAGMA query_only = ON")  # belt and braces: this connection can only read
            row = conn.execute(
                "SELECT COUNT(*), COUNT(DISTINCT project_id), "
                "COALESCE(SUM(tokens_input), 0), COALESCE(SUM(tokens_output), 0), "
                "COALESCE(SUM(tokens_cache_read), 0), COALESCE(SUM(tokens_cache_write), 0), "
                "COALESCE(SUM(tokens_reasoning), 0), COALESCE(SUM(cost), 0), MAX(time_updated) "
                "FROM session_v2"
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return empty
    if not row:
        return empty
    sessions, projects, t_in, t_out, c_read, c_write, reasoning, cost, updated = row
    sessions = int(sessions or 0)
    if sessions <= 0:  # nothing to show yet: better an empty card than a row of zeros
        return empty
    updated_s = int(updated or 0)
    if updated_s > 10_000_000_000:  # OpenCode stores milliseconds; the rest of the app uses seconds
        updated_s //= 1000
    t_in, t_out = int(t_in or 0), int(t_out or 0)
    c_read, c_write = int(c_read or 0), int(c_write or 0)
    return {
        "ok": True,
        "total": t_in + t_out + c_read + c_write,
        "input": t_in,
        "output": t_out,
        "cache_read": c_read,
        "cache_write": c_write,
        "reasoning": int(reasoning or 0),
        "cost": round(float(cost or 0), 4),
        "sessions": sessions,
        "projects": int(projects or 0),
        "updated": updated_s,
    }


def _find_pythonw() -> str | None:
    if not getattr(sys, "frozen", False):
        exe = Path(sys.executable)
        pw = exe.with_name("pythonw.exe")
        return str(pw if pw.is_file() else exe)
    for cmd in ("pythonw.exe", "python.exe"):
        found = shutil.which(cmd)
        if found and "WindowsApps" not in found:
            return found
    return None


def _bundled_src_dir() -> Path:
    for candidate in (_BUNDLE / "src", _BUNDLE / "windows", _BUNDLE, Path(__file__).resolve().parent):
        if (candidate / "ouc_common.py").is_file():
            return candidate
    return _BUNDLE


def _deploy_scripts_and_shims() -> dict[str, str]:
    ensure_dirs()
    src_dir = _bundled_src_dir()
    for fname in _WORKER_FILES:
        src = src_dir / fname
        if src.is_file():
            shutil.copy2(src, SCRIPTS_DIR / fname)

    # Frozen: the shims must call openhop.exe (a user's PC may have no Python at all, and a shim pointing inside
    # PyInstaller's temp folder dies with the app). From source: the Python that is running this file.
    py_exe = None if getattr(sys, "frozen", False) else sys.executable

    if py_exe:
        pyw_exe = str(Path(py_exe).with_name("pythonw.exe"))
        if not Path(pyw_exe).is_file():
            pyw_exe = py_exe
        wrappers = {
            "opencode.cmd": f'@echo off\r\n"{py_exe}" "{SCRIPTS_DIR / "zen_lane.py"}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            "opencode-desktop.cmd": f'@echo off\r\nstart "" "{pyw_exe}" "{SCRIPTS_DIR / "zen_lane.py"}" --ouc-desktop %*\r\n',
            "openhop-hop.cmd": f'@echo off\r\n"{py_exe}" "{SCRIPTS_DIR / "zen_hop.py"}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            "openhop-doctor.cmd": f'@echo off\r\n"{py_exe}" "{SCRIPTS_DIR / "doctor.py"}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            "openhop-keeper.cmd": f'@echo off\r\n"{py_exe}" "{SCRIPTS_DIR / "warp_keeper.py"}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            "openhop-guard.cmd": f'@echo off\r\n"{py_exe}" "{SCRIPTS_DIR / "zen_guard.py"}" %*\r\nexit /b %ERRORLEVEL%\r\n',
            "openhop.cmd": f'@echo off\r\nstart "" "{pyw_exe}" "{Path(__file__).resolve()}" %*\r\n',
        }
        for name, content in wrappers.items():
            (BIN_DIR / name).write_text(content, encoding="ascii")
    elif getattr(sys, "frozen", False):
        exe = sys.executable
        wrappers = {
            "opencode.cmd": f'@echo off\r\n"{exe}" --lane %*\r\nexit /b %ERRORLEVEL%\r\n',
            "opencode-desktop.cmd": f'@echo off\r\nstart "" "{exe}" --lane --ouc-desktop %*\r\n',
            "openhop-hop.cmd": f'@echo off\r\n"{exe}" --hop %*\r\nexit /b %ERRORLEVEL%\r\n',
            "openhop-doctor.cmd": f'@echo off\r\n"{exe}" --doctor %*\r\nexit /b %ERRORLEVEL%\r\n',
            "openhop-keeper.cmd": f'@echo off\r\n"{exe}" --worker keeper\r\nexit /b %ERRORLEVEL%\r\n',
            "openhop-guard.cmd": f'@echo off\r\n"{exe}" --worker guard\r\nexit /b %ERRORLEVEL%\r\n',
            "openhop.cmd": f'@echo off\r\nstart "" "{exe}" %*\r\n',
        }
        for name, content in wrappers.items():
            (BIN_DIR / name).write_text(content, encoding="ascii")

    for old in _OLD_SHIMS:
        (BIN_DIR / old).unlink(missing_ok=True)
    return {"scripts_dir": str(SCRIPTS_DIR), "bin_dir": str(BIN_DIR)}


# ---------------------------------------------------------------------------
# Background worker lifecycle (keeper hosts the :41987 proxy, guard rotates on quota walls)
# ---------------------------------------------------------------------------
_WORKERS = {
    "keeper": ("warp_keeper.py", TASK_KEEPER, MUTEX_KEEPER),
    "guard": ("zen_guard.py", TASK_GUARD, MUTEX_GUARD),
}
_STAMP_FILE = DEST_DIR / "worker_version.txt"


def _disabled_flag(name: str) -> Path:
    return DEST_DIR / f"{name}.disabled"


def _bundle_fingerprint() -> str:
    """Hash of the worker code shipped with this build, used to detect stale deployed workers."""
    h = hashlib.sha256()
    src = _bundled_src_dir()
    for fname in _WORKER_FILES:
        fp = src / fname
        if fp.is_file():
            h.update(fname.encode())
            h.update(fp.read_bytes())
    if getattr(sys, "frozen", False):
        h.update(str(sys.executable).lower().encode())
    return h.hexdigest()[:20]


def _worker_procs(name: str) -> list:
    script = _WORKERS[name][0]
    me = os.getpid()
    found = []
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            if proc.info["pid"] == me:
                continue
            pname = (proc.info.get("name") or "").lower()
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
            if ("python" in pname and script in cmd) or f"--worker {name}" in cmd:
                found.append(proc)
        except Exception:
            pass
    return found


def _stop_worker_processes(name: str) -> int:
    killed = 0
    for proc in _worker_procs(name):
        try:
            proc.kill()
            killed += 1
        except Exception:
            pass
    for _ in range(20):
        if not _is_mutex_held(_WORKERS[name][2]):
            break
        time.sleep(0.15)
    return killed


def _start_worker(name: str, wait: float = 5.0) -> tuple[bool, str]:
    """Start a worker detached from this window (so it survives closing the UI) and verify it came up."""
    script, task_name, mutex = _WORKERS[name]
    if _is_mutex_held(mutex):
        return True, "already running"
    _deploy_scripts_and_shims()

    def _wait_up() -> bool:
        deadline = time.time() + wait
        while time.time() < deadline:
            if _is_mutex_held(mutex):
                return True
            time.sleep(0.25)
        return _is_mutex_held(mutex)

    if _task_exists(task_name):
        res = run_hidden(["schtasks", "/Run", "/TN", task_name], timeout=8.0)
        if res.returncode == 0 and _wait_up():
            return True, "Scheduled Task"

    pyw = _find_pythonw()
    if pyw and (SCRIPTS_DIR / script).is_file():
        subprocess.Popen(
            [pyw, str(SCRIPTS_DIR / script)],
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
        if _wait_up():
            return True, "background daemon"

    if getattr(sys, "frozen", False):
        subprocess.Popen(
            [sys.executable, "--worker", name],
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
        if _wait_up():
            return True, "bundled daemon"
    return False, f"could not start {name}"


# ---------------------------------------------------------------------------
# Plain-language error explanations
# ---------------------------------------------------------------------------
# (needles, code, title, explanation, fix action or None). First match wins.
_ERROR_RULES: tuple = (
    (("aborterror", "operation was aborted", "the user aborted"), None, "", "", None),
    (("name 'sys' is not defined",), "proxy_bug",
     "Proxy crashed (old bug, fixed in this version)",
     "An older copy of the background proxy had a bug. This version fixes it and restarts the background service with the new code automatically.",
     "restart_workers"),
    (("econnrefused", "connection refused"), "proxy_down",
     "OpenCode couldn't reach the local proxy",
     "Nothing was listening on 127.0.0.1:41987, usually because the background service was stopped or restarting. It now restarts itself within a few seconds.",
     "start_keeper"),
    (("strict fail-closed",), "strict_block",
     "Blocked by Strict Fail-Closed mode",
     "WARP was down and Strict mode refuses to send traffic without it. Reconnect WARP, or turn Strict mode off.",
     "repair_warp"),
    (("freeusagelimit", "free usage", "quotaexceeded", "quota exceeded", "rate limit", "rate_limit",
      "ratelimit", "too many requests", "status=429", "status: 429", "status 429", "code=429"), "rate_limited",
     "Free usage limit reached",
     "OpenCode's free limit was hit for this IP. Zen Guard rotates to a fresh IP automatically; press Fix to rotate right now.",
     "hop"),
    (("err_proxy_tunnel", "proxy refused to open a tunnel"), "tunnel_failed",
     "The proxy couldn't reach the AI server",
     "Your connection (WARP, custom proxy or internet) dropped while OpenCode was talking to the server. Press Fix to repair WARP, then send 'continue'.",
     "repair_warp"),
    (("enotfound", "getaddrinfo", "eai_again", "name or service not known"), "dns",
     "Couldn't look up the server address",
     "No internet connection, or DNS is blocked. Check your connection; press Fix to reconnect WARP.",
     "repair_warp"),
    (("eaddrinuse", "address already in use"), "port_busy",
     "OpenCode's service port is busy",
     "A stuck OpenCode background process is holding its port. Press Fix to clear it.",
     "fix_port"),
    (("econnreset", "socket hang up", "connection was closed", "other side closed", "connection reset"), "conn_dropped",
     "Connection dropped mid-reply",
     "The connection closed while the model was answering (often during an IP rotation). Just send 'continue' to resume.",
     None),
    (("etimedout", "timed out", "timeout"), "timeout",
     "The server took too long to respond",
     "The network is slow or WARP is reconnecting. Wait a few seconds and send 'continue'.",
     None),
)

# The same explanations in Spanish (code -> (title, explanation)).
_ERROR_RULES_ES = {
    "proxy_bug": ("El proxy se cayó (fallo antiguo, ya arreglado)",
                  "Una copia antigua del proxy en segundo plano tenía un fallo. Esta versión lo arregla y reinicia "
                  "el servicio en segundo plano con el código nuevo automáticamente."),
    "proxy_down": ("OpenCode no pudo llegar al proxy local",
                   "No había nada escuchando en 127.0.0.1:41987, normalmente porque el servicio en segundo plano estaba "
                   "parado o reiniciándose. Ahora se vuelve a poner en marcha solo en unos segundos."),
    "strict_block": ("Bloqueado por el modo estricto",
                     "WARP estaba caído y el modo estricto no deja salir nada sin él. Vuelve a conectar WARP o "
                     "desactiva el modo estricto."),
    "rate_limited": ("Límite de uso gratis alcanzado",
                     "Se ha llegado al límite gratis de OpenCode con esta IP. Zen Guard cambia a una IP nueva "
                     "automáticamente; pulsa Arreglar para cambiarla ahora mismo."),
    "tunnel_failed": ("El proxy no pudo llegar al servidor de IA",
                      "Tu conexión (WARP, tu proxy o internet) se cortó mientras OpenCode hablaba con el servidor. "
                      "Pulsa Arreglar para reparar WARP y luego envía 'continue'."),
    "dns": ("No se pudo encontrar la dirección del servidor",
            "No hay internet o el DNS está bloqueado. Revisa tu conexión; pulsa Arreglar para reconectar WARP."),
    "port_busy": ("El puerto del servicio de OpenCode está ocupado",
                  "Un proceso de OpenCode atascado en segundo plano tiene ocupado su puerto. Pulsa Arreglar para "
                  "liberarlo."),
    "conn_dropped": ("La conexión se cortó a mitad de respuesta",
                     "La conexión se cerró mientras el modelo respondía (a menudo durante un cambio de IP). "
                     "Envía 'continue' y seguirá."),
    "timeout": ("El servidor tardó demasiado en responder",
                "La red va lenta o WARP se está reconectando. Espera unos segundos y envía 'continue'."),
}
_WHERE_ES = {"chat": "chat", "model list refresh": "lista de modelos", "background": "segundo plano"}

_LOG_KV_RE = re.compile(r'(\w+)=("(?:[^"\\]|\\.)*"|\S+)')


def _explain_error(text: str) -> dict | None:
    low = text.lower()
    for needles, code, title, detail, fix in _ERROR_RULES:
        if any(n in low for n in needles):
            if code is None:
                return None
            if code in _ERROR_RULES_ES and ui_lang() == "es":
                title, detail = _ERROR_RULES_ES[code]
            return {"code": code, "title": title, "detail": detail, "fix": fix}
    first = text.split("\\n")[0].strip().strip('"')
    return {"code": "other", "title": L("OpenCode reported an error", "OpenCode avisó de un error"),
            "detail": first[:200], "fix": None}


def _recent_wall_times(window: float = 900.0) -> list[int]:
    """Timestamps of "free limit" errors in opencode.log during the last `window` seconds."""
    from datetime import datetime

    now = time.time()
    out: list[int] = []
    for line in _tail_lines(OPENCODE_LOG_FILE, 600):
        if "level=ERROR" not in line:
            continue
        kv = {k: v.strip('"') for k, v in _LOG_KV_RE.findall(line)}
        info = _explain_error(f"{kv.get('cause', '')} {kv.get('message', '')}")
        if not info or info.get("code") != "rate_limited":
            continue
        try:
            ts = int(datetime.fromisoformat(kv.get("timestamp", "").replace("Z", "+00:00")).timestamp())
        except Exception:
            continue
        if now - ts <= window:
            out.append(ts)
    return out


def _used_ips_summary(guard_state: dict, now: float | None = None) -> dict:
    """Numbers for the top of the "Used IPs" tab, from guard/state.json.

    tried_today: new addresses since the last daily reset (~01:05 UTC, when every address's free messages
    come back). used_up: addresses still marked used up. back_at: when the soonest of them comes back
    (epoch; the UI shows it in local time).
    """
    from ouc_common import next_free_reset
    now = time.time() if now is None else now
    day = int(next_free_reset(now))
    until: list[float] = []
    for v in (guard_state.get("burnt_until") or {}).values():
        try:
            if float(v or 0) > now:
                until.append(float(v))
        except (TypeError, ValueError):
            continue
    tried = int(guard_state.get("day_hops", 0) or 0) if int(guard_state.get("day_key", 0) or 0) == day else 0
    # Older guards didn't count: every address used up today was tried today, at least.
    tried = max(tried, sum(1 for v in until if int(v) == day))
    return {"tried_today": tried, "used_up": len(until), "back_at": int(min(until)) if until else 0}


# One "limit incident" = from the moment OpenCode hits the free limit until a new IP really works.
INCIDENT_QUIET = 60      # a new IP with no limit error for this long = fixed (re-walls come 13-30 s after a hop)
INCIDENT_DONE_SHOW = 600 # how long the panel keeps saying "fixed"


def step_incident(inc: dict, now: float, *, active: bool, rotating: bool, last_wall: float, last_hop: float,
                  guard_failed: int, backoff_until: float) -> dict:
    """Advance the limit-incident state. Pure function so it can be tested; returns the new state.

    phase: "none" | "fixing" | "checking" (new IP, waiting to see if it holds) | "paused" | "done".
    `active` is False when auto-rotation can't run anyway (guard off, direct mode); the health issues
    explain that case, so no incident is shown.
    """
    inc = dict(inc or {})
    phase = inc.get("phase", "none")
    ongoing = phase in ("fixing", "checking", "paused")
    limit_open = bool(last_wall) and last_wall >= (last_hop or 0) - 2 and now - last_wall < 180
    paused = bool(backoff_until and backoff_until > now)

    def start() -> None:
        inc.update(id=int(inc.get("id", 0) or 0) + 1, since=float(last_wall or now), tried=0, took=0, ended=0,
                   burnt_hop=0, resumed=0)

    if not active:
        if ongoing:
            inc["phase"] = "none"
        return inc
    new_ip = bool(last_hop) and last_hop >= float(inc.get("since", 0) or 0) - 2  # an IP taken during this incident
    if paused:
        if not ongoing:
            start()
        inc["phase"] = "paused"
        inc["retry_at"] = float(backoff_until)
    elif rotating or limit_open:
        if not ongoing:
            start()
            new_ip = False
        inc["phase"] = "fixing"
    elif phase == "paused":
        # The pause just ended: the guard takes its next address within a few seconds.
        inc.update(phase="fixing", resumed=now)
    elif ongoing:
        if new_ip and (not last_wall or last_wall < last_hop - 2):
            if now - last_hop < INCIDENT_QUIET:
                inc["phase"] = "checking"
            else:
                inc.update(phase="done", ended=now, took=max(0.0, last_hop - float(inc.get("since", last_hop))))
        elif now - float(inc.get("resumed", 0) or 0) < INCIDENT_QUIET:
            pass  # after a pause: give the guard a moment to rotate
        else:
            inc["phase"] = "none"  # the limit went quiet without a new IP (nothing was waiting): just drop it
    elif phase == "done" and now - float(inc.get("ended", 0) or 0) > INCIDENT_DONE_SHOW:
        inc["phase"] = "none"

    if inc.get("phase") in ("fixing", "checking", "paused"):
        # Count used-up addresses ourselves (a new IP followed by the limit again). The guard's own counter
        # still holds the previous incident's number at first, so it's only used for the pause (where it's
        # exact and we may have missed tries between polls).
        if new_ip and limit_open and last_hop != inc.get("burnt_hop"):
            inc["burnt_hop"] = last_hop
            inc["tried"] = int(inc.get("tried", 0) or 0) + 1
        if inc["phase"] == "paused":
            inc["tried"] = max(int(inc.get("tried", 0) or 0), int(guard_failed or 0))
    return inc


def _recent_opencode_errors(limit: int = 8) -> list[dict]:
    """Translate recent ERROR lines in opencode.log into short, deduplicated explanations."""
    from datetime import datetime

    events: list[dict] = []
    for line in _tail_lines(OPENCODE_LOG_FILE, 600):
        if "level=ERROR" not in line:
            continue
        kv = {k: v.strip('"') for k, v in _LOG_KV_RE.findall(line)}
        message = kv.get("message", "")
        info = _explain_error(f"{kv.get('cause', '')} {message}")
        if not info:
            continue
        ts = 0
        try:
            ts = int(datetime.fromisoformat(kv.get("timestamp", "").replace("Z", "+00:00")).timestamp())
        except Exception:
            pass
        low_msg = message.lower()
        where = "chat" if "session" in low_msg else ("model list refresh" if "models" in low_msg else "background")
        # merge repeats of the same error, but only when they're close together (a "×54" spanning hours misleads)
        if events and events[-1]["code"] == info["code"] and (not ts or not events[-1]["ts"] or ts - events[-1]["ts"] < 600):
            ev = events[-1]
            ev["count"] += 1
            ev["ts"] = ts or ev["ts"]
            if where == "chat":
                ev["where"] = "chat"
            continue
        events.append({**info, "ts": ts, "count": 1, "where": where})
    if ui_lang() == "es":
        for ev in events:
            ev["where"] = _WHERE_ES.get(ev["where"], ev["where"])
    return list(reversed(events))[:limit]


def _update_user_path(add: bool) -> bool:
    import winreg

    target = str(BIN_DIR).rstrip("\\")
    drop = {target.lower(), str(OLD_DEST_DIR / "bin").rstrip("\\").lower()}  # the old one is never kept
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ | winreg.KEY_WRITE) as key:
            try:
                cur, reg_type = winreg.QueryValueEx(key, "Path")
            except FileNotFoundError:
                cur, reg_type = "", winreg.REG_EXPAND_SZ
            parts = [p for p in str(cur).split(";") if p and p.rstrip("\\").lower() not in drop]
            if add:
                new_parts = [target, *parts]
            else:
                new_parts = parts
            winreg.SetValueEx(key, "Path", 0, reg_type, ";".join(new_parts))
        return True
    except Exception:
        return False


def _user_path_has_bin() -> bool:
    import winreg

    target = str(BIN_DIR).rstrip("\\").lower()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ) as key:
            cur, _ = winreg.QueryValueEx(key, "Path")
            parts = [p.rstrip("\\").lower() for p in str(cur).split(";") if p]
            return bool(parts and parts[0] == target)
    except Exception:
        return False


def _settings_intact() -> tuple[bool, str]:
    """True when the saved settings still load (the post-update check reports a damaged file honestly)."""
    path = DEST_DIR / "app_settings.json"
    if not path.is_file():
        return True, L("No saved settings yet; the defaults are fine.",
                       "Aún no hay ajustes guardados; los de serie están bien.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return True, L("Saved settings still load.", "Los ajustes guardados se siguen cargando.")
    except Exception:
        pass
    return False, L("The settings file couldn't be read, so openhop is using defaults.",
                    "No se pudo leer el archivo de ajustes, así que openhop usa los de serie.")


def _heartbeat_age(path: Path) -> float | None:
    """Seconds since the worker last wrote its heartbeat, or None if it never did."""
    try:
        return max(0.0, time.time() - path.stat().st_mtime)
    except OSError:
        return None


def _probe_http(url: str, proxy: str | None, timeout: float = PROBE_TIMEOUT) -> tuple[bool, str]:
    """Make one real HTTPS request. Returns (reached, stage).

    Any HTTP answer (even 403/429) proves DNS, TCP, TLS and the tunnel work; only transport errors
    count as failures. `stage` is one of: ok, proxy, timeout, upstream.
    """
    handlers = [urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})]
    opener = urllib.request.build_opener(*handlers)
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"openhop-probe/{VERSION}",
            "Cache-Control": "no-cache",
            "Accept": "*/*",
        },
    )
    try:
        with opener.open(req, timeout=timeout) as resp:
            resp.read(64)
        return True, "ok"
    except urllib.error.HTTPError as exc:
        try:
            exc.close()
        except Exception:
            pass
        # 407/502/503/504 come from the proxy or its upstream, not from the far end.
        return (False, "upstream") if exc.code in (407, 502, 503, 504) else (True, "ok")
    except Exception as exc:
        text = str(getattr(exc, "reason", exc)).lower()
        if "connection refused" in text or "errno 10061" in text or "actively refused" in text:
            return False, "proxy"
        if "timed out" in text or "timeout" in text:
            return False, "timeout"
        return False, "upstream"


# ---------------------------------------------------------------------------
# Custom window frame (frameless window + HTML title bar). The native move/resize loop can't
# be used because WebView2 owns the mouse, so the page drives it and we read the real cursor.
# ---------------------------------------------------------------------------
class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", _RECT), ("rcWork", _RECT), ("dwFlags", ctypes.c_ulong)]


# Private user32 instance: setting argtypes on the shared ctypes.windll.user32 broke the tray menu
# (other modules pass their own RECT class to GetWindowRect).
_user32 = ctypes.WinDLL("user32") if sys.platform == "win32" else None
if _user32 is not None:
    _user32.MonitorFromWindow.restype = ctypes.c_void_p
    _user32.MonitorFromWindow.argtypes = (ctypes.c_void_p, ctypes.c_ulong)
    _user32.GetMonitorInfoW.argtypes = (ctypes.c_void_p, ctypes.POINTER(_MONITORINFO))
    _user32.SetWindowPos.argtypes = (
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_uint,
    )
    _user32.GetWindowRect.argtypes = (ctypes.c_void_p, ctypes.POINTER(_RECT))
_MIN_W, _MIN_H = 980, 680


def _find_hwnd() -> int:
    return int(_user32.FindWindowW(None, WINDOW_TITLE) or 0)


def _win_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = _RECT()
    _user32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _cursor_pos() -> tuple[int, int]:
    p = _POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return p.x, p.y


def _work_area(hwnd: int) -> tuple[int, int, int, int]:
    mon = _user32.MonitorFromWindow(ctypes.c_void_p(hwnd), 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    _user32.GetMonitorInfoW(mon, ctypes.byref(mi))
    w = mi.rcWork
    return w.left, w.top, w.right, w.bottom


def _dpi_scale(hwnd: int) -> float:
    try:
        return max(1.0, _user32.GetDpiForWindow(ctypes.c_void_p(hwnd)) / 96.0)
    except Exception:
        return 1.0


def _set_bounds(hwnd: int, x: int, y: int, w: int, h: int) -> None:
    _user32.SetWindowPos(ctypes.c_void_p(hwnd), None, int(x), int(y), int(w), int(h), 0x0004 | 0x0010)


def _set_corners(hwnd: int, rounded: bool) -> None:
    try:
        val = ctypes.c_int(2 if rounded else 1)  # DWMWCP_ROUND / DWMWCP_DONOTROUND (Windows 11)
        ctypes.windll.dwmapi.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), ctypes.c_uint(33), ctypes.byref(val), ctypes.sizeof(val))
    except Exception:
        pass


class _WindowFrame:
    """Window controls exposed to the HTML title bar (minimize / maximize / close / move / resize)."""

    def __init__(self) -> None:
        self._hwnd = 0
        self._maximized = False
        self._restore_rect: tuple[int, int, int, int] | None = None
        self._drag: dict | None = None
        self._lock = threading.Lock()

    def hwnd(self) -> int:
        if not self._hwnd or not _user32.IsWindow(ctypes.c_void_p(self._hwnd)):
            self._hwnd = _find_hwnd()
        return self._hwnd

    def minimize(self) -> dict:
        _user32.ShowWindow(ctypes.c_void_p(self.hwnd()), 6)  # SW_MINIMIZE
        return {"maximized": self._maximized}

    def _maximize(self, hwnd: int) -> None:
        self._restore_rect = _win_rect(hwnd)
        l, t, r, b = _work_area(hwnd)
        _set_corners(hwnd, False)
        _set_bounds(hwnd, l, t, r - l, b - t)
        self._maximized = True

    def _restore(self, hwnd: int) -> None:
        if self._restore_rect:
            l, t, r, b = self._restore_rect
            _set_bounds(hwnd, l, t, r - l, b - t)
        _set_corners(hwnd, True)
        self._maximized = False

    def toggle_maximize(self) -> dict:
        hwnd = self.hwnd()
        with self._lock:
            if self._maximized:
                self._restore(hwnd)
            else:
                self._maximize(hwnd)
        return {"maximized": self._maximized}

    def begin(self, kind: str) -> dict:
        hwnd = self.hwnd()
        with self._lock:
            cx, cy = _cursor_pos()
            rect = _win_rect(hwnd)
            if kind == "move" and self._maximized:
                # Dragging a maximized window restores it under the cursor (like native windows).
                rl, rt, rr, rb = self._restore_rect or (rect[0], rect[1], rect[0] + 1180, rect[1] + 790)
                w, h = rr - rl, rb - rt
                frac = (cx - rect[0]) / max(1, rect[2] - rect[0])
                nx, ny = cx - int(w * frac), cy - int(20 * _dpi_scale(hwnd))
                _set_corners(hwnd, True)
                _set_bounds(hwnd, nx, ny, w, h)
                self._maximized = False
                rect = (nx, ny, nx + w, ny + h)
            elif self._maximized:
                return {"maximized": True}
            self._drag = {"kind": kind, "cursor": (cx, cy), "rect": rect}
        return {"maximized": self._maximized}

    def step(self) -> None:
        d = self._drag
        if not d:
            return
        hwnd = self.hwnd()
        cx, cy = _cursor_pos()
        dx, dy = cx - d["cursor"][0], cy - d["cursor"][1]
        l, t, r, b = d["rect"]
        kind = d["kind"]
        if kind == "move":
            _set_bounds(hwnd, l + dx, t + dy, r - l, b - t)
            return
        s = _dpi_scale(hwnd)
        min_w, min_h = int(_MIN_W * s), int(_MIN_H * s)
        nl, nt, nr, nb = l, t, r, b
        if "e" in kind:
            nr = max(r + dx, l + min_w)
        if "w" in kind:
            nl = min(l + dx, r - min_w)
        if "s" in kind:
            nb = max(b + dy, t + min_h)
        if "n" in kind:
            nt = min(t + dy, b - min_h)
        _set_bounds(hwnd, nl, nt, nr - nl, nb - nt)

    def end(self) -> dict:
        d, self._drag = self._drag, None
        if d and d["kind"] == "move":
            hwnd = self.hwnd()
            _, cy = _cursor_pos()
            if cy <= _work_area(hwnd)[1] + 2:  # dropped on the top edge: snap to maximized
                with self._lock:
                    self._maximize(hwnd)
        return {"maximized": self._maximized}


class AvatarControlApi:
    """Backend controller exposed to the Avatar WebView2 UI."""

    def __init__(self) -> None:
        self._migration: dict = {}
        try:
            from ouc_migrate import migrate, needs_migration
            if needs_migration():
                self._migration = migrate()
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "migration.log", f"migration failed: {exc}")
        ensure_dirs()
        self._egress_lock = threading.Lock()
        self._egress_v6 = ""
        self._egress_checking = False
        self._egress_failed = False
        self._last_egress_ts = 0.0
        self._probe_lock = threading.Lock()
        self._probe: dict = {"ok": None, "code": "unknown", "ms": 0, "ts": 0}
        self._probe_checking = False
        self._probe_ts = 0.0
        self._last_local_v6 = ""
        self._edge_pool: list[str] = []
        self._action_lock = threading.Lock()
        self._startup_note = ""
        self._incident: dict = {"phase": "none", "id": 0}
        self._incident_lock = threading.Lock()
        self._fallback_proxy = None
        self._frame = _WindowFrame()
        self._window = None          # set by main()
        self._tray = None            # openhop_tray.Tray, set by main()
        self._last_open_notice = 0.0 # when we last showed "update ready" because the window was opened
        self._quitting = False
        self._state_cache: tuple[float, dict] | None = None
        self._state_lock = threading.Lock()
        self._token_cache: tuple[float, dict] | None = None
        self._token_lock = threading.Lock()
        threading.Thread(target=self._startup_sync, daemon=True).start()
        self._refresh_egress_async(force=True)
        self._refresh_probe_async(force=True)
        self._refresh_pool_async()
        self._auto_heal_warp_startup()

    # ---- custom title bar (called from the HTML frame) ----
    def win_minimize(self) -> dict:
        return self._frame.minimize()

    def win_toggle_maximize(self) -> dict:
        return self._frame.toggle_maximize()

    def win_close(self) -> dict:
        if self.hide_to_tray():
            return {"ok": True, "hidden": True}
        self.quit_app()
        return {"ok": True}

    # ---- tray / window lifetime ----
    def hide_to_tray(self) -> bool:
        """Hide the window instead of closing it (when the tray is running and the user wants that)."""
        from openhop_tray import load_settings, save_setting
        if self._quitting or not (self._tray and self._tray.alive) or not self._window:
            return False
        if not load_settings()["close_to_tray"]:
            return False
        try:
            self._window.hide()
        except Exception:
            return False
        if not load_settings()["tray_tip_shown"]:
            save_setting("tray_tip_shown", True)
            self._tray.notify("info", L("openhop is still running", "openhop sigue funcionando"),
                              L("It keeps OpenCode working from the tray (by the clock). "
                                "Right-click the icon to quit.",
                                "Mantiene OpenCode funcionando desde la bandeja (junto al reloj). "
                                "Haz clic derecho en el icono para salir."), force=True)
        return True

    def show_window(self) -> None:
        w = self._window
        if not w:
            return
        self._on_window_opened()
        try:
            w.show()
            w.restore()
        except Exception:
            pass
        try:
            hwnd = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
            if hwnd:
                ctypes.windll.user32.ShowWindow(ctypes.c_void_p(hwnd), 9)  # SW_RESTORE
                ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
        except Exception:
            pass

    def quit_app(self) -> None:
        """Really close openhop (window + tray). Background protection keeps running."""
        self._quitting = True
        if self._tray:
            self._tray.stop()
        for w in list(webview.windows):
            try:
                w.destroy()
            except Exception:
                pass

    def _on_closing(self) -> bool:
        """Alt+F4 / taskbar close: hide to tray instead (returning False cancels the close)."""
        if self._quitting:
            return True
        return not self.hide_to_tray()

    # ---- app settings (tray, notifications, start with Windows) ----
    def get_app_settings(self) -> dict:
        from openhop_tray import autostart_enabled, load_settings
        data = load_settings()
        data["start_with_windows"] = autostart_enabled()
        data["tray_available"] = bool(self._tray and self._tray.alive)
        data["ui_lang"] = ui_lang()
        return data

    def _chat_moves_state(self) -> list:
        """Chats openhop moved to a new chat in the last 30 min (OpenCode limits each chat): for the tray notice."""
        try:
            import chat_handoff
            return chat_handoff.recent_moves()
        except Exception:
            return []

    def _turbo_state(self) -> dict:
        """openhop Turbo for the UI: {enabled, state: on|paused|starting|off, listed, skipped_today, ...}."""
        try:
            import turbo
            return turbo.status()
        except Exception:
            return {"enabled": False, "state": "off"}

    def set_app_setting(self, key: str, value) -> dict:
        from openhop_tray import save_setting, set_autostart
        try:
            if key == "start_with_windows":
                ok = set_autostart(bool(value))
                return {"ok": ok, "message": (L("openhop will start with Windows (in the tray).",
                                                "openhop se abrirá con Windows (en la bandeja).") if value
                                              else L("openhop won't start with Windows.",
                                                     "openhop ya no se abrirá con Windows.")) if ok
                        else L("Couldn't change the Windows startup setting.",
                               "No se pudo cambiar el inicio con Windows.")}
            if key == "language":
                if value not in LANGUAGES:
                    raise KeyError(key)
                save_setting(key, value)
                self._state_cache = None  # the next poll builds every text in the new language
                return {"ok": True, "language": value, "ui_lang": ui_lang(),
                        "message": L("Language changed.", "Idioma cambiado.")}
            save_setting(key, value)
            if key == "desktop_brand":
                return self._sync_desktop_brand(bool(value))
            if key == "hide_limit_banner":
                return self._sync_limit_banner()
            if key == "opencode_extras":
                return self._sync_extras()
            return {"ok": True, "message": L("Saved.", "Guardado.")}
        except KeyError:
            return {"ok": False, "message": L(f"Unknown setting: {key}", f"Ajuste desconocido: {key}")}

    def _sync_desktop_brand(self, want: bool) -> dict:
        try:
            import desktop_brand
            st = desktop_brand.status()
            if not st.get("installed"):
                return {"ok": True, "message": L("Saved. OpenCode Desktop isn't installed, so there's nothing to change.",
                                                 "Guardado. OpenCode Desktop no está instalado, así que no hay nada que cambiar.")}
            ok, msg = desktop_brand.apply() if want else desktop_brand.restore()
            if not ok and st.get("running"):
                msg = L("Saved. Close OpenCode Desktop and openhop will " +
                        ("add openhop.cfd" if want else "put OpenCode's own word back") + " within a couple of minutes.",
                        "Guardado. Cierra OpenCode Desktop y openhop " +
                        ("pondrá openhop.cfd" if want else "devolverá la palabra de OpenCode") + " en un par de minutos.")
                ok = True
            return {"ok": ok, "message": msg}
        except Exception:
            return {"ok": True, "message": L("Saved. openhop will update OpenCode in the background.",
                                             "Guardado. openhop actualizará OpenCode en segundo plano.")}

    def _sync_extras(self) -> dict:
        """Add or take out the extras script in OpenCode Desktop now if it's closed (else the keeper does it later)."""
        try:
            import desktop_brand
            st = desktop_brand.status()
            if not st.get("installed"):
                return {"ok": True, "message": L("Saved. OpenCode Desktop isn't installed, so there's nothing to change.",
                                                 "Guardado. OpenCode Desktop no está instalado, así que no hay nada que cambiar.")}
            if st.get("running"):
                return {"ok": True, "message": L("Saved. Close OpenCode Desktop and openhop will change it within "
                                                 "a couple of minutes.",
                                                 "Guardado. Cierra OpenCode Desktop y openhop lo cambiará en un par "
                                                 "de minutos.")}
            desktop_brand.sync()
            return {"ok": True, "message": L("Saved. It applies the next time OpenCode Desktop opens.",
                                             "Guardado. Se aplica la próxima vez que abras OpenCode Desktop.")}
        except Exception:
            return {"ok": True, "message": L("Saved. openhop will update OpenCode in the background.",
                                             "Guardado. openhop actualizará OpenCode en segundo plano.")}

    def _voice_call(self, path: str, method: str = "GET") -> dict:
        try:
            import opencode_extras
            return opencode_extras.call(path, method)
        except Exception:
            return {"ok": False, "error": "no_keeper"}

    def voice_status(self) -> dict:
        return self._voice_call("/voice/status")

    def voice_install(self) -> dict:
        return self._voice_call("/voice/install", "POST")

    def voice_remove(self) -> dict:
        return self._voice_call("/voice/remove", "POST")

    def _sync_limit_banner(self) -> dict:
        """Hide or bring back OpenCode Desktop's free-limit card now, not in the keeper's next pass."""
        try:
            import desktop_brand
            st = desktop_brand.status()
            if not st.get("installed"):
                return {"ok": True, "message": L("Saved. OpenCode Desktop isn't installed, so there's nothing to change.",
                                                 "Guardado. OpenCode Desktop no está instalado, así que no hay nada que cambiar.")}
            if not st.get("banner_supported"):
                return {"ok": True, "message": L("Saved. This OpenCode version doesn't show that card.",
                                                 "Guardado. Esta versión de OpenCode no muestra ese aviso.")}
            if st.get("running"):
                return {"ok": True, "message": L("Saved. Close OpenCode Desktop and openhop will change it within "
                                                 "a couple of minutes.",
                                                 "Guardado. Cierra OpenCode Desktop y openhop lo cambiará en un par "
                                                 "de minutos.")}
            desktop_brand.sync()
            return {"ok": True, "message": L("Saved. It applies the next time OpenCode Desktop opens.",
                                             "Guardado. Se aplica la próxima vez que abras OpenCode Desktop.")}
        except Exception:
            return {"ok": True, "message": L("Saved. openhop will update OpenCode in the background.",
                                             "Guardado. openhop actualizará OpenCode en segundo plano.")}

    # ---- automatic updates (see updater.py) ----
    def _on_window_opened(self) -> None:
        """The window was opened (tray, or openhop started again while it was running in the background).

        If a new version is waiting, say so right away, even if the "ready" popup was shown yesterday.
        """
        now = time.time()
        if now - self._last_open_notice < 120:
            return
        self._last_open_notice = now

        def run() -> None:
            try:
                import updater
                from openhop_tray import load_settings
                settings = load_settings()
                if not settings.get("notify_updates", True):
                    return
                s = updater.state()
                if not s.get("ready") and settings.get("auto_update", True):
                    s = {**s, **updater.check(download=True)}   # small request to openhop.cfd
                    s = {**s, **updater.state()}
                if not (s.get("ready") and s.get("can_install")):
                    return
                time.sleep(1.2)   # let the window appear first
                if self._tray and self._tray.alive:
                    self._tray.notify("update", L(f"openhop {s.get('latest', '')} is ready",
                                                  f"openhop {s.get('latest', '')} está listo"),
                                      L("Click to see it. Update now takes a few seconds; protection keeps running.",
                                        "Haz clic para verlo. Actualizar tarda unos segundos; la protección sigue activa."),
                                      action_label=L("Update now", "Actualizar"), action="update", force=True,
                                      body_action="show_update")
                self._focus_update_bar(scroll=False)
            except Exception as exc:
                append_log(DEST_DIR / "logs" / "update.log", f"update notice on open failed: {exc}")

        threading.Thread(target=run, daemon=True, name="openhop-open-notice").start()

    def _focus_update_bar(self, scroll: bool = True) -> None:
        w = self._window
        if not w:
            return
        try:
            w.evaluate_js(f"window.focusUpdate && window.focusUpdate({'true' if scroll else 'false'})")
        except Exception:
            pass

    def show_update(self) -> dict:
        """Clicked the update popup: bring the window up and point at the Update now button."""
        self._last_open_notice = time.time()   # don't pop the same notice again for this open
        self.show_window()
        threading.Timer(0.4, self._focus_update_bar).start()
        return {"ok": True}

    def _watch_open_requests(self) -> None:
        """openhop.exe started again while this one runs in the background: treat it as 'window opened'."""
        flag = DEST_DIR / "run" / "open_request"

        def loop() -> None:
            try:
                seen = flag.stat().st_mtime if flag.is_file() else 0.0
            except OSError:
                seen = 0.0
            while not self._quitting:
                time.sleep(1.0)
                try:
                    m = flag.stat().st_mtime if flag.is_file() else 0.0
                except OSError:
                    continue
                if m > seen:
                    seen = m
                    self.show_window()

        threading.Thread(target=loop, daemon=True, name="openhop-open-requests").start()

    def _start_update_checks(self) -> None:
        try:
            import updater
            from openhop_tray import load_settings

            def ready(s: dict) -> None:
                if self._tray and self._tray.alive and load_settings().get("notify_updates", True):
                    self._tray.notify("update", L(f"openhop {s.get('latest', '')} is ready",
                                                  f"openhop {s.get('latest', '')} está listo"),
                                      L("Press Update now, or it finishes when openhop restarts. It takes a few seconds.",
                                        "Pulsa Actualizar, o se termina cuando openhop se reinicie. Tarda unos segundos."),
                                      action_label=L("Update now", "Actualizar"), action="update", force=True,
                                      body_action="show_update")

            updater.start_background(on_ready=ready, enabled=lambda: load_settings().get("auto_update", True))
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "update.log", f"update checks not started: {exc}")

    # ---- announcements and maintenance (set by the openhop dev team; see notices.py)
    def get_notices(self) -> dict:
        try:
            import notices
            return notices.current()
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "notices.log", f"get_notices failed: {exc}")
            return {"maintenance": {"mode": "off"}, "notices": [], "lang": ui_lang()}

    def dismiss_notice(self, notice_id: str) -> dict:
        import notices
        notices.dismiss(str(notice_id))
        return {"ok": True}

    def maintenance_continue(self) -> dict:
        import notices
        return {"ok": notices.continue_anyway()}

    def open_link(self, url: str) -> dict:
        """Links from notices: https only (they come from the server, so never trust them blindly)."""
        url = str(url or "")
        if not url.startswith("https://") or len(url) > 300 or any(c in url for c in " \"'<>\\`"):
            return {"ok": False, "message": L("That link isn't allowed.", "Ese enlace no está permitido.")}
        webbrowser.open(url)
        return {"ok": True}

    def _start_notice_checks(self) -> None:
        try:
            import notices
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "notices.log", f"notices unavailable: {exc}")
            return
        seen = {"mode": None}

        def changed(cur: dict) -> None:
            w = self._window
            if w:
                try:
                    w.evaluate_js("window.onNotices && window.onNotices()")
                except Exception:
                    pass
            m = cur.get("maintenance") or {}
            mode = m.get("mode", "off")
            if seen["mode"] is not None and mode != seen["mode"] and self._tray and self._tray.alive:
                if mode != "off":
                    self._tray.notify("problem", m.get("title") or L("openhop is in maintenance",
                                                                     "openhop está en mantenimiento"),
                                      m.get("message") or L("Open openhop to see the details.",
                                                            "Abre openhop para ver los detalles."), force=True)
                else:
                    self._tray.notify("info", L("Maintenance is over", "El mantenimiento ha terminado"),
                                      L("openhop works normally again.", "openhop vuelve a funcionar con normalidad."),
                                      force=True)
            seen["mode"] = mode

        notices.start_background(on_change=changed)

    # ---------------------------------------------------------------- ranking (src/rank.py; opt-in, nothing is sent before joining)
    def _rank(self):
        import rank
        return rank

    def rank_status(self) -> dict:
        try:
            return {"ok": True, **self._rank().status()}
        except Exception as exc:
            return {"ok": False, "code": "error", "message": str(exc)[:200]}

    def _rank_call(self, fn, *args) -> dict:
        try:
            r = fn(*args)
            return r if isinstance(r, dict) else {"ok": False, "code": "error"}
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "rank.log", f"rank error: {exc}")
            return {"ok": False, "code": "error", "message": str(exc)[:200]}

    def rank_join(self, handle: str) -> dict:
        return self._rank_call(self._rank().join, str(handle or ""))

    def rank_board(self, kind: str = "nocache", period: str = "week") -> dict:
        return self._rank_call(self._rank().board, str(kind or ""), str(period or ""))

    def rank_me(self) -> dict:
        return self._rank_call(self._rank().me)

    def rank_link_start(self) -> dict:
        return self._rank_call(self._rank().link_start)

    def rank_link_claim(self, code: str) -> dict:
        return self._rank_call(self._rank().link_claim, str(code or ""))

    def rank_set_handle(self, handle: str) -> dict:
        return self._rank_call(self._rank().set_handle, str(handle or ""))

    def rank_remove_device(self, device_id: str) -> dict:
        return self._rank_call(self._rank().remove_device, str(device_id or ""))

    def rank_leave(self) -> dict:
        return self._rank_call(self._rank().leave)

    def rank_pause(self, paused: bool) -> dict:
        return self._rank_call(self._rank().set_paused, bool(paused))

    def rank_sync(self) -> dict:
        return self._rank_call(self._rank().sync, True)

    def rank_stats(self) -> dict:
        return self._rank_call(self._rank().fun_stats)

    def _start_rank_sync(self) -> None:
        try:
            self._rank().start_background()
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "rank.log", f"ranking unavailable: {exc}")

    def _start_post_update_checks(self) -> None:
        """After an update, prove the new version works and tell the user how it went."""
        try:
            import updater
            if not updater.just_updated_pending():
                return
        except Exception:
            return

        def run() -> None:
            # Give the workers time to deploy and start: judge the new version once the proxy answers,
            # or after 20 seconds at the latest (a real problem should still be reported).
            deadline = time.time() + 20
            while time.time() < deadline and not _port_open(SERVICE_PROXY_PORT):
                time.sleep(1)
            time.sleep(3)
            try:
                self.run_post_update_checks(notify=True)
            except Exception as exc:
                append_log(DEST_DIR / "logs" / "update.log", f"post-update checks couldn't run: {exc}")

        threading.Thread(target=run, daemon=True, name="openhop-post-update").start()

    def run_post_update_checks(self, notify: bool = False) -> dict:
        """A short, honest pass after an update: new version, right file, settings, proxy, workers, connection."""
        import updater
        last = updater.post_update_report()
        marker = updater.take_just_updated()
        was_update = bool(marker)
        previous = str(marker.get("from") or last.get("from") or "")
        target = str(marker.get("to") or VERSION)
        checks: list[dict] = []

        def add(key: str, label: str, ok: bool, detail: str = "") -> None:
            checks.append({"key": key, "label": label, "ok": bool(ok), "detail": detail})

        # --- the update itself ---
        add("version", L("The new version is running", "La versión nueva está funcionando"), VERSION == target,
            f"openhop {VERSION}")
        if was_update:
            if not updater.frozen():
                add("file", L("The downloaded file is the one running", "El archivo descargado es el que funciona"), True,
                    L("Running from source, so there's nothing to swap.",
                      "Funciona desde el código fuente, así que no hay nada que cambiar."))
            else:
                want = str(marker.get("sha256") or "")
                got = updater.own_sha()
                good = bool(want) and got == want
                add("file", L("The downloaded file is the one running", "El archivo descargado es el que funciona"), good,
                    L("It matches the checksum from openhop.cfd.", "Coincide con la suma de comprobación de openhop.cfd.")
                    if good else L("The file doesn't match the checksum from openhop.cfd.",
                                   "El archivo no coincide con la suma de comprobación de openhop.cfd."))
        ok_settings, settings_detail = _settings_intact()
        add("settings", L("Your settings are still here", "Tus ajustes siguen aquí"), ok_settings, settings_detail)

        # --- everything around it ---
        st = self.get_state()
        svc = st.get("services") or {}
        warp = st.get("warp") or {}
        health = st.get("health") or {}
        proxy = bool(svc.get("proxy_listening"))
        add("proxy", L("The local proxy answers", "El proxy local responde"), proxy,
            L(f"Port {SERVICE_PROXY_PORT} is open.", f"El puerto {SERVICE_PROXY_PORT} está abierto.") if proxy
            else L(f"Port {SERVICE_PROXY_PORT} isn't open yet.", f"El puerto {SERVICE_PROXY_PORT} aún no está abierto."))
        keeper = bool(svc.get("keeper_running"))
        add("keeper", L("Background protection is running", "La protección en segundo plano está activa"), keeper,
            L("It keeps the proxy up.", "Mantiene el proxy en marcha.") if keeper
            else L("The keeper isn't running.", "WARP Keeper no está funcionando."))
        guard = bool(svc.get("guard_running"))
        add("guard", L("Automatic IP rotation is running", "El cambio automático de IP está activo"), guard,
            L("It watches OpenCode's log for the limit.", "Vigila el registro de OpenCode por si llega el límite.")
            if guard else L("It isn't running.", "No está funcionando."))
        try:
            synced = (_STAMP_FILE.is_file()
                      and _STAMP_FILE.read_text(encoding="utf-8").strip() == _bundle_fingerprint())
        except Exception:
            synced = False
        add("workers", L("Background workers run the new code", "Los procesos en segundo plano usan el código nuevo"),
            synced, L("They were restarted with this version.", "Se reiniciaron con esta versión.") if synced
            else L("They haven't picked up the new version yet.", "Todavía no han cogido la versión nueva."))
        bad = [i.get("title", "") for i in health.get("issues", []) if i.get("level") == "bad"]
        add("connection", L("OpenCode can connect", "OpenCode puede conectarse"), not bad,
            str(warp.get("status_text") or L("Connected.", "Conectado.")) if not bad else bad[0])

        passed = sum(1 for c in checks if c["ok"])
        report = {
            "at": int(time.time()),
            "from": previous,
            "to": target,
            "version": VERSION,
            "ok": passed == len(checks),
            "passed": passed,
            "total": len(checks),
            "checks": checks,
        }
        updater.save_post_update_report(report)
        if notify and self._tray and self._tray.alive:
            try:
                from openhop_tray import load_settings
                if load_settings().get("notify_updates", True):
                    if report["ok"]:
                        self._tray.notify("info", L(f"Updated to openhop {VERSION}", f"Actualizado a openhop {VERSION}"),
                                          L(f"All {passed} checks passed. Everything is working.",
                                            f"Las {passed} comprobaciones han ido bien. Todo funciona."),
                                          action_label=L("Open", "Abrir"), action="open", force=True)
                    else:
                        first = next((c["label"] for c in checks if not c["ok"]), L("something", "algo"))
                        self._tray.notify("problem", L(f"Updated to openhop {VERSION}", f"Actualizado a openhop {VERSION}"),
                                          L(f"{passed} of {len(checks)} checks passed. Needs a look: ",
                                            f"{passed} de {len(checks)} comprobaciones bien. Hay que revisar: ")
                                          + f"{first.lower()}.",
                                          action_label=L("Open", "Abrir"), action="open", force=True)
            except Exception:
                pass
        return report

    def get_update_state(self) -> dict:
        try:
            import updater
            return updater.state()
        except Exception as exc:
            return {"available": False, "ready": False, "error": str(exc), "current": VERSION}

    def check_update(self) -> dict:
        try:
            import updater
            s = updater.check(download=True)
        except Exception as exc:
            return {"ok": False, "message": L(f"Couldn't check for updates: {exc}",
                                              f"No se pudo buscar actualizaciones: {exc}")}
        if s.get("error"):
            return {"ok": False, "message": L("Couldn't reach openhop.cfd to check for updates. Try again later.",
                                              "No se pudo contactar con openhop.cfd para buscar actualizaciones. "
                                              "Prueba más tarde."), **s}
        if s.get("ready"):
            return {"ok": True, "message": L(f"openhop {s.get('latest')} is ready. Press Update now.",
                                             f"openhop {s.get('latest')} está listo. Pulsa Actualizar."), **s}
        if s.get("available"):
            return {"ok": True, "message": L(f"openhop {s.get('latest')} is out: download it from openhop.cfd.",
                                             f"Ya ha salido openhop {s.get('latest')}: descárgalo de openhop.cfd."), **s}
        return {"ok": True, "message": L(f"You have the latest version ({VERSION}).",
                                         f"Tienes la última versión ({VERSION})."), **s}

    def apply_update(self) -> dict:
        try:
            import updater
            # --updated tells the new copy that the window it may still see is the one being replaced.
            ok, msg = updater.install(["--updated"])
        except Exception as exc:
            return {"ok": False, "message": L(f"Couldn't install the update: {exc}",
                                              f"No se pudo instalar la actualización: {exc}")}
        if ok:
            threading.Timer(0.8, self.quit_app).start()
        return {"ok": ok, "message": msg}

    def run_update_checks(self) -> dict:
        """'Run checks again' in the update bar: same pass, without a notification."""
        try:
            report = self.run_post_update_checks(notify=False)
        except Exception as exc:
            return {"ok": False, "message": L(f"Couldn't run the checks: {exc}",
                                              f"No se pudieron hacer las comprobaciones: {exc}")}
        if report.get("ok"):
            return {"ok": True, "message": L(f"All {report['passed']} checks passed.",
                                             f"Las {report['passed']} comprobaciones han ido bien."),
                    "report": report}
        return {"ok": False,
                "message": L(f"{report['passed']} of {report['total']} checks passed. "
                             f"See what needs attention in the OpenCode status panel.",
                             f"{report['passed']} de {report['total']} comprobaciones bien. "
                             f"Mira qué falla en el panel de estado de OpenCode."),
                "report": report}

    def test_notification(self, kind: str = "info") -> dict:
        if not (self._tray and self._tray.alive):
            return {"ok": False, "message": L("The tray icon isn't running.", "El icono de la bandeja no está funcionando.")}
        samples = {
            "problem": (L("OpenCode can't connect right now", "OpenCode no puede conectarse ahora"),
                        L("The local proxy isn't running.", "El proxy local no está funcionando."), L("Fix", "Arreglar")),
            "hop": (L("Limit hit: switched to a new IP", "Límite alcanzado: IP nueva"),
                    L("OpenCode can keep going. openhop sent 'continue' for you.",
                      "OpenCode puede seguir. openhop ha enviado 'continue' por ti."), ""),
            "update": (L("A new openhop version is ready", "Hay una versión nueva de openhop lista"),
                       L("Press Update now to install it. It takes a few seconds.",
                         "Pulsa Actualizar para instalarla. Tarda unos segundos."), L("Update", "Actualizar")),
            "info": (L("This is a test notification", "Esto es una notificación de prueba"),
                     L("Notifications are working.", "Las notificaciones funcionan."), ""),
        }
        title, body, label = samples.get(kind, samples["info"])
        self._tray.notify(kind, title, body, action_label=label, action="open", force=True)
        return {"ok": True, "message": L("Notification sent.", "Notificación enviada.")}

    def win_begin(self, kind: str) -> dict:
        return self._frame.begin(str(kind))

    def win_step(self) -> None:
        self._frame.step()

    def win_end(self) -> dict:
        return self._frame.end()

    def _startup_sync(self) -> None:
        """Make sure the background workers run the code shipped with this build and are alive.

        The keeper (not this window) owns the :41987 proxy, so closing the window never breaks
        OpenCode. Workers running stale code (e.g. the old 'sys' proxy bug) are restarted once.
        """
        if self._migration.get("migrated"):
            # Coming from Opencode Unlimited: put the new scripts, shims, PATH and tasks in place.
            try:
                _deploy_scripts_and_shims()
                if self._migration.get("had_path"):
                    _update_user_path(True)
                if self._migration.get("had_tasks"):
                    self.toggle_autostart(True)
                self._startup_note = lambda: L("openhop moved your settings over from Opencode Unlimited.",
                                               "openhop ha traído tus ajustes de Opencode Unlimited.")
            except Exception as exc:
                append_log(DEST_DIR / "logs" / "migration.log", f"post-migration setup failed: {exc}")
        installed = (SCRIPTS_DIR / "warp_keeper.py").is_file()
        if installed:
            try:
                fp = _bundle_fingerprint()
                old = _STAMP_FILE.read_text(encoding="utf-8").strip() if _STAMP_FILE.is_file() else ""
                if fp != old:
                    _deploy_scripts_and_shims()
                    restarted = []
                    for name in _WORKERS:
                        if _worker_procs(name):
                            _stop_worker_processes(name)
                            ok, _ = _start_worker(name)
                            if ok:
                                restarted.append(name)
                    _STAMP_FILE.write_text(fp, encoding="utf-8")
                    if restarted:
                        names = ", ".join(restarted)
                        self._startup_note = lambda: L(
                            f"Updated background service ({names}) to this version.",
                            f"Servicio en segundo plano ({names}) actualizado a esta versión.")
            except Exception:
                pass
            for name in _WORKERS:
                if not _disabled_flag(name).is_file() and not _is_mutex_held(_WORKERS[name][2]):
                    try:
                        _start_worker(name)
                    except Exception:
                        pass
        self._ensure_proxy_active(wait=8.0)

    def _ensure_proxy_active(self, wait: float = 4.0) -> bool:
        """Last-resort fallback: host the :41987 proxy in this window only if no keeper serves it."""
        deadline = time.time() + wait
        while time.time() < deadline:
            if _port_open(SERVICE_PROXY_PORT):
                return True
            time.sleep(0.4)
        if _port_open(SERVICE_PROXY_PORT):
            return True
        try:
            from zen_proxy import start_proxy_server
            self._fallback_proxy = start_proxy_server(SERVICE_PROXY_PORT)[0]
            return True
        except Exception:
            return _port_open(SERVICE_PROXY_PORT)

    def _auto_heal_warp_startup(self) -> None:
        def _check_and_heal() -> None:
            time.sleep(3.0)
            wb = find_warp_cli()
            if not wb:
                return
            try:
                conn, mode, raw = warp_status(wb)
                raw_lower = raw.lower()
                # Only auto-heal if there is an explicit handshake MITM or cryptographic failure.
                # Never wipe registration on simple startup disconnected state!
                if ("mitm" in raw_lower) or ("cf_happy_eyeballs" in raw_lower):
                    self.repair_warp_connection()
            except Exception:
                pass

        threading.Thread(target=_check_and_heal, daemon=True).start()

    def _refresh_pool_async(self) -> None:
        def _run() -> None:
            pool = fetch_opencode_aaaa_pool()
            if pool:
                self._edge_pool = pool

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_egress_async(self, force: bool = False) -> None:
        with self._egress_lock:
            if self._egress_checking:
                return
            if not force and (time.time() - self._last_egress_ts) < 25.0:
                return
            self._egress_checking = True

        def _run() -> None:
            try:
                v6 = get_egress_v6(timeout=6.0)
                with self._egress_lock:
                    self._egress_v6 = v6
                    self._egress_failed = not v6
                    self._last_egress_ts = time.time()
            finally:
                with self._egress_lock:
                    self._egress_checking = False

        threading.Thread(target=_run, daemon=True).start()

    def _refresh_probe_async(self, force: bool = False) -> None:
        """Ask opencode.ai through the local proxy whether traffic really gets in and out."""
        with self._probe_lock:
            if self._probe_checking:
                return
            # After a failure retry sooner (30 s), but don't hammer while the tunnel is fine (120 s).
            limit = 30.0 if self._probe.get("ok") is False else PROBE_EVERY
            if not force and (time.time() - self._probe_ts) < limit:
                return
            self._probe_checking = True

        def _run() -> None:
            started = time.time()
            code = "unknown"
            try:
                # On startup the keeper may still be binding the port: give it a few seconds.
                for _ in range(24):
                    if _port_open(SERVICE_PROXY_PORT):
                        break
                    time.sleep(0.25)
                proxy = f"http://127.0.0.1:{SERVICE_PROXY_PORT}"
                if not _port_open(SERVICE_PROXY_PORT):
                    code = "proxy_down"
                else:
                    reached, stage = _probe_http(PROBE_URL, proxy)
                    if reached:
                        code = "ok"
                    else:
                        direct_ok, _ = _probe_http(PROBE_DIRECT_URL, None, timeout=5.0)
                        code = stage if direct_ok else "offline"
            except Exception:
                code = "upstream"
            finally:
                with self._probe_lock:
                    self._probe = {
                        "ok": code == "ok",
                        "code": code,
                        "ms": int((time.time() - started) * 1000),
                        "ts": int(time.time()),
                    }
                    self._probe_ts = time.time()
                    self._probe_checking = False

        threading.Thread(target=_run, daemon=True).start()

    def get_state(self, force_egress: bool = False) -> dict:
        state = self._compute_state(force_egress)
        self._state_cache = (time.time(), state)
        return state

    def get_state_cached(self, max_age: float = 10.0) -> dict:
        """Recent state without hammering warp-cli (used by the tray watcher while the UI also polls)."""
        cached = self._state_cache
        if cached and time.time() - cached[0] <= max_age:
            return cached[1]
        with self._state_lock:
            cached = self._state_cache
            if cached and time.time() - cached[0] <= max_age:
                return cached[1]
            return self.get_state()

    def get_token_stats(self) -> dict:
        """Token usage totals from OpenCode's database, cached for a minute.

        The UI asks for this on load and every ~45 s. It stays out of get_state() on purpose: the
        3-second status poll must never touch SQLite.
        """
        with self._token_lock:
            cached = self._token_cache
            if cached and time.time() - cached[0] < TOKEN_STATS_CACHE_SECONDS:
                return cached[1]
        stats = _read_token_stats()
        with self._token_lock:
            self._token_cache = (time.time(), stats)
        return stats

    def fix_everything(self) -> dict:
        """Run every one-click fix the health check suggests, most serious first; plain summary back."""
        order = ["install", "connect_opencode", "start_keeper", "restart_workers", "connect_warp", "start_tor", "start_guard", "fix_port"]
        done: list[str] = []
        failed: list[str] = []
        manual: list[str] = []
        tried: set[str] = set()
        for _round in range(2):
            health = self.get_state().get("health") or {}
            issues = sorted(health.get("issues", []), key=lambda i: 0 if i.get("level") == "bad" else 1)
            todo = [i for i in issues if i.get("fix") and i["fix"] not in tried]
            if not todo:
                break
            for issue in sorted(todo, key=lambda i: order.index(i["fix"]) if i["fix"] in order else 99):
                fix = issue["fix"]
                tried.add(fix)
                if fix == "restart_opencode":
                    continue  # it would cut off a chat that's answering: only from its own button
                if fix == "install_warp":
                    manual.append(L("Install Cloudflare WARP (free) from one.one.one.one, then press Fix everything again.",
                                    "Instala Cloudflare WARP (gratis) desde one.one.one.one y vuelve a pulsar Arreglarlo todo."))
                    continue
                try:
                    res = self.fix_issue(fix)
                except Exception as exc:
                    res = {"ok": False, "message": str(exc)}
                (done if res.get("ok") else failed).append(issue.get("title", fix))
        final = self.get_state().get("health") or {}
        left = [i.get("title", "") for i in final.get("issues", []) if i.get("level") == "bad"]
        if not left and not manual:
            msg = (L("Everything is working.", "Todo funciona.") if not done
                   else L(f"Fixed: {', '.join(done)}. Everything is working.",
                          f"Arreglado: {', '.join(done)}. Todo funciona."))
            return {"ok": True, "message": msg, "fixed": done, "remaining": []}
        parts = []
        if done:
            parts.append(L(f"Fixed: {', '.join(done)}.", f"Arreglado: {', '.join(done)}."))
        if left:
            parts.append(L(f"Still not working: {', '.join(left)}.", f"Sigue sin funcionar: {', '.join(left)}."))
        parts += manual
        return {"ok": False, "message": " ".join(parts), "fixed": done, "remaining": left + manual}

    def _compute_state(self, force_egress: bool = False) -> dict:
        warp_bin = find_warp_cli()
        connected, mode, status_text = warp_status(warp_bin) if warp_bin else (False, "", "Not Installed")
        local_v6 = get_warp_local_v6() or ""

        scope = current_warp_scope()

        if scope == "isolated":
            if connected and not self._egress_v6:
                # after a failed check, retry every ~25 s instead of on every poll (it looked stuck on "checking")
                self._refresh_egress_async(force=not self._egress_failed)
            elif not connected:
                with self._egress_lock:
                    self._egress_v6 = ""
            elif force_egress:
                self._refresh_egress_async(force=True)
        else:
            if local_v6 != self._last_local_v6:
                self._last_local_v6 = local_v6
                if local_v6:
                    self._refresh_egress_async(force=True)
                else:
                    with self._egress_lock:
                        self._egress_v6 = ""
            elif force_egress:
                self._refresh_egress_async(force=True)

        with self._egress_lock:
            egress_v6 = self._egress_v6
            checking_egress = self._egress_checking

        guard_state = {"blacklist": [], "hops": 0, "last_hop": 0, "current": ""}
        if GUARD_STATE_FILE.is_file():
            try:
                guard_state.update(json.loads(GUARD_STATE_FILE.read_text(encoding="utf-8")))
            except Exception:
                pass

        blacklist = [x for x in guard_state.get("blacklist", []) if x]
        pinned_edge = ""
        last_edge_file = LANE_DIR / "last_edge.txt"
        if last_edge_file.is_file():
            pinned_edge = last_edge_file.read_text(encoding="utf-8").strip()

        edge_override = "auto"
        override_file = LANE_DIR / "edge_override.txt"
        if override_file.is_file():
            val = override_file.read_text(encoding="utf-8").strip()
            if val:
                edge_override = val

        svc_cfg = Path.home() / ".config" / "opencode" / "service.json"
        svc_wired = False
        if svc_cfg.is_file():
            try:
                cfg = json.loads(svc_cfg.read_text(encoding="utf-8"))
                env_map = cfg.get("env", {}) if isinstance(cfg.get("env"), dict) else {}
                svc_wired = f"127.0.0.1:{SERVICE_PROXY_PORT}" in str(env_map.get("HTTPS_PROXY", ""))
            except Exception:
                pass

        cli_path = find_opencode_cli() or ""
        desktop_path = find_opencode_desktop() or ""
        log_size = OPENCODE_LOG_FILE.stat().st_size if OPENCODE_LOG_FILE.is_file() else 0

        egress_state = self._get_egress_state()
        keeper_running = _is_mutex_held(MUTEX_KEEPER)
        guard_running = _is_mutex_held(MUTEX_GUARD)
        proxy_listening = _port_open(SERVICE_PROXY_PORT)
        path_wired = _user_path_has_bin()
        recent_errors = _recent_opencode_errors()
        if egress_state.get("mode") == "vpn":
            for error in recent_errors:
                if error["code"] == "rate_limited":
                    error["detail"] = L("OpenCode reports that your free usage is used up. Wait for it to reset.",
                                        "OpenCode avisa de que has agotado el uso gratis. Espera a que se restablezca.")
                    error["fix"] = None
        rotating = is_keeper_paused()
        if not rotating:
            self._refresh_probe_async(force=force_egress)
        with self._probe_lock:
            probe = dict(self._probe)
            probe["checking"] = self._probe_checking
        health = self._build_health(
            warp_installed=bool(warp_bin),
            warp_connected=connected,
            rotating=rotating,
            egress=egress_state,
            keeper_running=keeper_running,
            guard_running=guard_running,
            proxy_listening=proxy_listening,
            svc_wired=svc_wired,
            installed=(SCRIPTS_DIR / "warp_keeper.py").is_file(),
            recent_errors=recent_errors,
            last_hop=int(guard_state.get("last_hop", 0) or 0),
            probe=probe,
            guard_backoff=int(guard_state.get("backoff_until", 0) or 0),
            guard_failed=int(guard_state.get("failed_hops", 0) or 0),
            guard_last_ok=float(guard_state.get("last_ok_hop", 0) or 0),
            guard_hops_max=int(guard_state.get("hops_max", 0) or 0),
            keeper_heartbeat=_heartbeat_age(KEEPER_LOG_FILE.parent / "heartbeat.txt"),
            guard_heartbeat=_heartbeat_age(GUARD_LOG_FILE.parent / "heartbeat.txt"),
            desktop_installed=bool(desktop_path),
            cli_installed=bool(cli_path),
            path_wired=path_wired,
            brand_pending=self._brand_pending_cached(),
        )

        return {
            "version": VERSION,
            "health": health,
            "recent_errors": recent_errors,
            "probe": probe,
            "warp": {
                "installed": bool(warp_bin),
                "bin": warp_bin or "",
                "connected": connected,
                "mode": mode or "unknown",
                "status_text": status_text or ("Connected" if connected else "Disconnected"),
                "adapter_v6": local_v6,
                "egress_v6": egress_v6,
                "egress_prefix": normalize_v6_64(egress_v6) if egress_v6 else "",
                "is_cf": is_cloudflare_v6(egress_v6) if egress_v6 else False,
                "checking_egress": checking_egress,
                "egress_failed": bool(self._egress_failed and not checking_egress),
                "rotating": rotating,
            },
            "services": {
                "keeper_running": keeper_running,
                "guard_running": guard_running,
                "keeper_task": _task_exists(TASK_KEEPER),
                "guard_task": _task_exists(TASK_GUARD),
                "keeper_heartbeat": _heartbeat_age(KEEPER_LOG_FILE.parent / "heartbeat.txt"),
                "guard_heartbeat": _heartbeat_age(GUARD_LOG_FILE.parent / "heartbeat.txt"),
                "proxy_listening": proxy_listening,
                "proxy_port": SERVICE_PROXY_PORT,
                "firewall_installed": is_killswitch_installed(),
                "is_admin": is_admin(),
                "auto_resume": self._is_auto_resume_active(),
                "turbo": self._turbo_state(),
                "chat_moves": self._chat_moves_state(),
            },
            "lane": {
                "pinned_edge": pinned_edge,
                "edge_override": edge_override,
                "edge_pool": self._edge_pool,
                "strict_mode": (LANE_DIR / "strict_proxy.flag").is_file(),
                "cli_path": cli_path,
                "desktop_path": desktop_path,
                "path_wired": path_wired,
                "desktop_service_wired": svc_wired,
                "scripts_installed": (SCRIPTS_DIR / "warp_keeper.py").is_file(),
            },
            "stats": {
                # addresses that really worked (older guards: every address tried)
                "hops": int(guard_state.get("good_hops", guard_state.get("hops", 0)) or 0),
                "blacklist_count": len(blacklist),
                "blacklist": list(reversed(blacklist)),
                "used_ips": _used_ips_summary(guard_state),
                "last_hop": int(guard_state.get("last_hop", 0) or 0),
                "db": _read_db_stats(),
                "log_size_bytes": log_size,
            },
            "logs": {
                "guard": _tail_lines(GUARD_LOG_FILE, 75),
                "keeper": _tail_lines(KEEPER_LOG_FILE, 75),
                "opencode": _tail_lines(OPENCODE_LOG_FILE, 55),
            },
            "egress": egress_state,
        }

    def _build_health(
        self,
        *,
        warp_installed: bool,
        warp_connected: bool,
        rotating: bool,
        egress: dict,
        keeper_running: bool,
        guard_running: bool,
        proxy_listening: bool,
        svc_wired: bool,
        installed: bool,
        recent_errors: list[dict],
        last_hop: int = 0,
        probe: dict | None = None,
        guard_backoff: int = 0,
        guard_failed: int = 0,
        guard_last_ok: float = 0.0,
        guard_hops_max: int = 0,
        keeper_heartbeat: float | None = None,
        guard_heartbeat: float | None = None,
        desktop_installed: bool = False,
        cli_installed: bool = False,
        path_wired: bool = False,
        brand_pending: bool = False,
    ) -> dict:
        """Summarise the live state as a short list of plain-language problems with one-click fixes."""
        issues: list[dict] = []

        def add(level: str, title: str, detail: str, fix: str | None = None, fix_label: str = "") -> None:
            issues.append({"level": level, "title": title, "detail": detail, "fix": fix,
                           "fix_label": fix_label or L("Fix it", "Arreglar")})

        mode = egress.get("mode", "warp")
        # While the free limit is being fixed, Zen Guard disconnects WARP on purpose for every new IP, and
        # a repair can take a minute or two. Showing "WARP is disconnected" with a Connect button then is
        # wrong (and pressing it fights the guard), so only report it if it lasts longer than that.
        now0 = time.time()
        if warp_connected or rotating:
            self._warp_down_since = 0.0
        elif not getattr(self, "_warp_down_since", 0.0):
            self._warp_down_since = now0
        warp_down_for = now0 - self._warp_down_since if getattr(self, "_warp_down_since", 0.0) else 0.0
        hunting_now = str((getattr(self, "_incident", None) or {}).get("phase", "none")) in ("fixing", "checking")
        warp_blip = hunting_now and warp_down_for < 150
        if not installed:
            add("warn", L("Setup isn't finished", "La instalación no está terminada"),
                L("The background service isn't installed yet, so OpenCode isn't protected or auto-rotated.",
                  "El servicio en segundo plano aún no está instalado, así que OpenCode no está protegido ni cambia "
                  "de IP solo."),
                "install", L("Install", "Instalar"))
        if installed and (desktop_installed or cli_installed) and not svc_wired and not path_wired:
            add("warn", L("OpenCode isn't using openhop", "OpenCode no está usando openhop"),
                L("OpenCode is installed but it's connecting directly, so it will hit the free limit and nothing "
                  "will rotate. Press Connect, then reopen OpenCode.",
                  "OpenCode está instalado pero se conecta directamente, así que llegará al límite gratis y no "
                  "cambiará de IP. Pulsa Conectar y vuelve a abrir OpenCode."),
                "connect_opencode", L("Connect", "Conectar"))
        if brand_pending:
            # openhop can only change OpenCode Desktop's files while it's closed. Someone who never closes it
            # never gets the openhop.cfd name, the hidden limit card or the extras, and nothing said why.
            add("warn", L("Close OpenCode once to finish", "Cierra OpenCode una vez para terminar"),
                L("openhop can only add its changes to OpenCode (the extras, hiding the limit card) while OpenCode "
                  "is closed. Press the button: it closes OpenCode, finishes and opens it again. Your chats are "
                  "kept; if one is answering right now, wait until it's done.",
                  "openhop solo puede poner sus cambios en OpenCode (los extras, quitar el aviso del límite) con "
                  "OpenCode cerrado. Pulsa el botón: lo cierra, termina y lo vuelve a abrir. Tus chats se "
                  "conservan; si uno está respondiendo ahora, espera a que acabe."),
                "restart_opencode", L("Close and reopen", "Cerrar y volver a abrir"))
        if (svc_wired or path_wired) and not proxy_listening:
            add("bad", L("OpenCode can't connect right now", "OpenCode no puede conectarse ahora"),
                L("OpenCode is set to go through the local proxy (port 41987), but it isn't running. "
                  "Messages fail with ECONNREFUSED until it's back.",
                  "OpenCode está configurado para pasar por el proxy local (puerto 41987), pero no está funcionando. "
                  "Los mensajes fallan con ECONNREFUSED hasta que vuelva."),
                "start_keeper", L("Start it", "Iniciarlo"))
        elif installed and not keeper_running:
            add("warn", L("Background protection is off", "La protección en segundo plano está desactivada"),
                L("WARP Keeper isn't running. This window is covering for it, but OpenCode will lose "
                  "its connection when you close the app.",
                  "WARP Keeper no está funcionando. Esta ventana lo está supliendo, pero OpenCode perderá "
                  "la conexión cuando cierres la app."),
                "start_keeper", L("Turn on", "Activar"))
        if keeper_running and keeper_heartbeat is not None and keeper_heartbeat > HEARTBEAT_STALE:
            add("bad", L("Background protection stopped responding", "La protección en segundo plano no responde"),
                L(f"WARP Keeper is still running but hasn't checked in for {int(keeper_heartbeat // 60)} minutes, "
                  "so OpenCode may lose its connection. Press Restart.",
                  f"WARP Keeper sigue en marcha pero lleva {int(keeper_heartbeat // 60)} minutos sin dar señales, "
                  "así que OpenCode puede perder la conexión. Pulsa Reiniciar."),
                "restart_workers", L("Restart", "Reiniciar"))
        if guard_running and guard_heartbeat is not None and guard_heartbeat > HEARTBEAT_STALE:
            add("warn", L("Auto-rotation stopped responding", "El cambio automático de IP no responde"),
                L(f"Zen Guard is still running but hasn't checked in for {int(guard_heartbeat // 60)} minutes, "
                  "so the free limit won't rotate. Press Restart.",
                  f"Zen Guard sigue en marcha pero lleva {int(guard_heartbeat // 60)} minutos sin dar señales, "
                  "así que no cambiará de IP al llegar al límite gratis. Pulsa Reiniciar."),
                "restart_workers", L("Restart", "Reiniciar"))
        if mode in ("warp", "clean"):
            if not warp_installed:
                add("bad", L("Cloudflare WARP isn't installed", "Cloudflare WARP no está instalado"),
                    L("WARP mode needs the free Cloudflare WARP app (1.1.1.1).",
                      "El modo WARP necesita la app gratuita Cloudflare WARP (1.1.1.1)."),
                    "install_warp", L("Download WARP", "Descargar WARP"))
            elif not warp_connected and not rotating and not warp_blip:
                add("bad", L("WARP is disconnected", "WARP está desconectado"),
                    L("Traffic can't go through Cloudflare right now.",
                      "Ahora mismo el tráfico no puede pasar por Cloudflare."),
                    "connect_warp", L("Connect", "Conectar"))
            elif not rotating and warp_connected:
                try:
                    from ouc_common import warp_path_leaking
                    leaking = warp_path_leaking()
                except Exception:
                    leaking = False
                if leaking:
                    # WARP's own status says Connected, but a real request went out with the user's IP.
                    add("bad", L("WARP is connected but isn't protecting you", "WARP está conectado pero no te protege"),
                        L("Cloudflare WARP says it's connected, but traffic is leaving with your own IP. openhop "
                          "is blocking OpenCode so your IP isn't used, and is reconnecting WARP. "
                          "Press Repair to do it now.",
                          "Cloudflare WARP dice que está conectado, pero el tráfico sale con tu propia IP. openhop "
                          "está bloqueando OpenCode para que no se use tu IP y está reconectando WARP. "
                          "Pulsa Reparar para hacerlo ya."),
                        "repair_warp", L("Repair", "Reparar"))
        elif mode == "vpn":
            vname = egress.get("vpn_name") or "VPN"
            is_wg = egress.get("vpn_id") == "wireguard"
            if not egress.get("vpn_connected") and is_wg:
                add("bad", L("The WireGuard tunnel isn't running", "El túnel WireGuard no está en marcha"),
                    L("OpenCode is set to go through your WireGuard files, but the tunnel isn't up. openhop holds "
                      "OpenCode back so your own IP isn't used. Press Start, or add a WireGuard file.",
                      "OpenCode está configurado para salir por tus archivos WireGuard, pero el túnel no está en "
                      "marcha. openhop frena OpenCode para que no se use tu propia IP. Pulsa Iniciar o añade un "
                      "archivo WireGuard."),
                    "wg_start", L("Start", "Iniciar"))
            elif not egress.get("vpn_connected"):
                add("bad", L(f"{vname} isn't connected", f"{vname} no está conectada"),
                    L(f"OpenCode is set to go through {vname}, but it's off. openhop holds OpenCode back so your "
                      f"own IP isn't used. Connect it in its app.",
                      f"OpenCode está configurado para salir por {vname}, pero está apagada. openhop frena "
                      f"OpenCode para que no se use tu propia IP. Conéctala en su app."),
                    "open_vpn", L(f"Open {vname}", f"Abrir {vname}"))
        elif mode == "tor" and not egress.get("tor_active"):
            add("bad", L("Tor isn't running", "Tor no está funcionando"),
                L("Tor mode is selected but no Tor proxy is reachable.",
                  "Has elegido el modo Tor, pero no se encuentra ningún proxy de Tor."),
                "start_tor", L("Start Tor", "Iniciar Tor"))
        if mode != "vpn" and installed and not guard_running:
            add("warn", L("Auto-rotation is off", "El cambio automático de IP está desactivado"),
                L("Zen Guard isn't running, so when you hit the free limit you'll have to rotate manually.",
                  "Zen Guard no está funcionando, así que cuando llegues al límite gratis tendrás que cambiar "
                  "de IP a mano."),
                "start_guard", L("Turn on", "Activar"))

        # A real request through the proxy: the only check that proves traffic gets in and out.
        code = (probe or {}).get("code", "")
        if proxy_listening and code in ("upstream", "timeout", "offline"):
            if code == "offline":
                add("bad", L("This PC has no internet", "Este PC no tiene internet"),
                    L("Neither the direct connection nor openhop could reach the internet. Check Wi-Fi or the "
                      "network cable, then press Refresh.",
                      "Ni la conexión directa ni openhop han podido llegar a internet. Revisa el wifi o el "
                      "cable de red y pulsa Recargar."), None)
            elif (
                (mode in ("warp", "clean") and warp_connected)
                or (mode == "tor" and egress.get("tor_active"))
                or mode == "custom"
                or (mode == "vpn" and egress.get("vpn_connected") and not egress.get("vpn_wait"))
            ):
                # Don't repeat what the WARP/Tor rows already say when that connection is down.
                hint = {
                    "vpn": L(f"Pick another server in {egress.get('vpn_name') or 'the VPN'}, or another connection "
                             f"type in Advanced.",
                             f"Elige otro servidor en {egress.get('vpn_name') or 'la VPN'} u otro tipo de conexión "
                             f"en Avanzado."),
                    "tor": L("Press Start Tor, or pick another connection type in Advanced.",
                             "Pulsa Iniciar Tor o elige otro tipo de conexión en Avanzado."),
                    "custom": L("Press New IP to try another proxy node, or pick another connection type in Advanced.",
                                "Pulsa IP nueva para probar otro proxy de tu lista o elige otro tipo de conexión en Avanzado."),
                }.get(mode, L("Press Repair to reconnect WARP, or pick another connection type in Advanced.",
                              "Pulsa Reparar para reconectar WARP o elige otro tipo de conexión en Avanzado."))
                add("bad", L("OpenCode can't get through openhop", "OpenCode no consigue salir a través de openhop"),
                    L("The local proxy is running, but nothing comes back from opencode.ai. ",
                      "El proxy local está funcionando, pero opencode.ai no devuelve nada. ") + hint,
                    ("start_tor" if mode == "tor" else "hop" if mode == "custom"
                     else "open_vpn" if mode == "vpn" else "repair_warp"),
                    (L("Open the VPN", "Abrir la VPN") if mode == "vpn"
                     else L("Repair", "Reparar") if mode not in ("tor", "custom") else L("Try", "Probar")))

        if mode != "vpn" and guard_backoff and guard_backoff > time.time():
            at = time.strftime("%H:%M", time.localtime(guard_backoff))
            if mode in ("warp", "clean"):
                add("warn", L("Every address tried was already used up",
                              "Todas las direcciones probadas ya estaban gastadas"),
                    L(f"Other people already used up the free messages on WARP's shared addresses today. openhop "
                      f"tries again by itself at {at}. Or switch to Tor now: different addresses, a bit slower.",
                      f"Otras personas ya han gastado hoy los mensajes gratis de las direcciones compartidas de WARP. "
                      f"openhop lo vuelve a intentar solo a las {at}. O cambia a Tor ahora: otras direcciones, "
                      f"un poco más lento."),
                    "use_tor", L("Try Tor", "Probar Tor"))
            else:
                add("warn", L("Every address tried was already used up",
                              "Todas las direcciones probadas ya estaban gastadas"),
                    L(f"openhop tries again by itself at {at}, or press \"Try now\".",
                      f"openhop lo vuelve a intentar solo a las {at}, o pulsa \"Probar ya\"."),
                    "kick_guard", L("Try now", "Probar ya"))

        # The limit keeps coming back even after new IPs: don't claim "everything is working".
        limit_back_title = L("The free limit keeps coming back", "El límite gratis no deja de volver")
        if mode in ("warp", "clean", "tor", "custom") and not rotating:
            walls = _recent_wall_times(900.0)
            after_hop = [t for t in walls if not last_hop or t - last_hop > 90]
            if len(after_hop) >= 3 and time.time() - max(after_hop) < 300:
                hint = (L("WARP's shared IPs in your area may be used up right now. Try again, "
                          "or switch to Tor in Advanced > Connection type.",
                          "Puede que las IPs compartidas de WARP en tu zona estén gastadas ahora mismo. Vuelve a "
                          "intentarlo o cambia a Tor en Avanzado > Tipo de conexión.") if mode in ("warp", "clean")
                        else L("This connection's IPs may be used up. Try again, or pick another connection type in Advanced.",
                               "Puede que las IPs de esta conexión estén gastadas. Vuelve a intentarlo o elige otro "
                               "tipo de conexión en Avanzado."))
                add("warn", limit_back_title,
                    L(f"OpenCode hit the limit {len(walls)} times in the last 15 minutes, even after a new IP. {hint}",
                      f"OpenCode ha llegado al límite {len(walls)} veces en los últimos 15 minutos, incluso con una "
                      f"IP nueva. {hint}"),
                    "hop", L("New IP", "IP nueva"))

        last = recent_errors[0] if recent_errors else None
        last_recent = bool(last and last.get("ts") and time.time() - last["ts"] < 15 * 60)

        # The free-limit incident: from the limit error until a new IP really holds. Right after a new IP
        # everything looks green and 15 s later the limit can be back, so only call it fixed once a new
        # IP has gone a full minute without the limit.
        now = time.time()
        try:
            from zen_guard import FAILED_HOPS_MAX as max_tries
        except Exception:
            max_tries = 15
        if guard_hops_max > 0:   # the guard switches to Tor sooner when it can (zen_guard._hops_max)
            max_tries = guard_hops_max
        walls_recent = _recent_wall_times(300.0)
        last_wall = max(walls_recent) if walls_recent else 0
        try:
            import notices as _notices
            maint_block = _notices.blocked()
        except Exception:
            maint_block = False
        with self._incident_lock:
            self._incident = step_incident(
                self._incident, now,
                active=guard_running and not maint_block and mode in ("warp", "clean", "tor", "custom"),
                rotating=rotating, last_wall=last_wall, last_hop=last_hop,
                guard_failed=guard_failed, backoff_until=guard_backoff)
            inc = dict(self._incident)
        phase = inc.get("phase", "none")
        tried = int(inc.get("tried", 0) or 0)
        inc_view = {
            "phase": phase, "id": int(inc.get("id", 0) or 0), "since": inc.get("since", 0),
            "tried": tried, "attempt": min(tried + 1, max_tries), "max": max_tries,
            "retry_at": inc.get("retry_at", 0) if phase == "paused" else 0,
            "took": inc.get("took", 0), "ended": inc.get("ended", 0),
        }
        # Paused = every address tried is used up: say when OpenCode refills them (from its retryAfterMs).
        back_en = back_es = ""
        if phase == "paused":
            try:
                from zen_guard import free_limit_back
                back = free_limit_back(now)
                inc_view.update(back_at=back["at"], back_to=back["to"])
                a = time.strftime("%H:%M", time.localtime(back["at"]))
                b = time.strftime("%H:%M", time.localtime(back["to"])) if back["to"] else ""
                back_en = f"between {a} and {b}" if b else f"around {a}"
                back_es = f"entre las {a} y las {b}" if b else f"sobre las {a}"
            except Exception:
                pass
        if phase != "none":  # the incident lines below already explain it, with the right advice
            issues[:] = [i for i in issues if i["title"] != limit_back_title]
        used_up = L(f" {tried} address{'es were' if tried != 1 else ' was'} already used up by other people, "
                    f"so openhop keeps trying (up to {max_tries}).",
                    f" {tried} direcci{'ones ya estaban gastadas' if tried != 1 else 'ón ya estaba gastada'} por "
                    f"otras personas, así que openhop sigue probando (hasta {max_tries}).") if tried else ""

        # Chats that ended on the free limit with nothing fixing them (e.g. openhop started after the limit
        # hit): don't say "Everything is working" then. Only when no incident explains it already.
        failed_cont = self._failed_continues() if mode != "vpn" and not rotating else {}
        continue_issue = None
        if failed_cont:
            n_fc = len(failed_cont)
            continue_issue = {
                "code": "continue_failed", "level": "warn",
                "title": L(f"The 'continue' didn't reach {'your chat' if n_fc == 1 else f'{n_fc} chats'}",
                           f"El 'continue' no llegó a {'tu chat' if n_fc == 1 else f'{n_fc} chats'}"),
                "detail": L("Send 'continue' in OpenCode when the chat is available again.",
                            "Envía 'continue' en OpenCode cuando el chat vuelva a estar disponible."),
                "fix": None,
            }
            issues.append(continue_issue)
        stuck = 0
        if mode != "vpn" and phase == "none" and not rotating and not maint_block:
            stuck = len([s for s in self._stuck_chats() if s not in failed_cont])
        stuck_title = (L(f"{stuck} chat{'s are' if stuck != 1 else ' is'} stopped at the free limit",
                         f"{stuck} chat{'s están parados' if stuck != 1 else ' está parado'} en el límite gratis")
                       if stuck else "")
        if stuck:
            add("warn", L("Stopped chats need a new IP", "Los chats parados necesitan una IP nueva"),
                L(f"openhop gets a new IP and sends 'continue' to {'them' if stuck != 1 else 'it'}.",
                  f"openhop consigue una IP nueva y {'les' if stuck != 1 else 'le'} envía 'continue'."),
                "rescue_stuck", L("New IP", "IP nueva"))

        # Zen Guard switched to Tor by itself because WARP's addresses were used up: say so while it lasts.
        auto_tor_until = float(egress.get("auto_tor_until", 0) or 0) if mode == "tor" else 0.0
        auto_tor_line = ""
        if auto_tor_until > now:
            back = time.strftime("%H:%M", time.localtime(auto_tor_until))
            auto_tor_line = L(f"WARP's addresses were used up, so openhop switched to Tor by itself. "
                              f"It goes back to WARP after {back}.",
                              f"Las direcciones de WARP estaban gastadas, así que openhop ha cambiado a Tor solo. "
                              f"Vuelve a WARP después de las {back}.")

        bad = any(i["level"] == "bad" for i in issues)
        wired = svc_wired or path_wired
        needs_attention = (L("OpenCode needs attention", "OpenCode necesita atención"),
                           L("Fix the problem below and OpenCode will work again.",
                             "Arregla el problema de abajo y OpenCode volverá a funcionar."))
        if maint_block and not bad:
            status, title = "warn", L("openhop is under maintenance", "openhop está en mantenimiento")
            sub = L("Getting new IPs is paused until the maintenance is over. OpenCode still connects; "
                    "if it hits the free limit, send 'continue' once openhop is back.",
                    "Conseguir IPs nuevas está en pausa hasta que acabe el mantenimiento. OpenCode sigue "
                    "conectando; si llega al límite gratis, envía 'continue' cuando openhop vuelva.")
        elif phase == "paused":
            # The issue row below says when it retries and offers Tor; keep the headline honest.
            if back_en:
                status, title = "warn", L(f"Free messages come back {back_en}",
                                          f"Los mensajes gratis vuelven {back_es}")
                sub = L(f"OpenCode expects free messages to be available {back_en} (your time).",
                        f"OpenCode prevé que los mensajes gratis vuelvan {back_es} (tu hora).")
            else:
                status, title = "warn", L("Waiting for a free address", "Esperando una dirección libre")
                sub = L("OpenCode ran out of free messages. Check your chat when the quota resets.",
                        "OpenCode ha agotado los mensajes gratis. Comprueba tu chat cuando se restablezca la cuota.")
        elif bad and not rotating:
            status, (title, sub) = "bad", needs_attention
        elif phase == "fixing" or (rotating and phase != "checking"):
            status, title = "busy", L("Changing the connection…", "Cambiando la conexión…")
            sub = L("Check your chat in OpenCode once the connection is back.",
                    "Comprueba tu chat en OpenCode cuando vuelva la conexión.") + used_up
        elif phase == "checking":
            status, title = "busy", L("Checking the new IP…", "Comprobando la IP nueva…")
            sub = L("openhop is checking the connection. Check your chat for a reply.",
                    "openhop está comprobando la conexión. Mira si tu chat ha recibido una respuesta.") + used_up
        elif bad:
            status, (title, sub) = "bad", needs_attention
        elif continue_issue:
            status, title, sub = "warn", continue_issue["title"], continue_issue["detail"]
        elif stuck:
            status, title = "warn", stuck_title
            sub = L("They hit OpenCode's free limit and nothing has fixed it yet (for example, openhop wasn't "
                    "running then). Press New IP below: openhop gets a fresh address and continues them for you.",
                    "Llegaron al límite gratis de OpenCode y nada lo ha arreglado todavía (por ejemplo, openhop no "
                    "estaba en marcha). Pulsa IP nueva abajo: openhop consigue una dirección nueva y los sigue por ti.")
        elif issues:
            status, title, sub = "warn", L("Working, with warnings", "Funciona, con avisos"), L(
                "OpenCode works, but something below should be turned on.",
                "OpenCode funciona, pero deberías activar algo de lo que hay abajo.")
        elif wired and probe is not None and probe.get("ok") is not True:
            # Never claim "everything is working" before a real request has gone through the proxy.
            if probe.get("ok") is None:
                status, title, sub = "warn", L("Checking the connection…", "Comprobando la conexión…"), L(
                    "Making sure traffic really gets through openhop. This takes a few seconds.",
                    "Comprobando que el tráfico pasa de verdad por openhop. Tarda unos segundos.")
            else:
                status, title, sub = "warn", L("Couldn't confirm the connection", "No se pudo confirmar la conexión"), L(
                    "The last real test didn't finish. Press Refresh to try again.",
                    "La última prueba real no terminó. Pulsa Recargar para volver a probar.")
        else:
            status, title = "ok", L("Everything is working", "Todo funciona")
            sub = {
                "tor": L("OpenCode is connected through the Tor network.", "OpenCode está conectado a través de la red Tor."),
                "custom": L("OpenCode is connected through your custom proxy.", "OpenCode está conectado a través de tu proxy."),
                "vpn": L(f"OpenCode is connected through {egress.get('vpn_name') or 'your VPN'}.",
                         f"OpenCode está conectado a través de {egress.get('vpn_name') or 'tu VPN'}."),
            }.get(mode, L("OpenCode is connected through Cloudflare WARP and auto-rotates when you hit the limit.",
                          "OpenCode está conectado a través de Cloudflare WARP y cambia de IP solo cuando llegas al límite."))
            if auto_tor_line:
                sub = f"{auto_tor_line} {sub}"
            if phase == "done":
                title = L("Check your chat", "Comprueba tu chat")
                sub = L("The connection changed. Check your chat in OpenCode for a reply.",
                        "La conexión ha cambiado. Mira en OpenCode si tu chat ha recibido una respuesta.")
            elif last_recent and (not last_wall or now - last_wall > 180):
                sub += L(f" The last OpenCode error ({last['title'].lower()}) looks resolved now.",
                         f" El último error de OpenCode ({last['title'].lower()}) parece resuelto.")
        if auto_tor_line and status == "warn" and auto_tor_line not in sub:
            sub = f"{sub} {auto_tor_line}"
        if self._startup_note and status in ("ok", "warn"):
            sub = f"{self._startup_note()} {sub}"
        return {"status": status, "title": title, "sub": sub, "issues": issues, "incident": inc_view,
                "hunting": phase in ("fixing", "checking") and status == "busy"}

    STUCK_CHECK_EVERY = 30.0

    def _stuck_chats(self) -> list[str]:
        """Chats whose last run ended on the free limit (zen_guard._stuck_sessions), read at most every 30 s.

        get_state() is polled every few seconds; the SQLite query is cheap (~0.03 s) but needn't run that often.
        """
        cached = getattr(self, "_stuck_cache", None)
        if cached and time.time() - cached[0] < self.STUCK_CHECK_EVERY:
            return cached[1]
        try:
            from auto_resume import is_auto_resume_enabled
            from zen_guard import _stuck_sessions
            # Auto-continue off: the user continues by hand, and the fix couldn't continue them anyway.
            # min_age: a limit hit seconds ago is the guard's to fix (it checks every 10 s); don't flash a warning.
            stuck = list(_stuck_sessions(min_age=90)) if is_auto_resume_enabled() else []
        except Exception:
            stuck = []
        self._stuck_cache = (time.time(), stuck)
        return stuck

    def _failed_continues(self) -> dict:
        """Chats where openhop's 'continue' didn't arrive (auto_resume.failed_resumes), read at most every 10 s."""
        cached = getattr(self, "_failed_cont_cache", None)
        if cached and time.time() - cached[0] < 10:
            return cached[1]
        try:
            from auto_resume import failed_resumes
            failed = failed_resumes()
        except Exception:
            failed = {}
        self._failed_cont_cache = (time.time(), failed)
        return failed

    @staticmethod
    def _kick_guard() -> None:
        """Ask Zen Guard to end its rotation pause now (it checks every 10 s)."""
        try:
            (GUARD_LOG_FILE.parent / "kick").write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass

    def fix_issue(self, action: str) -> dict:
        """One-click fixes for the problems listed in the health panel."""
        if action in ("kick_guard", "resend_continue", "rescue_stuck"):
            from proxy_chain import get_egress_config
            if get_egress_config().get("mode") == "vpn":
                return {"ok": False, "message": L("This action is unavailable with Your VPN.",
                                                  "Esta acción no está disponible con Tu VPN.")}
        if action == "start_keeper":
            res = self.toggle_worker("keeper", True)
            if self._ensure_proxy_active(wait=6.0):
                return {"ok": True, "message": L("Background service is running. OpenCode can connect again — send 'continue'.",
                                                 "El servicio en segundo plano está en marcha. OpenCode ya puede conectarse: envía 'continue'.")}
            return res
        if action == "start_guard":
            return self.toggle_worker("guard", True)
        if action == "restart_opencode":
            return self.restart_opencode_desktop()
        if action == "restart_workers":
            for name in _WORKERS:
                _stop_worker_processes(name)
                _disabled_flag(name).unlink(missing_ok=True)
                _start_worker(name)
            self._ensure_proxy_active(wait=6.0)
            return {"ok": True, "message": L("Background service restarted with the latest code.",
                                             "Servicio en segundo plano reiniciado con el código más reciente.")}
        if action == "open_vpn":
            return self.open_vpn_app()
        if action == "wg_import":
            return self.wg_import()
        if action == "wg_start":
            return self.wg_start()
        if action == "connect_warp":
            return self.warp_connect()
        if action == "repair_warp":
            return self.repair_warp_connection()
        if action == "hop":
            return self.hop_now()
        if action == "kick_guard":
            self._kick_guard()
            return {"ok": True, "message": L("openhop is trying a new address now. The chat continues by itself if it works.",
                                             "openhop está probando una dirección nueva. Si funciona, el chat sigue solo.")}
        if action == "resend_continue":
            self._failed_cont_cache = None
            try:
                from auto_resume import resend_failed
                sent, note = resend_failed()
            except Exception as exc:
                sent, note = 0, str(exc)
            if sent:
                return {"ok": True, "message": L("Request started. Check your chat in OpenCode.",
                                                 "Solicitud iniciada. Comprueba tu chat en OpenCode.")}
            return {"ok": False, "message": L("Couldn't send it. Type 'continue' in the chat yourself.",
                                              "No se pudo enviar. Escribe 'continue' en el chat tú mismo.")}
        if action == "rescue_stuck":
            # Chats stopped at the free limit with nothing fixing them: Zen Guard gets a new IP (in any
            # connection type) and sends them 'continue'. A guard that wasn't running does this when it starts.
            self._stuck_cache = None
            if not _is_mutex_held(MUTEX_GUARD):
                res = self.toggle_worker("guard", True)
                if not res.get("ok"):
                    return res
            else:
                try:
                    (GUARD_LOG_FILE.parent / "rescue").write_text(str(time.time()), encoding="utf-8")
                except Exception as exc:
                    return {"ok": False, "message": L(f"Couldn't ask auto-rotation to help: {exc}",
                                                      f"No se pudo pedir ayuda al cambio automático de IP: {exc}")}
            return {"ok": True, "message": L("openhop is getting a new IP and will send 'continue' to the stopped "
                                             "chats. This takes about 15 seconds.",
                                             "openhop está consiguiendo una IP nueva y enviará 'continue' a los chats "
                                             "parados. Tarda unos 15 segundos.")}
        if action == "use_tor":
            try:
                from proxy_chain import switch_to_tor
                switch_to_tor(wait=False)   # same path Zen Guard uses for its automatic switch
            except Exception as exc:
                return {"ok": False, "message": L(f"Couldn't switch to Tor: {exc}", f"No se pudo cambiar a Tor: {exc}")}
            self._kick_guard()
            return {"ok": True, "message": L("Switched to Tor. openhop is getting a Tor address and will continue "
                                             "the chat by itself. To go back later: Advanced > Connection type > WARP.",
                                             "Cambiado a Tor. openhop está consiguiendo una dirección de Tor y seguirá "
                                             "el chat solo. Para volver más tarde: Avanzado > Tipo de conexión > WARP.")}
        if action == "fix_port":
            return self.fix_opencode_service()
        if action == "start_tor":
            return self.toggle_tor_service(True)
        if action == "install":
            return self.install_or_repair()
        if action == "connect_opencode":
            res = self.toggle_desktop_service_proxy(True)
            try:
                _deploy_scripts_and_shims()
                _update_user_path(True)
            except Exception:
                pass
            if res.get("ok"):
                res["message"] = L(
                    "OpenCode is connected to openhop. Close and reopen OpenCode so it picks up the change.",
                    "OpenCode está conectado a openhop. Cierra y vuelve a abrir OpenCode para que coja el cambio.",
                )
            return res
        if action == "install_warp":
            webbrowser.open("https://one.one.one.one/")
            return {"ok": True, "message": L("Opened the Cloudflare WARP download page. Install it, then press Refresh.",
                                             "Se ha abierto la página de descarga de Cloudflare WARP. Instálalo y pulsa Recargar.")}
        return {"ok": False, "message": L("Unknown fix.", "Arreglo desconocido.")}

    def _get_egress_state(self) -> dict:
        try:
            from proxy_chain import get_egress_config
            from tor_manager import get_active_tor_target, is_portable_tor_running
            from clean_endpoint import KNOWN_CLEAN_ENDPOINTS
            cfg = get_egress_config()
            target = get_active_tor_target()
            from proxy_chain import get_current_custom_proxy
            cur_h, cur_p, cur_i, cur_total = get_current_custom_proxy()
            # Zen Guard switched to Tor by itself (WARP's addresses were used up): when it goes back.
            auto_until = float(cfg.get("auto_tor_until", 0) or 0) if cfg.get("mode") == "tor" else 0.0
            vpn_info = self._vpn_state(cfg)
            return {
                **vpn_info,
                "mode": cfg.get("mode", "warp"),
                "auto_tor_until": int(auto_until),
                "auto_tor_since": int(float(cfg.get("auto_tor_since", 0) or 0)) if auto_until else 0,
                "warp_scope": current_warp_scope(),
                "custom_proxy": cfg.get("custom_proxy", "127.0.0.1:10808"),
                "custom_type": cfg.get("custom_type", "socks5"),
                "custom_pool_count": cur_total,
                "custom_current_node": f"{cur_h}:{cur_p} (#{cur_i + 1}/{cur_total})",
                "tor_active": bool(target),
                "tor_target": f"{target[0]}:{target[1]}" if target else L("Not running", "Apagado"),
                "tor_running": is_portable_tor_running(),
                "clean_endpoints": list(KNOWN_CLEAN_ENDPOINTS),
            }
        except Exception as exc:
            return {"mode": "warp", "warp_scope": "isolated", "error": str(exc), "clean_endpoints": []}

    # --- VPN apps the user already has (Norton, Proton VPN, ...) -------------------------------------------------

    def _vpn_state(self, cfg: dict) -> dict:
        """Report the chosen VPN and whether its adapter or tunnel is up."""
        vpn_id = str(cfg.get("vpn_id") or "")
        if not vpn_id:
            return {"vpn_id": "", "vpn_name": "", "vpn_connected": False, "vpn_ip": ""}
        try:
            from vpn_detect import get_vpn, vpn_bind_address
            vpn = get_vpn(vpn_id) or {}
            bind = vpn_bind_address(vpn_id)
        except Exception:
            vpn, bind = {}, None
        ip = bind[0] if bind else ""
        if vpn_id == "wireguard" and bind:
            try:
                import wg_manager
                cur = wg_manager.current_conf()
                ip = wg_manager._label(cur) if cur else ""
            except OSError:
                ip = ""
        return {"vpn_id": vpn_id, "vpn_name": vpn.get("name") or vpn_id, "vpn_connected": bool(bind),
                "vpn_ip": ip}

    def detect_vpns(self) -> dict:
        """Look for VPN apps on this PC (the "Detect" button). Real icons come from each app's own .exe."""
        try:
            from vpn_detect import catalog_names, detect, tunnel_adapters
            found = detect(with_icons=True)
            adapters = [{"name": a["name"], "desc": a["desc"], "connected": bool(a["up"] and (a["v4"] or a["v6"]))}
                        for a in tunnel_adapters()]
            self._vpn_cache = found
            n = len(found)
            msg = (L("No VPN app found on this PC.", "No se ha encontrado ninguna app de VPN en este PC.") if not n
                   else L(f"Found {n} VPN app{'s' if n != 1 else ''}.",
                          f"Encontrada{'s' if n != 1 else ''} {n} app{'s' if n != 1 else ''} de VPN."))
            return {"ok": True, "vpns": found, "known": catalog_names(), "adapters": adapters, "message": msg}
        except Exception as exc:
            return {"ok": False, "vpns": [], "known": [], "adapters": [],
                    "message": L(f"Couldn't look for VPN apps: {exc}", f"No se pudieron buscar apps de VPN: {exc}")}

    def set_vpn(self, vpn_id: str) -> dict:
        """Send OpenCode through this VPN app (connection type "vpn")."""
        from ouc_common import bump_egress_epoch
        from proxy_chain import get_egress_config, set_egress_config
        from vpn_detect import get_vpn, vpn_bind_address
        vpn = get_vpn(vpn_id)
        if not vpn:
            return {"ok": False, "message": L("That VPN isn't on the list any more. Press Detect.",
                                              "Esa VPN ya no está en la lista. Pulsa Detectar.")}
        if vpn_id == "wireguard":
            import wg_manager
            if not wg_manager.list_confs():
                return {"ok": False, "message": L("Add a WireGuard file first (Add files).",
                                                  "Añade antes un archivo WireGuard (Añadir archivos).")}
            ok, why = wg_manager.ensure_running(download_if_missing=True)
            if not ok:
                return {"ok": False, "message": L(f"The WireGuard tunnel didn't start: {why}.",
                                                  f"El túnel WireGuard no arrancó: {why}.")}
        old = get_egress_config()
        cfg = set_egress_config("vpn", str(old.get("custom_proxy") or "127.0.0.1:10808"),
                                str(old.get("custom_type") or "socks5"), old.get("warp_scope"),
                                extra={"vpn_id": vpn_id})
        bump_egress_epoch()   # open OpenCode connections reconnect through the VPN
        name = vpn["name"]
        if vpn_bind_address(vpn_id, max_age=0):
            msg = L(f"OpenCode now goes through {name}.", f"OpenCode ahora sale por {name}.")
        else:
            launched = self.open_vpn_app(vpn_id).get("ok")
            msg = L(f"OpenCode will go through {name}. Connect it in its app"
                    + (" (it's opening now)." if launched else "."),
                    f"OpenCode saldrá por {name}. Conéctala en su app"
                    + (" (se está abriendo)." if launched else "."))
        return {"ok": True, "config": cfg, "message": msg}

    def open_vpn_app(self, vpn_id: str = "") -> dict:
        from proxy_chain import get_egress_config
        from vpn_detect import get_vpn, launch_app
        vpn_id = vpn_id or str(get_egress_config().get("vpn_id") or "")
        if vpn_id == "wireguard":
            return self.wg_import()
        name = (get_vpn(vpn_id) or {}).get("name", "VPN")
        if launch_app(vpn_id):
            return {"ok": True, "message": L(f"Opening {name}.", f"Abriendo {name}.")}
        return {"ok": False, "message": L(f"Couldn't open {name}. Open it from the Start menu.",
                                          f"No se pudo abrir {name}. Ábrela desde el menú Inicio.")}

    def pick_vpn_exe(self) -> dict:
        """File picker for "Add by hand": the VPN's program, so we can show its real icon and open it."""
        try:
            import webview
            win = webview.windows[0] if webview.windows else None
            if not win:
                return {"ok": False, "path": ""}
            res = win.create_file_dialog(webview.FileDialog.OPEN, directory=os.environ.get("ProgramFiles", ""),
                                         file_types=(L("Programs (*.exe)", "Programas (*.exe)"),))
            path = (res[0] if isinstance(res, (list, tuple)) and res else res) or ""
            icon = ""
            if path:
                from vpn_detect import icon_data_uri
                icon = icon_data_uri("preview-" + Path(path).stem, str(path))
            return {"ok": bool(path), "path": str(path), "icon": icon}
        except Exception as exc:
            return {"ok": False, "path": "", "message": str(exc)}

    def add_manual_vpn(self, name: str, exe: str = "", adapter_name: str = "") -> dict:
        from vpn_detect import add_manual
        if not (name or "").strip():
            return {"ok": False, "message": L("Give it a name.", "Ponle un nombre.")}
        if not adapter_name:
            return {"ok": False, "message": L("Pick its network adapter. Connect the VPN first if it's not on the list.",
                                              "Elige su adaptador de red. Si no sale en la lista, conecta la VPN primero.")}
        item = add_manual(name, exe, adapter_name)
        return {"ok": True, "id": item["id"], "message": L(f"Added {item['name']}.", f"Añadida {item['name']}.")}

    def remove_manual_vpn(self, vpn_id: str) -> dict:
        from proxy_chain import get_egress_config
        from vpn_detect import remove_manual
        cfg = get_egress_config()
        if cfg.get("mode") == "vpn" and cfg.get("vpn_id") == vpn_id:
            return {"ok": False, "message": L("It's in use. Pick another connection first.",
                                              "Está en uso. Elige otra conexión primero.")}
        ok = remove_manual(vpn_id)
        return {"ok": ok, "message": L("Removed.", "Quitada.") if ok else L("Not found.", "No encontrada.")}

    # --- WireGuard files (Proton VPN Free and others), run through wireproxy -------------------------------

    def wg_import(self) -> dict:
        """File picker for WireGuard files (.conf). Several at once: each one is a server openhop can use."""
        try:
            import webview
            import wg_manager
            win = webview.windows[0] if webview.windows else None
            if not win:
                return {"ok": False, "message": ""}
            start = str(Path.home() / "Downloads")
            res = win.create_file_dialog(webview.FileDialog.OPEN, directory=start, allow_multiple=True,
                                         file_types=(L("WireGuard files (*.conf)", "Archivos WireGuard (*.conf)"),))
            paths = list(res) if isinstance(res, (list, tuple)) else ([res] if res else [])
            if not paths:
                return {"ok": False, "message": ""}
            added, problems = wg_manager.import_confs([str(x) for x in paths])
            msg = ""
            if added:
                n = len(added)
                msg = L(f"Added {n} WireGuard file{'s' if n != 1 else ''}.",
                        f"Añadido{'s' if n != 1 else ''} {n} archivo{'s' if n != 1 else ''} WireGuard.")
            if problems:
                msg += (" " if msg else "") + L("Not added: ", "No añadidos: ") + "; ".join(problems[:3])
            if added and not wg_manager.find_exe():
                ok, why = wg_manager.download()
                if not ok:
                    msg += " " + L(f"wireproxy couldn't be downloaded: {why}.", f"No se pudo descargar wireproxy: {why}.")
            return {"ok": bool(added), "message": msg, "added": added}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def wg_remove(self, name: str) -> dict:
        import wg_manager
        from proxy_chain import get_egress_config
        cfg = get_egress_config()
        in_use = cfg.get("mode") == "vpn" and cfg.get("vpn_id") == "wireguard"
        if in_use and len(wg_manager.list_confs()) <= 1:
            return {"ok": False, "message": L("It's the only one in use. Pick another connection first.",
                                              "Es el único y está en uso. Elige otra conexión primero.")}
        ok = wg_manager.remove_conf(name)
        if ok and in_use:
            wg_manager.ensure_running()
        return {"ok": ok, "message": L("Removed.", "Quitado.") if ok else L("Not found.", "No encontrado.")}

    def wg_use(self, name: str) -> dict:
        """Select a WireGuard file and apply it if the tunnel is in use."""
        import wg_manager
        from ouc_common import bump_egress_epoch
        if name not in [c["name"] for c in wg_manager.list_confs()]:
            return {"ok": False, "message": L("Not found.", "No encontrado.")}
        wg_manager.set_current(name)
        from proxy_chain import get_egress_config
        cfg = get_egress_config()
        if cfg.get("mode") == "vpn" and cfg.get("vpn_id") == "wireguard":
            ok, why = wg_manager.ensure_running(download_if_missing=True)
            bump_egress_epoch()
            if not ok:
                return {"ok": False, "message": L(f"It didn't start: {why}.", f"No arrancó: {why}.")}
        return {"ok": True, "message": L(f"Using {wg_manager._label(name)}.", f"Usando {wg_manager._label(name)}.")}

    def wg_start(self) -> dict:
        import wg_manager
        if not wg_manager.list_confs():
            return self.wg_import()
        ok, why = wg_manager.ensure_running(download_if_missing=True)
        return {"ok": ok, "message": L("WireGuard tunnel started.", "Túnel WireGuard iniciado.") if ok
                else L(f"It didn't start: {why}.", f"No arrancó: {why}.")}

    def set_warp_scope(self, scope: str) -> dict:
        from proxy_chain import AUTO_TOR_KEYS, get_egress_config, set_egress_config
        cfg = get_egress_config()
        clean_scope = "tun" if scope.lower().strip() == "tun" else "isolated"
        new_cfg = set_egress_config(
            mode=cfg.get("mode", "warp"),
            custom_proxy=cfg.get("custom_proxy", "127.0.0.1:10808"),
            custom_type=cfg.get("custom_type", "socks5"),
            warp_scope=clean_scope,
            extra={k: cfg[k] for k in AUTO_TOR_KEYS if k in cfg},  # same connection type: keep the auto-Tor mark
        )
        warp = find_warp_cli()
        if warp:
            apply_warp_mode(clean_scope, warp_bin=warp)
            run_warp(["connect"], warp_bin=warp)
        self._refresh_egress_async(force=True)
        label = (L("only OpenCode", "solo OpenCode") if clean_scope == "isolated"
                 else L("whole PC", "todo el PC"))
        return {"ok": True, "scope": clean_scope, "message": L(f"WARP now applies to: {label}",
                                                              f"WARP ahora se aplica a: {label}")}

    def set_egress_mode(self, mode: str, custom_proxy: str = "127.0.0.1:10808", custom_type: str = "socks5") -> dict:
        from proxy_chain import get_egress_config, set_egress_config
        if mode == "vpn":
            return self.set_vpn(str(get_egress_config().get("vpn_id") or ""))
        if mode != "custom":
            # WARP / Tor: keep the user's saved custom proxy list and WARP scope (the buttons don't send them,
            # and the defaults used to wipe the list).
            old = get_egress_config()
            custom_proxy = str(old.get("custom_proxy") or custom_proxy)
            custom_type = str(old.get("custom_type") or custom_type)
            cfg = set_egress_config(mode, custom_proxy, custom_type, old.get("warp_scope"))
        else:
            cfg = set_egress_config(mode, custom_proxy, custom_type, get_egress_config().get("warp_scope"))
        if mode == "tor":
            from tor_manager import get_active_tor_target, start_portable_tor
            if not get_active_tor_target():
                threading.Thread(target=start_portable_tor, daemon=True).start()
        return {"ok": True, "config": cfg, "message": L(f"Connection type switched to: {mode.upper()}",
                                                        f"Tipo de conexión cambiado a: {mode.upper()}")}

    def rotate_tor_circuit(self) -> dict:
        from tor_manager import rotate_tor_circuit
        from ouc_common import bump_egress_epoch
        ok, msg = rotate_tor_circuit()
        if ok:
            bump_egress_epoch()
        return {"ok": ok, "message": msg}

    def toggle_tor_service(self, enable: bool) -> dict:
        from tor_manager import start_portable_tor, stop_portable_tor
        ok, msg = start_portable_tor() if enable else stop_portable_tor()
        return {"ok": ok, "message": msg}

    def rotate_custom_proxy(self) -> dict:
        from proxy_chain import rotate_custom_proxy
        ok, msg = rotate_custom_proxy()
        return {"ok": ok, "message": msg}

    def set_clean_endpoint(self, endpoint: str) -> dict:
        from clean_endpoint import set_custom_endpoint
        ok, msg = set_custom_endpoint(endpoint)
        if ok:
            self._refresh_egress_async(force=True)
        return {"ok": ok, "message": msg}

    def reset_clean_endpoint(self) -> dict:
        from clean_endpoint import reset_custom_endpoint
        ok, msg = reset_custom_endpoint()
        if ok:
            self._refresh_egress_async(force=True)
        return {"ok": ok, "message": msg}

    def test_clean_endpoints(self, timeout: float = 1.2) -> dict:
        import concurrent.futures
        from clean_endpoint import KNOWN_CLEAN_ENDPOINTS, test_endpoint_latency

        def _check(ep: str) -> dict:
            lat = test_endpoint_latency(ep, timeout=timeout)
            return {"endpoint": ep, "latency": lat}

        with concurrent.futures.ThreadPoolExecutor(max_workers=10) as ex:
            res = list(ex.map(_check, KNOWN_CLEAN_ENDPOINTS))
        return {"ok": True, "endpoints": res}

    def warp_connect(self) -> dict:
        warp = find_warp_cli()
        if not warp:
            return {"ok": False, "message": L("Cloudflare WARP isn't installed (warp-cli.exe not found).",
                                              "Cloudflare WARP no está instalado (no se encuentra warp-cli.exe).")}
        scope = current_warp_scope()
        scope_name = L("only OpenCode", "solo OpenCode") if scope == "isolated" else L("whole PC", "todo el PC")

        run_warp(["registration", "new"], warp_bin=warp)
        apply_warp_mode(scope, warp_bin=warp)

        rc, msg = run_warp(["connect"], warp_bin=warp)
        for _ in range(8):
            time.sleep(1.0)
            conn, _, raw = warp_status(warp)
            if conn:
                self._refresh_egress_async(force=True)
                return {"ok": True, "message": L(f"WARP connected ({scope_name}).", f"WARP conectado ({scope_name}).")}
            if "unable" in raw.lower() or "mitm" in raw.lower():
                break

        # Auto-repair CF_HAPPY_EYEBALLS_MITM_FAILURE or broken endpoint
        run_warp(["disconnect"], warp_bin=warp)
        run_warp(["tunnel", "endpoint", "reset"], warp_bin=warp)
        ensure_warp_protocol(preferred_warp_protocol(scope), warp_bin=warp)
        run_warp(["tunnel", "rotate-keys"], warp_bin=warp)
        apply_warp_mode(scope, warp_bin=warp)
        run_warp(["connect"], warp_bin=warp)

        for _ in range(6):
            time.sleep(1.0)
            conn, _, _ = warp_status(warp)
            if conn:
                self._refresh_egress_async(force=True)
                return {"ok": True, "message": L(f"WARP repaired itself and connected ({scope_name}).",
                                                 f"WARP se ha reparado solo y está conectado ({scope_name}).")}

        return {"ok": False, "message": L("WARP is still connecting; press refresh in a moment.",
                                          "WARP todavía se está conectando; pulsa recargar en un momento.")}

    def warp_disconnect(self) -> dict:
        warp = find_warp_cli()
        if not warp:
            return {"ok": False, "message": L("Cloudflare WARP isn't installed (warp-cli.exe not found).",
                                              "Cloudflare WARP no está instalado (no se encuentra warp-cli.exe).")}
        rc, msg = run_warp(["disconnect"], warp_bin=warp)
        with self._egress_lock:
            self._egress_v6 = ""
        return {"ok": rc == 0, "message": L("WARP disconnected.", "WARP desconectado.") if rc == 0 else msg}

    def hop_now(self) -> dict:
        try:
            from proxy_chain import get_egress_config
            vcfg = get_egress_config()
        except Exception:
            vcfg = {}
        if vcfg.get("mode") == "vpn" and str(vcfg.get("vpn_id") or "") == "wireguard":
            return {"ok": True, "message": L("Choose a WireGuard server in Advanced → Your VPN.",
                                             "Elige un servidor WireGuard en Avanzado → Tu VPN.")}
        if vcfg.get("mode") == "vpn":
            return self.open_vpn_app()
        if not self._action_lock.acquire(blocking=False):
            return {"ok": False, "message": L("Another IP change or setup action is already running.",
                                              "Ya hay otro cambio de IP o ajuste en marcha.")}
        try:
            warp = find_warp_cli()
            if not warp:
                return {"ok": False, "message": L("Cloudflare WARP isn't installed (warp-cli.exe not found).",
                                                  "Cloudflare WARP no está instalado (no se encuentra warp-cli.exe).")}
            old_v6 = self._egress_v6 or (get_warp_local_v6() or "")
            with keeper_pause_ctx("avatar-ui-hop", seconds=180):
                new_v6 = hop_once(warp, max_reg_tries=5)
            if not new_v6:
                return {"ok": False, "message": L("Couldn't get a new IP. openhop will try again by itself.",
                                                  "No se pudo conseguir una IP nueva. openhop lo volverá a intentar solo.")}
            with self._egress_lock:
                self._egress_v6 = new_v6
                self._last_egress_ts = time.time()
            # Trigger edge re-roll on persistent proxy
            if (LANE_DIR / "edge_override.txt").is_file():
                ov = (LANE_DIR / "edge_override.txt").read_text(encoding="utf-8").strip().lower()
            else:
                ov = "auto"
            if ov == "auto":
                (LANE_DIR / "edge_reroll").write_text(str(time.time()), encoding="utf-8")
            # Record hop in state.json
            if GUARD_STATE_FILE.is_file():
                try:
                    st = json.loads(GUARD_STATE_FILE.read_text(encoding="utf-8"))
                except Exception:
                    st = {}
            else:
                st = {}
            st["hops"] = int(st.get("hops", 0) or 0) + 1
            st["last_hop"] = int(time.time())
            st["current"] = new_v6
            GUARD_STATE_FILE.write_text(json.dumps(st), encoding="utf-8")
            old_p = normalize_v6_64(old_v6) or "?"
            new_p = normalize_v6_64(new_v6) or new_v6
            return {"ok": True, "message": L(f"New IP: {old_p} -> {new_p}", f"IP nueva: {old_p} -> {new_p}")}
        finally:
            self._action_lock.release()

    def toggle_worker(self, name: str, enable: bool) -> dict:
        if name not in ("keeper", "guard"):
            return {"ok": False, "message": L("Unknown worker.", "Proceso desconocido.")}
        label = "WARP Keeper" if name == "keeper" else "Zen Guard"

        if not enable:
            ensure_dirs()
            try:
                _disabled_flag(name).write_text("1", encoding="utf-8")
            except Exception:
                pass
            killed = _stop_worker_processes(name)
            if name == "keeper":
                # The keeper hosts the proxy OpenCode is wired to; unwire it so OpenCode
                # connects directly instead of failing with ECONNREFUSED.
                self.toggle_desktop_service_proxy(False)
            return {"ok": True, "message": L(f"Stopped {label} ({killed} process{'es' if killed != 1 else ''}).",
                                             f"{label} detenido ({killed} proceso{'s' if killed != 1 else ''}).")}

        _disabled_flag(name).unlink(missing_ok=True)
        ok, how = _start_worker(name)
        if ok:
            return {"ok": True, "message": L(f"{label} is running.", f"{label} está en marcha.")}
        return {
            "ok": False,
            "message": L(f"Couldn't start {label}. Press 'Install / Repair' at the bottom, then try again.",
                         f"No se pudo iniciar {label}. Pulsa 'Instalar / Reparar' abajo y vuelve a intentarlo."),
        }

    def toggle_desktop_service_proxy(self, enable: bool) -> dict:
        proxy_url = f"http://127.0.0.1:{SERVICE_PROXY_PORT}"
        cfg_file = Path.home() / ".config" / "opencode" / "service.json"

        # 1. If enabling, ensure keeper is running to host port 41987
        if enable:
            if not _port_open(SERVICE_PROXY_PORT):
                self.toggle_worker("keeper", True)
                self._ensure_proxy_active(wait=6.0)
            if not _port_open(SERVICE_PROXY_PORT):
                return {
                    "ok": False,
                    "message": L("The background proxy couldn't start, so OpenCode was left on its normal connection. "
                                 "Press 'Install / Repair' and try again.",
                                 "El proxy en segundo plano no pudo arrancar, así que OpenCode se ha quedado con su "
                                 "conexión normal. Pulsa 'Instalar / Reparar' y vuelve a intentarlo."),
                }

        # 2. Update service.json directly in Python (100% reliable, zero CLI dependencies)
        try:
            cfg = {}
            if cfg_file.is_file():
                try:
                    cfg = json.loads(cfg_file.read_text(encoding="utf-8"))
                except Exception:
                    pass
            if not isinstance(cfg.get("env"), dict):
                cfg["env"] = {}

            if enable:
                for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
                    cfg["env"][k] = proxy_url
                for k in ("NO_PROXY", "no_proxy"):
                    cfg["env"][k] = "127.0.0.1,localhost,::1"
            else:
                for k in _SERVICE_ENV_KEYS:
                    cfg["env"].pop(k, None)
                if not cfg["env"]:
                    cfg.pop("env", None)

            cfg_file.parent.mkdir(parents=True, exist_ok=True)
            cfg_file.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
        except Exception as exc:
            return {"ok": False, "message": L(f"Couldn't update OpenCode's service.json: {exc}",
                                              f"No se pudo actualizar service.json de OpenCode: {exc}")}

        # 3. If opencode CLI is present, run official commands as well
        oc = find_opencode_cli()
        if oc:
            try:
                if enable:
                    run_hidden([oc, "service", "set", "env", "HTTPS_PROXY", proxy_url], timeout=4.0)
                    run_hidden([oc, "service", "set", "env", "HTTP_PROXY", proxy_url], timeout=4.0)
                    run_hidden([oc, "service", "set", "env", "NO_PROXY", "127.0.0.1,localhost,::1"], timeout=4.0)
                else:
                    for k in _SERVICE_ENV_KEYS:
                        run_hidden([oc, "service", "unset", "env", k], timeout=4.0)
                run_hidden([oc, "service", "restart"], timeout=5.0)
            except Exception:
                pass

        # 4. Kill active opencode-cli background daemons so OpenCode restarts them with fresh settings
        try:
            import psutil
            for p in psutil.process_iter(["name", "exe"]):
                try:
                    pname = (p.info["name"] or "").lower()
                    pexe = p.info["exe"] or ""
                    if "opencode-cli" in pname and not is_own_path(pexe):
                        p.kill()
                except Exception:
                    pass
        except Exception:
            pass

        return {
            "ok": True,
            "message": (
                L(f"OpenCode Desktop now goes through openhop ({proxy_url}).",
                  f"OpenCode Desktop ahora pasa por openhop ({proxy_url}).")
                if enable
                else L("OpenCode Desktop is back on its normal connection.",
                       "OpenCode Desktop vuelve a usar su conexión normal.")
            ),
        }

    def toggle_autostart(self, enable: bool) -> dict:
        if not enable:
            for tn in (TASK_KEEPER, TASK_GUARD):
                run_hidden(["schtasks", "/Delete", "/TN", tn, "/F"], timeout=8.0)
            return {"ok": True, "message": L("Protection no longer starts when you sign in.",
                                             "La protección ya no se activa al iniciar sesión.")}

        _deploy_scripts_and_shims()
        pyw = _find_pythonw()
        for tn, script_name, flag in (
            (TASK_KEEPER, "warp_keeper.py", "keeper"),
            (TASK_GUARD, "zen_guard.py", "guard"),
        ):
            if pyw and (SCRIPTS_DIR / script_name).is_file():
                exe, ps_arg = pyw, f"'\"{SCRIPTS_DIR / script_name}\"'"
            else:
                exe, ps_arg = sys.executable, f"'--worker {flag}'"
            ps = (
                f"$a=New-ScheduledTaskAction -Execute '{exe}' -Argument {ps_arg};"
                f"$t=New-ScheduledTaskTrigger -AtLogOn -User \"$env:USERDOMAIN\\$env:USERNAME\";"
                f"$s=New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
                f"-StartWhenAvailable -ExecutionTimeLimit (New-TimeSpan -Seconds 0) -RestartCount 3 "
                f"-RestartInterval (New-TimeSpan -Minutes 1) -MultipleInstances IgnoreNew;"
                f"Unregister-ScheduledTask -TaskName '{tn}' -Confirm:$false -ErrorAction SilentlyContinue;"
                f"Register-ScheduledTask -TaskName '{tn}' -Action $a -Trigger $t -Settings $s | Out-Null"
            )
            res = run_hidden(["powershell.exe", "-NoProfile", "-Command", ps], timeout=12.0)
            if res.returncode != 0:
                return {"ok": False, "message": L(f"Couldn't set up the sign-in tasks: {res.stderr.strip()}",
                                                  f"No se pudieron crear las tareas de inicio de sesión: {res.stderr.strip()}")}
        return {"ok": True, "message": L("Protection now starts by itself when you sign in.",
                                         "La protección ahora se activa sola al iniciar sesión.")}

    def toggle_firewall(self, enable: bool) -> dict:
        action = "install" if enable else "remove"
        if is_admin():
            ok, msg = install_killswitch() if enable else remove_killswitch()
            return {"ok": ok, "message": msg}

        py = shutil.which("python.exe")
        if py and "WindowsApps" not in py and (SCRIPTS_DIR / "ouc_firewall.py").is_file():
            exe, params = py, f'"{SCRIPTS_DIR / "ouc_firewall.py"}" {action}'
        else:
            exe, params = sys.executable, f"--firewall {action}"

        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe, params, None, 0)
        if rc <= 32:
            return {"ok": False, "message": L("Windows didn't give administrator permission (the prompt was declined).",
                                              "Windows no dio permiso de administrador (se rechazó la ventana).")}
        for _ in range(12):
            time.sleep(0.35)
            if is_killswitch_installed() == enable:
                break
        state_ok = is_killswitch_installed() == enable
        return {
            "ok": state_ok,
            "message": (
                L("Leak blocker is on: the OpenCode CLI can't skip openhop anymore.",
                  "Bloqueo de fugas activado: la CLI de OpenCode ya no puede saltarse openhop.")
                if enable and state_ok
                else L("Leak blocker is off.", "Bloqueo de fugas desactivado.")
                if not enable and state_ok
                else L("Firewall change sent; refresh in a moment.",
                       "Cambio del firewall enviado; recarga en un momento.")
            ),
        }

    def _is_auto_resume_active(self) -> bool:
        try:
            from auto_resume import is_auto_resume_enabled
            return is_auto_resume_enabled()
        except Exception:
            return True

    def toggle_auto_resume(self, enable: bool) -> dict:
        try:
            from auto_resume import set_auto_resume
            set_auto_resume(enable)
            return {
                "ok": True,
                "message": (
                    L("Auto-continue is on: openhop sends 'continue' for you after it switches to a new IP.",
                      "Seguir solo está activado: openhop envía 'continue' por ti después de cambiar de IP.")
                    if enable
                    else L("Auto-continue is off.", "Seguir solo está desactivado.")
                ),
            }
        except Exception as exc:
            return {"ok": False, "message": L(f"Couldn't change auto-continue: {exc}",
                                              f"No se pudo cambiar seguir solo: {exc}")}

    def toggle_strict_mode(self, enable: bool) -> dict:
        ensure_dirs()
        flag = LANE_DIR / "strict_proxy.flag"
        if enable:
            flag.write_text("1\n", encoding="utf-8")
            return {"ok": True, "message": L("Strict mode is on: OpenCode only connects through the protected connection.",
                                             "Modo estricto activado: OpenCode solo se conecta por la conexión protegida.")}
        flag.unlink(missing_ok=True)
        return {"ok": True, "message": L("Strict mode is off: OpenCode keeps working if the protected connection drops.",
                                         "Modo estricto desactivado: OpenCode sigue funcionando si se cae la conexión protegida.")}

    def auto_heal_endpoint(self) -> dict:
        try:
            from clean_endpoint import auto_heal_warp_endpoint
            ok, msg = auto_heal_warp_endpoint()
            if ok:
                self._refresh_egress_async(force=True)
            return {"ok": ok, "message": msg}
        except Exception as exc:
            return {"ok": False, "message": L(f"Couldn't pick the fastest entry point: {exc}",
                                              f"No se pudo elegir el punto de entrada más rápido: {exc}")}

    def _warp_path_ok(self, wb: str) -> bool:
        """Check with a real request that traffic goes through WARP; reconnect up to twice if it leaks."""
        try:
            from ouc_common import record_warp_path, trace_warp_path
        except Exception:
            return True
        for attempt in range(3):
            info = trace_warp_path(timeout=6.0)
            if info:
                record_warp_path(info)
            if info.get("warp") != "off":
                return True   # on/plus, or no answer (the other health checks cover "no internet")
            if attempt == 2:
                break
            run_warp(["disconnect"], warp_bin=wb)
            time.sleep(1.0)
            run_warp(["connect"], warp_bin=wb)
            for _ in range(10):
                time.sleep(1.0)
                if warp_status(wb)[0]:
                    break
        return False

    def repair_warp_connection(self) -> dict:
        """Purges stale cryptographic keys, resets endpoint overrides, and auto-repairs MITM errors."""
        wb = find_warp_cli()
        if not wb:
            return {"ok": False, "message": L("Cloudflare WARP isn't installed (warp-cli.exe not found).",
                                              "Cloudflare WARP no está instalado (no se encuentra warp-cli.exe).")}
        try:
            scope = current_warp_scope()
            scope_name = L("only OpenCode", "solo OpenCode") if scope == "isolated" else L("whole PC", "todo el PC")

            # Phase 1: Rapid key rotation and endpoint reset (preserves active client registration)
            run_warp(["tunnel", "endpoint", "reset"], warp_bin=wb)
            try:
                ensure_warp_protocol(preferred_warp_protocol(scope), warp_bin=wb)
            except Exception:
                pass
            run_warp(["tunnel", "rotate-keys"], warp_bin=wb)
            apply_warp_mode(scope, warp_bin=wb)
            run_warp(["connect"], warp_bin=wb)

            for _ in range(5):
                time.sleep(1.0)
                conn, _, raw = warp_status(wb)
                raw_low = raw.lower()
                # "unstable" simply means ping latency is elevated; traffic still flows normally!
                if conn and "mitm" not in raw_low and "unable" not in raw_low:
                    # "Connected" isn't enough: WARP can say that while sending traffic out with the real IP.
                    if not self._warp_path_ok(wb):
                        return {"ok": False, "message": L(
                            "WARP says it's connected, but traffic still leaves with your own IP. openhop keeps "
                            "OpenCode blocked so your IP isn't used. Quit Cloudflare WARP from the tray and open "
                            "it again, then press Repair once more.",
                            "WARP dice que está conectado, pero el tráfico sigue saliendo con tu propia IP. openhop "
                            "mantiene OpenCode bloqueado para que no se use tu IP. Cierra Cloudflare WARP desde la "
                            "bandeja, vuelve a abrirlo y pulsa Reparar otra vez.")}
                    self._refresh_egress_async(force=True)
                    return {"ok": True, "message": L(f"WARP repaired. Traffic goes through Cloudflare again ({scope_name}).",
                                                     f"WARP reparado. El tráfico vuelve a pasar por Cloudflare ({scope_name}).")}

            # Phase 2: Re-register only if registration is actually corrupted, missing, or blocked by MITM
            if "registration missing" in raw_low or "not registered" in raw_low or "mitm" in raw_low:
                run_warp(["disconnect"], warp_bin=wb)
                run_warp(["registration", "delete"], warp_bin=wb)
                run_warp(["registration", "new"], warp_bin=wb)
                try:
                    ensure_warp_protocol(preferred_warp_protocol(scope), warp_bin=wb)
                except Exception:
                    pass
                apply_warp_mode(scope, warp_bin=wb)
                run_warp(["connect"], warp_bin=wb)

                for _ in range(6):
                    time.sleep(1.0)
                    conn, _, _ = warp_status(wb)
                    if conn:
                        self._refresh_egress_async(force=True)
                        return {"ok": True, "message": L(f"WARP was reset and is connected again ({scope_name}).",
                                                         f"WARP se ha reiniciado y vuelve a estar conectado ({scope_name}).")}

            return {"ok": True, "message": L("WARP was reset. It's reconnecting in the background…",
                                             "WARP se ha reiniciado. Se está reconectando en segundo plano…")}
        except Exception as exc:
            return {"ok": False, "message": L(f"Repair failed: {exc}", f"La reparación falló: {exc}")}

    def reroll_edge(self) -> dict:
        ensure_dirs()
        (LANE_DIR / "edge_override.txt").write_text("auto\n", encoding="utf-8")
        (LANE_DIR / "edge_reroll").write_text(str(time.time()), encoding="utf-8")
        time.sleep(1.7)
        edge = ""
        if (LANE_DIR / "last_edge.txt").is_file():
            edge = (LANE_DIR / "last_edge.txt").read_text(encoding="utf-8").strip()
        return {"ok": True, "message": L(f"Switched OpenCode server: {edge or 'on the next request'}",
                                         f"Servidor de OpenCode cambiado: {edge or 'en la próxima petición'}")}

    def set_edge_override(self, edge: str) -> dict:
        ensure_dirs()
        clean = edge.strip() or "auto"
        (LANE_DIR / "edge_override.txt").write_text(clean + "\n", encoding="utf-8")
        time.sleep(1.6)
        shown = clean if clean != "auto" else L("automatic", "automático")
        return {"ok": True, "message": L(f"OpenCode server: {shown}", f"Servidor de OpenCode: {shown}")}

    def refresh_edge_pool(self) -> dict:
        pool = fetch_opencode_aaaa_pool()
        self._edge_pool = pool
        return {"ok": True, "pool": pool, "message": L(f"Found {len(pool)} opencode.ai servers.",
                                                       f"Encontrados {len(pool)} servidores de opencode.ai.")}

    def deep_reset_identity(self) -> dict:
        """Deep clean transient caches & rotate network identity without touching user chats/sessions."""
        import shutil, tempfile

        cleared: list[str] = []

        # Clear transient client caches only (never touch opencode.db or user chat sessions!)
        base_dir = Path.home() / ".local" / "share" / "opencode"
        if base_dir.is_dir():
            for item_name in ("storage", "olliecode-fingerprint.json"):
                target = base_dir / item_name
                if target.is_file():
                    try:
                        target.unlink(missing_ok=True)
                        cleared.append(item_name)
                    except Exception:
                        pass
                elif target.is_dir():
                    try:
                        shutil.rmtree(target, ignore_errors=True)
                        cleared.append(item_name)
                    except Exception:
                        pass

        for extra_cache in [
            Path.home() / ".cache" / "opencode",
            Path.home() / ".local" / "state" / "opencode",
            Path(os.environ.get("LOCALAPPDATA", "")) / "opencode" / "cache" if os.environ.get("LOCALAPPDATA") else None,
            Path(tempfile.gettempdir()) / "opencode",
        ]:
            if extra_cache and extra_cache.is_dir():
                try:
                    shutil.rmtree(extra_cache, ignore_errors=True)
                    cleared.append(extra_cache.name)
                except Exception:
                    pass

        # Clear client-side rate limit lockouts from drafts.sqlite
        fl_cleared = clear_opencode_free_limit()
        if fl_cleared > 0:
            cleared.append(f"{fl_cleared} limit entries")

        # Unblock port 42914
        free_service_port(42914)

        # Clear guard blacklist and reset edge override
        self.clear_blacklist()
        self.set_edge_override("auto")
        self.repair_warp_connection()

        rot_msg = ""
        try:
            from proxy_chain import get_egress_config
            mode = get_egress_config().get("mode", "warp")
            if mode == "tor":
                from tor_manager import rotate_tor_circuit
                _, rot_msg = rotate_tor_circuit()
            elif mode == "custom":
                from proxy_chain import rotate_custom_proxy
                _, rot_msg = rotate_custom_proxy()
            else:
                from ouc_common import find_warp_cli, hop_once
                wbin = find_warp_cli()
                if wbin:
                    new_ip = hop_once(wbin)
                    rot_msg = (L(f"New WARP IP: {new_ip}", f"IP nueva de WARP: {new_ip}") if new_ip
                               else L("WARP reconnected.", "WARP reconectado."))
        except Exception as exc:
            rot_msg = L(f"Note about the new IP: {exc}", f"Nota sobre la IP nueva: {exc}")

        return {
            "ok": True,
            "message": L(f"Deep reset done. Cleared: {', '.join(cleared) or 'nothing to clear'}. Your chats are kept. {rot_msg}",
                         f"Reinicio a fondo hecho. Borrado: {', '.join(cleared) or 'nada que borrar'}. "
                         f"Tus chats se conservan. {rot_msg}"),
        }

    def clear_blacklist(self) -> dict:
        if not GUARD_STATE_FILE.is_file():
            return {"ok": True, "message": L("The used-IP list was already empty.", "La lista de IPs usadas ya estaba vacía.")}
        try:
            st = json.loads(GUARD_STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            st = {}
        st["blacklist"] = []
        GUARD_STATE_FILE.write_text(json.dumps(st), encoding="utf-8")
        return {"ok": True, "message": L("Used-IP list cleared.", "Lista de IPs usadas borrada.")}

    def _brand_pending_cached(self) -> bool:
        """desktop_brand.pending(), checked at most once a minute (it reads OpenCode's app.asar header)."""
        now = time.time()
        cached = getattr(self, "_brand_pending", None)
        if cached and now - cached[0] < 60:
            return cached[1]
        try:
            import desktop_brand
            val = bool(desktop_brand.pending())
        except Exception:
            val = False
        self._brand_pending = (now, val)
        return val

    def restart_opencode_desktop(self) -> dict:
        """Close OpenCode Desktop, put openhop's changes in (desktop_brand.sync) and open it again, protected.

        The chats live in OpenCode's own database, so closing the window keeps them. A polite close first (like the
        X button), then a forced one for anything still open after a few seconds.
        """
        try:
            import psutil
            import desktop_brand
            d = desktop_brand.desktop_dir()
            if not d:
                return {"ok": False, "message": L("OpenCode Desktop isn't installed.", "OpenCode Desktop no está instalado.")}
            exe = str(d / "OpenCode.exe").lower()

            def procs() -> list:
                out = []
                for p in psutil.process_iter(["name", "exe"]):
                    try:
                        if (p.info.get("name") or "").lower() == "opencode.exe" and (p.info.get("exe") or "").lower() == exe:
                            out.append(p)
                    except Exception:
                        pass
                return out

            running = procs()
            if running:
                no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0)
                for p in running:
                    try:  # without /F, taskkill asks the window to close, like pressing X
                        subprocess.run(["taskkill", "/PID", str(p.pid)], capture_output=True, timeout=10,
                                       creationflags=no_window)
                    except Exception:
                        pass
                _gone, alive = psutil.wait_procs(running, timeout=8)
                for p in alive:
                    try:
                        p.kill()
                    except Exception:
                        pass
                psutil.wait_procs(alive, timeout=5)
            if desktop_brand.desktop_running():
                return {"ok": False, "message": L("OpenCode didn't close. Close it yourself and openhop finishes "
                                                  "within a couple of minutes.",
                                                  "OpenCode no se ha cerrado. Ciérralo tú y openhop termina en un "
                                                  "par de minutos.")}
            desktop_brand.sync()
            self._brand_pending = None
            still = desktop_brand.status()
            res = self.launch_desktop()
            if not res.get("ok"):
                return res
            if still.get("extras") is False and desktop_brand.extras_setting_enabled():
                return {"ok": True, "message": L("OpenCode is opening again, but openhop couldn't add its changes. "
                                                 "Use Report a problem so we can look at it.",
                                                 "OpenCode se está abriendo otra vez, pero openhop no ha podido poner "
                                                 "sus cambios. Usa Informar de un problema para que lo miremos.")}
            return {"ok": True, "message": L("Done. OpenCode is opening again with openhop's changes.",
                                             "Hecho. OpenCode se está abriendo otra vez con los cambios de openhop.")}
        except Exception as exc:
            return {"ok": False, "message": L(f"Couldn't restart OpenCode: {exc}", f"No se pudo reiniciar OpenCode: {exc}")}

    def launch_desktop(self) -> dict:
        desktop = find_opencode_desktop()
        if not desktop:
            return {"ok": False, "message": L("OpenCode Desktop isn't installed (OpenCode.exe not found).",
                                              "OpenCode Desktop no está instalado (no se encuentra OpenCode.exe).")}

        # Clear any client-side rate limit lockouts
        clear_opencode_free_limit()

        # Free port 42914 to ensure OpenCode managed service starts without IPC collision
        free_service_port(42914)

        if not _port_open(SERVICE_PROXY_PORT):
            self.toggle_worker("keeper", True)
            self._ensure_proxy_active(wait=6.0)
        svc_cfg = Path.home() / ".config" / "opencode" / "service.json"
        already_wired = False
        if svc_cfg.is_file():
            try:
                cfg = json.loads(svc_cfg.read_text(encoding="utf-8"))
                env_map = cfg.get("env", {}) if isinstance(cfg.get("env"), dict) else {}
                already_wired = f"127.0.0.1:{SERVICE_PROXY_PORT}" in str(env_map.get("HTTPS_PROXY", ""))
            except Exception:
                pass
        if not already_wired:
            self.toggle_desktop_service_proxy(True)
        env = os.environ.copy()
        proxy_url = f"http://127.0.0.1:{SERVICE_PROXY_PORT}"
        env["HTTPS_PROXY"] = proxy_url
        env["HTTP_PROXY"] = proxy_url
        env["NO_PROXY"] = "127.0.0.1,localhost,::1"
        args_str = f"--proxy-server={proxy_url} --proxy-bypass-list=127.0.0.1;localhost;::1;*.local"
        launched = False
        if sys.platform == "win32" and hasattr(os, "startfile"):
            try:
                os.startfile(desktop, arguments=args_str)
                launched = True
            except Exception:
                pass
        if not launched:
            flags = getattr(subprocess, "DETACHED_PROCESS", 0x00000008) if sys.platform == "win32" else 0
            subprocess.Popen(
                [desktop, f"--proxy-server={proxy_url}", "--proxy-bypass-list=127.0.0.1;localhost;::1;*.local"],
                env=env,
                creationflags=flags,
                close_fds=False if sys.platform == "win32" else True,
            )
        return {"ok": True, "message": L("OpenCode Desktop is opening, already protected.",
                                         "OpenCode Desktop se está abriendo, ya protegido.")}

    def fix_opencode_service(self) -> dict:
        """Terminate orphaned daemons, unblock busy service ports, and restart clean."""
        killed = 0
        try:
            import psutil
            for p in psutil.process_iter(["pid", "name", "exe"]):
                if p.pid == os.getpid():
                    continue
                name = (p.info.get("name") or "").lower()
                exe = (p.info.get("exe") or "").lower()
                if is_own_path(exe):
                    continue
                if "opencode-cli" in name or name == "opencode.exe":
                    try:
                        p.kill()
                        killed += 1
                    except Exception:
                        pass
        except Exception:
            pass

        # Aggressively unblock managed service port 42914
        killed += free_service_port(42914)

        # Clear client-side rate limit lockouts
        fl_cleared = clear_opencode_free_limit()

        # Stop service via CLI if installed
        cli = find_opencode_cli()
        if cli:
            try:
                subprocess.run([cli, "service", "stop"], capture_output=True, timeout=5.0)
            except Exception:
                pass

        # Reset service.json proxy env: wire to proxy only if port 41987 is alive, otherwise restore direct
        if _port_open(SERVICE_PROXY_PORT):
            self.toggle_desktop_service_proxy(True)
        else:
            self.toggle_desktop_service_proxy(False)
        extra = L(f", cleared {fl_cleared} limit entries", f", {fl_cleared} avisos de límite borrados") if fl_cleared > 0 else ""
        return {"ok": True, "message": L(f"Port conflict fixed ({killed} stuck processes closed{extra}). Ready to open OpenCode.",
                                         f"Conflicto de puerto arreglado ({killed} procesos atascados cerrados{extra}). "
                                         f"Listo para abrir OpenCode.")}

    def launch_cli(self) -> dict:
        _deploy_scripts_and_shims()
        shim = BIN_DIR / "opencode.cmd"
        if not shim.is_file():
            return {"ok": False, "message": L("The 'opencode' command isn't ready yet; press Install / Repair first.",
                                              "El comando 'opencode' aún no está listo; pulsa Instalar / Reparar primero.")}
        wt = shutil.which("wt.exe")
        use_close_fds = False if sys.platform == "win32" else True
        if wt:
            subprocess.Popen([wt, "cmd.exe", "/k", str(shim)], close_fds=use_close_fds)
        else:
            subprocess.Popen(
                ["cmd.exe", "/c", "start", "OpenCode (openhop)", "cmd.exe", "/k", str(shim)],
                shell=False,
                close_fds=use_close_fds,
            )
        return {"ok": True, "message": L("Opened a terminal with OpenCode, already protected.",
                                         "Se ha abierto una terminal con OpenCode, ya protegido.")}

    def install_or_repair(self) -> dict:
        if not self._action_lock.acquire(blocking=False):
            return {"ok": False, "message": L("Another setup action is in progress.", "Ya hay otro ajuste en marcha.")}
        try:
            if not find_warp_cli():
                return {"ok": False, "message": L("Cloudflare WARP (warp-cli.exe) not found. Install WARP from https://1.1.1.1/ "
                                                  "or run install.bat first.",
                                                  "No se encuentra Cloudflare WARP (warp-cli.exe). Instala WARP desde "
                                                  "https://1.1.1.1/ o ejecuta install.bat primero.")}
            if not find_opencode_cli():
                return {"ok": False, "message": L("OpenCode CLI not found. Install OpenCode from https://opencode.ai or run "
                                                  "install.bat first.",
                                                  "No se encuentra la CLI de OpenCode. Instala OpenCode desde "
                                                  "https://opencode.ai o ejecuta install.bat primero.")}
            _deploy_scripts_and_shims()
            _update_user_path(True)
            self.toggle_autostart(True)
            self.toggle_worker("keeper", True)
            self.toggle_worker("guard", True)
            self.warp_connect()
            self.toggle_desktop_service_proxy(True)
            self._refresh_egress_async(force=True)
            return {"ok": True, "message": L("Everything is in place: background protection, startup, the 'opencode' "
                                             "command and OpenCode Desktop's connection.",
                                             "Todo está en su sitio: protección en segundo plano, inicio, el comando "
                                             "'opencode' y la conexión de OpenCode Desktop.")}
        finally:
            self._action_lock.release()

    def uninstall_all(self) -> dict:
        if not self._action_lock.acquire(blocking=False):
            return {"ok": False, "message": L("Another setup action is in progress.", "Ya hay otro ajuste en marcha.")}
        try:
            try:
                import desktop_brand
                from openhop_tray import save_setting
                save_setting("desktop_brand", False)  # so the keeper doesn't put it back meanwhile
                save_setting(desktop_brand.BANNER_KEY, False)
                save_setting(desktop_brand.EXTRAS_KEY, False)
                st = desktop_brand.status()
                if st.get("patched") or st.get("banner_hidden"):
                    ok, _msg = desktop_brand.restore_all()
                    if not ok:
                        return {"ok": False, "message": L("Close OpenCode Desktop first, then uninstall again "
                                                          "(openhop needs to put OpenCode's start screen back).",
                                                          "Cierra primero OpenCode Desktop y vuelve a desinstalar "
                                                          "(openhop tiene que dejar como estaba la pantalla de inicio de OpenCode).")}
            except Exception:
                pass
            self.toggle_autostart(False)
            self.toggle_worker("guard", False)
            self.toggle_worker("keeper", False)
            self.toggle_desktop_service_proxy(False)
            if is_killswitch_installed():
                self.toggle_firewall(False)
            _update_user_path(False)
            warp = find_warp_cli()
            if warp:
                run_warp(["disconnect"], warp_bin=warp)
            for tn in (OLD_TASK_KEEPER, OLD_TASK_GUARD):
                run_hidden(["schtasks", "/Delete", "/TN", tn, "/F"], timeout=8.0)
            for d in (DEST_DIR, OLD_DEST_DIR, OLD_DEST_DIR.with_name(OLD_DEST_DIR.name + ".old")):
                if d.is_dir():
                    shutil.rmtree(d, ignore_errors=True)
            with self._egress_lock:
                self._egress_v6 = ""
            return {"ok": True, "message": L("openhop was uninstalled. OpenCode is back to normal.",
                                             "openhop se ha desinstalado. OpenCode vuelve a estar como antes.")}
        finally:
            self._action_lock.release()

    def open_logs_folder(self) -> dict:
        """The data folder (settings, guard/keeper logs). Kept under this name: the Setup button calls it."""
        ensure_dirs()
        os.startfile(str(DEST_DIR))  # type: ignore[attr-defined]
        return {"ok": True, "message": L(f"Opened {DEST_DIR}", f"Abierto {DEST_DIR}")}

    def open_logs_dir(self) -> dict:
        """%LOCALAPPDATA%\\openhop\\logs: what support asks for, without anyone typing %LOCALAPPDATA%."""
        logs = DEST_DIR / "logs"
        try:
            logs.mkdir(parents=True, exist_ok=True)
            os.startfile(str(logs))  # type: ignore[attr-defined]
        except Exception as exc:
            return {"ok": False, "message": L(f"Couldn't open the logs folder: {exc}",
                                              f"No se pudo abrir la carpeta de registros: {exc}")}
        return {"ok": True, "message": L("Opened the logs folder.", "Carpeta de registros abierta.")}

    def copy_text(self, text: str) -> dict:
        try:
            subprocess.run(["clip.exe"], input=text, text=True, creationflags=CREATE_NO_WINDOW, timeout=3.0)
            return {"ok": True, "message": L(f"Copied: {text}", f"Copiado: {text}")}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    def open_external_url(self, url: str) -> dict:
        try:
            webbrowser.open(url)
            return {"ok": True, "url": url}
        except Exception as exc:
            return {"ok": False, "message": str(exc)}

    # ---- "Report a problem" (src/report_bundle.py builds the redacted report; web/src/report.mjs receives it) ----
    _report_lock = threading.Lock()      # one bundle build at a time (preview and send share it)
    _report_send_lock = threading.Lock()
    _REPORT_URL = "https://openhop.cfd/api/report"
    _REPORT_MAX_FILES = 5
    _REPORT_MAX_BYTES = 7 * 1024 * 1024  # attachments, total
    _REPORT_IMAGE_EXT = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
                         ".gif": "image/gif", ".webp": "image/webp"}
    _REPORT_TEXT_EXT = {".log", ".txt"}

    def _report_extra(self) -> dict:
        """What the health panel shows right now (titles only), so the report matches what the user saw."""
        cached = self._state_cache
        if not cached:
            return {}
        st = cached[1] or {}
        h = st.get("health") or {}
        return {
            "status": h.get("status"), "title": h.get("title"),
            "issues": [i.get("title") for i in h.get("issues", [])],
            "recent_errors": [{"title": e.get("title"), "count": e.get("count"), "where": e.get("where")}
                              for e in (st.get("recent_errors") or [])][:10],
            "services": st.get("services"),
        }

    def _report_bundle(self, refresh: bool = False) -> dict:
        import report_bundle
        with self._report_lock:
            cached = getattr(self, "_report_cache", None)
            if cached and not refresh and time.time() - cached[0] < 600:
                return cached[1]
            bundle = report_bundle.collect(extra=self._report_extra())
            self._report_cache = (time.time(), bundle)
            return bundle

    def _report_file_list(self) -> dict:
        files = getattr(self, "_report_files", [])
        items = [{"name": p.name, "size": size} for p, size in files]
        return {"ok": True, "files": items, "total": sum(i["size"] for i in items),
                "max_files": self._REPORT_MAX_FILES, "max_bytes": self._REPORT_MAX_BYTES}

    def report_preview(self, refresh: bool = False) -> dict:
        try:
            b = self._report_bundle(refresh=bool(refresh))
            return {"ok": True, "text": b["text"], "size": len(b["text"].encode("utf-8")), "summary": b["summary"]}
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "report.log", f"preview failed: {exc!r}")
            return {"ok": False, "code": "collect", "text": ""}

    def report_pick_files(self) -> dict:
        w = self._window or (webview.windows[0] if webview.windows else None)
        if w is None:
            return {**self._report_file_list(), "ok": False, "code": "no_window"}
        try:
            picked = w.create_file_dialog(
                webview.FileDialog.OPEN, allow_multiple=True,
                file_types=(L("Screenshots and logs", "Capturas y registros")
                            + " (*.png;*.jpg;*.jpeg;*.gif;*.webp;*.log;*.txt)",),
            ) or []
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "report.log", f"file dialog failed: {exc!r}")
            return {**self._report_file_list(), "ok": False, "code": "dialog"}
        files = list(getattr(self, "_report_files", []))
        skipped = ""
        for raw in picked:
            p = Path(raw)
            ext = p.suffix.lower()
            if ext not in self._REPORT_IMAGE_EXT and ext not in self._REPORT_TEXT_EXT:
                skipped = skipped or "type"
                continue
            try:
                size = p.stat().st_size
            except OSError:
                continue
            if any(str(q).lower() == str(p).lower() for q, _ in files):
                continue
            if len(files) >= self._REPORT_MAX_FILES:
                skipped = "count"
                break
            if sum(s for _, s in files) + size > self._REPORT_MAX_BYTES:
                skipped = skipped or "size"
                continue
            files.append((p, size))
        self._report_files = files
        out = self._report_file_list()
        if skipped:
            out["skipped"] = skipped
        return out

    def report_remove_file(self, index: int) -> dict:
        files = list(getattr(self, "_report_files", []))
        try:
            files.pop(int(index))
        except (IndexError, ValueError, TypeError):
            pass
        self._report_files = files
        return self._report_file_list()

    def report_clear_files(self) -> dict:
        self._report_files = []
        return self._report_file_list()

    def _report_multipart(self, fields: dict[str, str], files: list[tuple[str, str, str, bytes]]) -> tuple[bytes, str]:
        boundary = "----openhop" + hashlib.sha256(os.urandom(16)).hexdigest()[:24]
        out = bytearray()
        for name, value in fields.items():
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n"
                    "Content-Type: text/plain; charset=utf-8\r\n\r\n").encode()
            out += value.encode("utf-8") + b"\r\n"
        for field, filename, ctype, data in files:
            safe = re.sub(r"[^A-Za-z0-9._ -]", "_", filename)[:80] or "file"
            out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{field}\"; filename=\"{safe}\"\r\n"
                    f"Content-Type: {ctype}\r\n\r\n").encode()
            out += data + b"\r\n"
        out += f"--{boundary}--\r\n".encode()
        return bytes(out), f"multipart/form-data; boundary={boundary}"

    def send_report(self, description: str = "", contact: str = "", honeypot: str = "") -> dict:
        if not self._report_send_lock.acquire(blocking=False):
            return {"ok": False, "code": "busy"}
        try:
            import report_bundle
            try:
                bundle = self._report_bundle()
            except Exception as exc:
                append_log(DEST_DIR / "logs" / "report.log", f"collect failed: {exc!r}")
                return {"ok": False, "code": "collect"}
            attachments = []
            for i, (p, _size) in enumerate(getattr(self, "_report_files", [])):
                ext = p.suffix.lower()
                try:
                    data = p.read_bytes()
                except OSError:
                    continue
                if ext in self._REPORT_TEXT_EXT:  # extra logs get the same clean-up as the report itself
                    text = report_bundle.redact(data[:4_000_000].decode("utf-8", errors="replace"))
                    data = text.encode("utf-8")[:2_000_000]
                    attachments.append((f"file{i + 1}", p.stem + ".txt", "text/plain; charset=utf-8", data))
                else:
                    attachments.append((f"file{i + 1}", p.name, self._REPORT_IMAGE_EXT.get(ext, "application/octet-stream"), data))
            meta = {
                "version": VERSION,
                "description": str(description or "").strip()[:2000],
                "contact": str(contact or "").strip()[:200],
                "summary": bundle["summary"],
                "attachments": [a[2] for a in attachments],
            }
            body, ctype = self._report_multipart(
                {"meta": json.dumps(meta, ensure_ascii=False), "website": str(honeypot or "")[:200]},
                [("report", "report.txt", "text/plain; charset=utf-8", bundle["text"].encode("utf-8"))] + attachments,
            )
            url = os.environ.get("OPENHOP_REPORT_URL", "").strip() or self._REPORT_URL
            req = urllib.request.Request(url, data=body, method="POST", headers={
                "Content-Type": ctype, "User-Agent": f"openhop/{VERSION}", "X-Openhop-Version": VERSION,
                "Accept": "application/json",
            })
            # Direct, never through our own proxy on :41987 (the shell may export HTTP(S)_PROXY to it).
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            try:
                with opener.open(req, timeout=30) as res:
                    payload = self._report_json(res.read(8192))
                self._report_files = []
                self._report_cache = None
                return {"ok": True, "code": "sent", "dry_run": bool(payload.get("dryRun"))}
            except urllib.error.HTTPError as err:
                payload = self._report_json(err.read(8192) if err.fp else b"")
                server_msg = str(payload.get("message") or "")[:300]
                append_log(DEST_DIR / "logs" / "report.log", f"send failed: HTTP {err.code} {server_msg}")
                if err.code == 429:
                    return {"ok": False, "code": "rate_limited", "message": server_msg}
                if err.code == 413:
                    return {"ok": False, "code": "too_large", "message": server_msg}
                if 400 <= err.code < 500 and server_msg:
                    return {"ok": False, "code": "rejected", "message": server_msg}
                return {"ok": False, "code": "server"}
            except (urllib.error.URLError, socket.timeout, TimeoutError, ConnectionError, OSError) as err:
                append_log(DEST_DIR / "logs" / "report.log", f"send failed: {err!r}")
                return {"ok": False, "code": "offline"}
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "report.log", f"send crashed: {exc!r}")
            return {"ok": False, "code": "unknown"}
        finally:
            self._report_send_lock.release()

    @staticmethod
    def _report_json(raw: bytes) -> dict:
        try:
            data = json.loads(raw.decode("utf-8", errors="replace") or "{}")
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}


def _attach_parent_console() -> None:
    try:
        if ctypes.windll.kernel32.AttachConsole(-1):
            sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
            sys.stdout = open("CONOUT$", "w", encoding="utf-8", errors="replace")
            sys.stderr = open("CONOUT$", "w", encoding="utf-8", errors="replace")
    except Exception:
        pass


def _run_cli_mode(argv: list[str]) -> int | None:
    if len(argv) >= 2 and argv[0] == "--worker":
        role = argv[1].lower()
        if role == "keeper":
            import warp_keeper
            return warp_keeper.main()
        if role == "guard":
            import zen_guard
            return zen_guard.main()
    if len(argv) >= 2 and argv[0] == "--firewall":
        ok, msg = install_killswitch() if argv[1] == "install" else remove_killswitch()
        print(msg)
        return 0 if ok else 1
    if argv and argv[0] == "--lane":
        _attach_parent_console()
        import zen_lane
        sys.argv = ["zen_lane.py", *argv[1:]]
        return zen_lane.main()
    if argv and argv[0] == "--hop":
        _attach_parent_console()
        import zen_hop
        return zen_hop.main()
    if argv and argv[0] == "--doctor":
        _attach_parent_console()
        import doctor
        return doctor.main()
    return None


def _is_webview2_available() -> bool:
    """Check registry for installed Microsoft Edge WebView2 Runtime or Edge Canary/Beta/Dev."""
    try:
        import winreg
        keys = (
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}"),
            (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}"),
            (winreg.HKEY_CURRENT_USER, r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{56EB18F8-B008-4CBD-B6D2-8C97FE7E9062}"),
        )
        for hive, subkey in keys:
            try:
                with winreg.OpenKey(hive, subkey) as k:
                    val, _ = winreg.QueryValueEx(k, "pv")
                    if val and str(val).strip() not in ("0.0.0.0", "0"):
                        return True
            except Exception:
                continue
    except Exception:
        pass
    return False


def _prompt_webview2_install() -> None:
    """Prompt user to download official Microsoft Edge WebView2 runtime if missing."""
    import webbrowser
    MB_YESNO = 0x00000004
    MB_ICONEXCLAMATION = 0x00000030
    IDYES = 6
    msg = L(
        "openhop needs the Microsoft Edge WebView2 Runtime to show its window.\n\n"
        "It was not detected on this Windows installation (this commonly occurs on "
        "older Windows 10 versions or stripped/lite/gaming Windows editions).\n\n"
        "Would you like to open the official Microsoft WebView2 installer download page now?",
        "openhop necesita Microsoft Edge WebView2 Runtime para mostrar su ventana.\n\n"
        "No se ha encontrado en este Windows (pasa a menudo en versiones antiguas de "
        "Windows 10 o en ediciones recortadas/lite/gaming).\n\n"
        "¿Quieres abrir ahora la página oficial de descarga de WebView2 de Microsoft?",
    )
    ans = ctypes.windll.user32.MessageBoxW(
        None,
        msg,
        L("openhop — WebView2 needed", "openhop — hace falta WebView2"),
        MB_YESNO | MB_ICONEXCLAMATION,
    )
    if ans == IDYES:
        webbrowser.open("https://go.microsoft.com/fwlink/p/?LinkId=2124703")


def _load_ui_html() -> str:
    """Load index.html from bundle or adjacent filesystem paths."""
    candidates = (
        _BUNDLE / "src" / "ui" / "index.html",
        _BUNDLE / "windows" / "ui" / "index.html",
        _BUNDLE / "ui" / "index.html",
        Path(__file__).resolve().parent / "ui" / "index.html",
        Path(__file__).resolve().parent / "src" / "ui" / "index.html",
        Path(__file__).resolve().parent / "windows" / "ui" / "index.html",
        DEST_DIR / "ui" / "index.html",
    )
    for p in candidates:
        if p.is_file():
            try:
                txt = p.read_text(encoding="utf-8")
                if txt.strip():
                    return txt
            except Exception:
                pass
    raise FileNotFoundError("Could not find index.html in application bundle or local folders.")


def _log_startup_error(exc: Exception) -> None:
    """Log startup exceptions to disk and present an informative dialog."""
    import traceback
    err_text = traceback.format_exc()
    try:
        log_dir = DEST_DIR / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "openhop_error.log").write_text(err_text, encoding="utf-8")
    except Exception:
        pass
    MB_OK = 0x00000000
    MB_ICONERROR = 0x00000010
    netfx = _netfx_problem(exc)
    if _blocked_by_antivirus(exc) or netfx == "missing":
        _startup_log("an antivirus blocked openhop from reading its own files")
        title, text = _antivirus_block_message()
    elif netfx == "netfx":
        _startup_log(".NET Framework is missing or broken (mscoree.dll couldn't be loaded)")
        title, text = _netfx_message()
    else:
        title = L("openhop — couldn't start", "openhop — no se pudo iniciar")
        text = L(f"openhop couldn't open its window:\n\n{exc}\n\n"
                 f"Details have been written to:\n{DEST_DIR}\\logs\\openhop_error.log",
                 f"openhop no pudo abrir su ventana:\n\n{exc}\n\n"
                 f"Los detalles están en:\n{DEST_DIR}\\logs\\openhop_error.log")
    ctypes.windll.user32.MessageBoxW(None, text, title, MB_OK | MB_ICONERROR)


def _blocked_by_antivirus(exc: BaseException) -> bool:
    """True when the real cause is that openhop wasn't allowed to read its own exe or unpacked files.
    That's an antivirus holding or quarantining the file (seen with Bitdefender); the error on top of
    it (usually ".NET runtime") is only a side effect."""
    own = {os.path.normcase(os.path.abspath(sys.executable)), os.path.normcase(str(_BUNDLE))}
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, PermissionError):
            name = os.path.normcase(os.path.abspath(str(getattr(cur, "filename", "") or "")))
            if any(name == p or name.startswith(p + os.sep) for p in own):
                return True
        cur = cur.__cause__ or cur.__context__
    return False


_CLR_DLL_RE = re.compile(r"cannot load library '([^']*ClrLoader\.dll)': error 0x7e", re.I)


def _netfx_problem(exc: BaseException) -> str:
    """Why the window's .NET part (pythonnet's ClrLoader.dll) couldn't load, if that's what happened.

    'missing': the DLL isn't in openhop's unpacked files any more (an antivirus removed it).
    'netfx':   the DLL is there but Windows can't load it (error 0x7e). Its only dependency is mscoree.dll,
               so .NET Framework is missing or broken on this PC (seen in a user's openhop_error.log).
    '':        something else.
    """
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        m = _CLR_DLL_RE.search(str(cur))
        if m:
            return "netfx" if os.path.isfile(m.group(1)) else "missing"
        cur = cur.__cause__ or cur.__context__
    return ""


def _netfx_message() -> tuple[str, str]:
    """Plain-language dialog for a missing/broken .NET Framework, in Spanish on Spanish Windows."""
    url = "https://dotnet.microsoft.com/download/dotnet-framework/net48"
    try:
        spanish = ui_lang() == "es"
    except Exception:
        spanish = False
    if spanish:
        return ("openhop — falta una pieza de Windows", (
            "La ventana de openhop necesita .NET Framework, una pieza de Windows que en este PC falta o está rota.\n"
            "La protección en segundo plano sigue funcionando; solo la ventana no se puede abrir.\n\n"
            "Cómo arreglarlo:\n"
            "1. Instala .NET Framework 4.8 desde la página de Microsoft:\n"
            f"    {url}\n"
            "    (elige \"Runtime\").\n"
            "2. Reinicia el PC y vuelve a abrir openhop.\n\n"
            "Si dice que ya está instalado: Panel de control → Programas → Activar o desactivar las\n"
            "características de Windows → marca \".NET Framework 4.8 Advanced Services\" → Aceptar → reinicia."))
    return ("openhop — a Windows component is missing", (
        "openhop's window needs .NET Framework, a part of Windows that is missing or broken on this PC.\n"
        "Background protection keeps working; only the window can't open.\n\n"
        "How to fix it:\n"
        "1. Install .NET Framework 4.8 from Microsoft's page:\n"
        f"    {url}\n"
        "    (choose \"Runtime\").\n"
        "2. Restart the PC and open openhop again.\n\n"
        "If it says it's already installed: Control Panel → Programs → Turn Windows features on or off →\n"
        "tick \".NET Framework 4.8 Advanced Services\" → OK → restart."))


def _antivirus_block_message() -> tuple[str, str]:
    """Plain-language dialog for the antivirus case, in Spanish on Spanish Windows."""
    exe = sys.executable
    try:
        spanish = ui_lang() == "es"
    except Exception:
        spanish = False
    if spanish:
        return ("openhop — tu antivirus lo está bloqueando", (
            "Tu antivirus no deja que openhop lea sus propios archivos, así que se cierra.\n"
            "openhop no es un virus: es de código abierto (openhop.cfd/source).\n\n"
            "Cómo arreglarlo:\n"
            "1. Mueve openhop.exe a una carpeta fija (por ejemplo C:\\openhop) y borra las copias de Descargas.\n"
            "2. En tu antivirus, añade openhop.exe como excepción.\n"
            "    Bitdefender: Protección → Antivirus → Ajustes → Administrar excepciones, y también\n"
            "    Protección → Defensa avanzada contra amenazas → Ajustes → Administrar excepciones.\n"
            "    Antivirus de Windows: Seguridad de Windows → Protección contra virus y amenazas →\n"
            "    Administrar la configuración → Exclusiones.\n"
            "3. Si está en cuarentena, restáuralo. Luego ábrelo una sola vez y espera hasta un minuto.\n\n"
            f"Este archivo: {exe}"))
    return ("openhop — blocked by your antivirus", (
        "Your antivirus isn't letting openhop read its own files, so it closes.\n"
        "openhop isn't a virus: it's open source (openhop.cfd/source).\n\n"
        "How to fix it:\n"
        "1. Move openhop.exe to a fixed folder (for example C:\\openhop) and delete the copies in Downloads.\n"
        "2. In your antivirus, add openhop.exe as an exception.\n"
        "    Bitdefender: Protection → Antivirus → Settings → Manage exceptions, and also\n"
        "    Protection → Advanced Threat Defense → Settings → Manage exceptions.\n"
        "    Windows antivirus: Windows Security → Virus & threat protection →\n"
        "    Manage settings → Exclusions.\n"
        "3. If it's in quarantine, restore it. Then open it once and wait up to a minute.\n\n"
        f"This file: {exe}"))


MUTEX_UI = "openhop-UI"


def _openhop_windows() -> list[tuple[int, str]]:
    """Top-level openhop windows of any version (also hidden ones in the tray): [(hwnd, title)]."""
    found: list[tuple[int, str]] = []
    try:
        user32 = ctypes.windll.user32
        proc_t = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

        def _cb(hwnd, _lparam):
            try:
                n = user32.GetWindowTextLengthW(ctypes.c_void_p(hwnd))
                if n:
                    buf = ctypes.create_unicode_buffer(n + 1)
                    user32.GetWindowTextW(ctypes.c_void_p(hwnd), buf, n + 1)
                    title = buf.value
                    if title.startswith("openhop ") and title.endswith("openhop.cfd"):
                        found.append((int(hwnd or 0), title))
            except Exception:
                pass
            return True

        user32.EnumWindows(proc_t(_cb), 0)
    except Exception:
        pass
    return found


def _wait_for_starting_copy(timeout: float = 60.0, show: bool = True) -> bool:
    """Another openhop window process is running but has no window yet (the first start can take
    a while: Windows checks the exe and unpacks it). Wait for it instead of killing it, which is what
    happened when people double-clicked again because nothing showed up.

    Returns True when that copy's window appeared (it was brought to the front; this copy should exit).
    """
    _startup_log("another openhop is already starting: waiting for its window instead of starting twice")
    deadline = time.time() + timeout
    while time.time() < deadline:
        existing = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if existing:
            if show:  # not when this copy was started hidden (--tray, e.g. at Windows start)
                ctypes.windll.user32.ShowWindow(ctypes.c_void_p(existing), 5)
                ctypes.windll.user32.ShowWindow(ctypes.c_void_p(existing), 9)
                ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(existing))
            _startup_log("its window is open" + (": brought it to the front" if show else ""))
            return True
        if _openhop_windows():
            return False  # a different version is open: replace it as usual
        if not is_mutex_held(MUTEX_UI):
            return False  # it closed
        time.sleep(0.5)
    _startup_log("the other copy never showed a window: starting a fresh one")
    return False


def _kill_previous_instances() -> int:
    """Close older openhop / Opencode Unlimited *windows*.

    Background workers (keeper/guard) are deliberately left alone: the keeper hosts the proxy
    OpenCode is wired to, and killing it caused ECONNREFUSED outages. Outdated workers are
    restarted by AvatarControlApi._startup_sync instead.
    """
    current_pid = os.getpid()
    count = 0
    try:
        import psutil
        try:
            # PyInstaller onefile runs as bootloader parent + child with the same name.
            protected = {current_pid, *(p.pid for p in psutil.Process(current_pid).parents())}
        except Exception:
            protected = {current_pid}
        cli_flags = ("--worker", "--lane", "--hop", "--doctor", "--firewall")
        for p in psutil.process_iter(["pid", "name", "cmdline"]):
            try:
                pid = p.info["pid"]
                if pid in protected:
                    continue
                name = (p.info["name"] or "").lower()
                cmd = " ".join(p.info.get("cmdline") or []).lower()
                if any(flag in cmd for flag in cli_flags):
                    continue
                if "opencodeunlimited" in name or name.startswith("openhop"):
                    p.kill()
                    count += 1
                elif "python" in name and ("openhop_app.py" in cmd or "avatar_ui.py" in cmd):
                    p.kill()
                    count += 1
            except Exception:
                pass
        if count > 0:
            time.sleep(0.4)
    except Exception:
        pass
    return count


def main() -> int:
    cli_rc = _run_cli_mode(sys.argv[1:])
    if cli_rc is not None:
        return cli_rc

    start_in_tray = "--tray" in sys.argv[1:]

    # openhop already running (maybe hidden in the tray since yesterday)? Then don't swap the exe under it:
    # the new copy would just find the old window and bring it back, leaving the old version running.
    # Instead the running window comes to the front and shows the "update ready" popup itself.
    already_running = bool(ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)) and "--updated" not in sys.argv[1:]

    # Automatic updates: finish a downloaded update before anything else (the new exe takes over).
    if not already_running:
        try:
            import updater
            from openhop_tray import load_settings as _load_settings
            updater.cleanup_old()
            if _load_settings().get("auto_update", True) and updater.install_on_start(sys.argv[1:]):
                return 0
        except Exception as exc:
            append_log(DEST_DIR / "logs" / "update.log", f"startup update step failed: {exc}")

    # Same version already open (maybe hidden in the tray): bring it to the front.
    existing = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
    if existing and "--updated" in sys.argv[1:]:
        # The update started us: the old window is still closing. Wait for it instead of giving up
        # (otherwise both would exit and the user would be left with no window at all).
        deadline = time.time() + 30
        while existing and time.time() < deadline:
            time.sleep(0.25)
            existing = ctypes.windll.user32.FindWindowW(None, WINDOW_TITLE)
        if existing:
            return 0  # openhop was reopened by hand in the meantime; leave that window alone
    if existing:
        if not start_in_tray:
            try:  # tells the running openhop it was opened again (it then checks for a waiting update)
                (DEST_DIR / "run").mkdir(parents=True, exist_ok=True)
                (DEST_DIR / "run" / "open_request").write_text(str(time.time()), encoding="utf-8")
            except Exception:
                pass
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(existing), 5)  # SW_SHOW (un-hide)
            ctypes.windll.user32.ShowWindow(ctypes.c_void_p(existing), 9)  # SW_RESTORE
            ctypes.windll.user32.SetForegroundWindow(ctypes.c_void_p(existing))
        return 0

    # Only one openhop window process. A copy that is still starting has no window yet, so
    # FindWindow above can't see it: the named mutex can.
    ui_lock = acquire_mutex(MUTEX_UI)
    if ui_lock is None and "--updated" not in sys.argv[1:]:
        if _wait_for_starting_copy(show=not start_in_tray):
            return 0

    # Close windows of older versions (background workers keep running; see _startup_sync).
    killed = _kill_previous_instances()
    if killed:
        _startup_log(f"closed {killed} older openhop window(s)")
    if ui_lock is None:
        ui_lock = acquire_mutex(MUTEX_UI)

    # Own taskbar identity: without this, running from source groups the window under
    # pythonw.exe and Windows shows the Python icon instead of the openhop icon.
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("bernatlol.openhop.controller")
    except Exception:
        pass

    if not _is_webview2_available():
        _startup_log("Microsoft Edge WebView2 Runtime not found: asked the user to install it")
        _prompt_webview2_install()
        return 1

    try:
        html_content = _load_ui_html()
        html_content = html_content.replace('/*OPENHOP_LANG*/"en"', json.dumps(ui_lang()), 1)
    except Exception as exc:
        _log_startup_error(exc)
        return 1

    ico_path = Path()
    for candidate_ico in (
        _BUNDLE / "src" / "assets" / "openhop.ico",
        _BUNDLE / "windows" / "assets" / "openhop.ico",
        _BUNDLE / "assets" / "openhop.ico",
        Path(__file__).resolve().parent / "assets" / "openhop.ico",
    ):
        if candidate_ico.is_file():
            ico_path = candidate_ico
            break

    cache_dir = DEST_DIR / "webview_cache"
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass

    _startup_log("setting up")
    api = AvatarControlApi()
    _startup_log("creating the window")
    window = webview.create_window(
        WINDOW_TITLE,
        html=html_content,
        js_api=api,
        width=1180,
        height=790,
        min_size=(980, 680),
        background_color="#0a0d13",
        text_select=True,
        frameless=True,
        easy_drag=False,
        shadow=True,
        hidden=start_in_tray,
    )
    window.events.shown += _enable_dark_titlebar
    window.events.shown += lambda: _startup_log("window shown")
    api._window = window
    window.events.closing += api._on_closing

    # Tray icon + notifications (if it can't start, X simply closes the window as before)
    try:
        from openhop_tray import Tray, apply_first_run_defaults, register_app_identity
        register_app_identity(ico_path)
        apply_first_run_defaults()
        tray = Tray(api, ico_path, on_open=api.show_window, on_quit=api.quit_app)
        if tray.start():
            api._tray = tray
    except Exception as exc:  # the app still works without a tray; just note why
        append_log(DEST_DIR / "logs" / "tray.log", f"tray failed to start: {exc}")
    api._start_update_checks()
    api._start_notice_checks()
    api._start_rank_sync()
    api._start_post_update_checks()
    api._watch_open_requests()
    if start_in_tray and not api._tray:
        window.events.loaded += lambda: window.show()  # no tray -> never leave an invisible app behind
    start_kwargs: dict = {
        "gui": "edgechromium",
        "debug": False,
        "storage_path": str(cache_dir),
        "private_mode": False,
    }
    if ico_path.is_file():
        start_kwargs["icon"] = str(ico_path)

    try:
        webview.start(**start_kwargs)
    except Exception as exc:
        _startup_log(f"the window failed: {exc!r}")
        _log_startup_error(exc)
        return 1
    _startup_log("closed")
    return 0


if __name__ == "__main__":
    try:
        _rc = main()
    except SystemExit:
        raise
    except Exception as _exc:  # anything unexpected: leave a trace and say so, instead of vanishing
        import traceback as _tb
        _startup_log("crashed while starting:\n" + _tb.format_exc())
        try:
            _log_startup_error(_exc)
        except Exception:
            pass
        _rc = 1
    raise SystemExit(_rc)

"""WireGuard files (.conf) through wireproxy: Proton VPN Free and any VPN that gives WireGuard files.

Why: VPN apps like Norton or the Proton VPN app can't be told to change server. A WireGuard file is one
server, so with a few of them openhop can change server by itself, and only OpenCode goes through it (the
rest of the PC keeps its normal connection).

How: wireproxy (https://github.com/windtf/wireproxy, ISC licence) runs the WireGuard tunnel in user space
and offers a local SOCKS5 proxy on 127.0.0.1:40110. No driver, no admin rights, no change to the PC.
openhop downloads one pinned release and checks its SHA-256 before using it.

Files:  %LOCALAPPDATA%\\openhop\\wireguard\\configs\\*.conf   the user's WireGuard files
        %LOCALAPPDATA%\\openhop\\wireguard\\bin\\wireproxy.exe
        %LOCALAPPDATA%\\openhop\\wireguard\\state.json       which file is in use + each file's last exit IP
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import shutil
import socket
import subprocess
import tarfile
import time
import urllib.request
from contextlib import contextmanager
from pathlib import Path

from ouc_common import CREATE_NO_WINDOW, DEST_DIR, IS_WINDOWS, append_log, ensure_dirs, run_hidden

VPN_ID = "wireguard"
WG_DIR = DEST_DIR / "wireguard"
CONF_DIR = WG_DIR / "configs"
BIN_DIR = WG_DIR / "bin"
RUN_CONF = WG_DIR / "wireproxy.conf"
STATE_FILE = WG_DIR / "state.json"
PID_FILE = WG_DIR / "wireproxy.pid"
LOCK_FILE = WG_DIR / "lock"
LOG_FILE = DEST_DIR / "logs" / "wireproxy.log"
SOCKS_PORT = 40110

WIREPROXY_VERSION = "v1.1.3"
_RELEASE = f"https://github.com/windtf/wireproxy/releases/download/{WIREPROXY_VERSION}/"
# Pinned: the release's own checksums.txt, checked on 2026-11-18. A different file is never run.
WIREPROXY_ASSETS = {
    "windows": ("wireproxy_windows_amd64.tar.gz",
                "bce041ea9fe0f8a3351301dcbe29cdf6a523bb25cf9c62f17ebb5699a8051d0f"),
    "linux": ("wireproxy_linux_amd64.tar.gz",
              "e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c"),
}
EXE_NAME = "wireproxy.exe" if IS_WINDOWS else "wireproxy"
DETACHED_PROCESS = 0x00000008
MAX_CONF_BYTES = 16 * 1024


def _log(msg: str) -> None:
    try:
        append_log(LOG_FILE, msg)
    except Exception:
        pass


# ---------------------------------------------------------------------------------------------------------------
# wireproxy itself

def find_exe() -> Path | None:
    p = BIN_DIR / EXE_NAME
    return p if p.is_file() else None


def download() -> tuple[bool, str]:
    """Download the pinned wireproxy release and check its SHA-256. (ok, plain-language message)."""
    if find_exe():
        return True, "wireproxy is ready"
    name, want = WIREPROXY_ASSETS["windows" if IS_WINDOWS else "linux"]
    ensure_dirs()
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    try:
        req = urllib.request.Request(_RELEASE + name, headers={"User-Agent": "openhop"})
        with urllib.request.urlopen(req, timeout=90) as resp:
            data = resp.read(64 * 1024 * 1024)
    except Exception as exc:
        return False, f"couldn't download wireproxy: {exc}"
    got = hashlib.sha256(data).hexdigest()
    if got != want:
        _log(f"wireproxy download rejected: sha256 {got} != {want}")
        return False, "the downloaded wireproxy didn't match the expected file, so it wasn't used"
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            member = next((m for m in tar.getmembers() if m.isfile() and Path(m.name).name == EXE_NAME), None)
            if not member:
                return False, "the wireproxy download has no program inside"
            src = tar.extractfile(member)
            tmp = BIN_DIR / (EXE_NAME + ".part")
            with open(tmp, "wb") as out:
                shutil.copyfileobj(src, out)
            if not IS_WINDOWS:
                tmp.chmod(0o755)
            os.replace(tmp, BIN_DIR / EXE_NAME)
    except Exception as exc:
        return False, f"couldn't unpack wireproxy: {exc}"
    _log(f"wireproxy {WIREPROXY_VERSION} downloaded (sha256 ok)")
    return True, "wireproxy downloaded"


# ---------------------------------------------------------------------------------------------------------------
# The user's WireGuard files

def parse_conf(text: str) -> dict:
    """Check a WireGuard file and return what we show about it. Raises ValueError in plain words."""
    section, iface, peer = "", {}, {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        m = re.match(r"^\[(\w+)\]$", line)
        if m:
            section = m.group(1).lower()
            continue
        if "=" not in line:
            continue
        k, v = (s.strip() for s in line.split("=", 1))
        if section == "interface":
            iface[k.lower()] = v
        elif section == "peer" and not peer.get("_done"):
            peer[k.lower()] = v
    if not iface.get("privatekey") or not iface.get("address"):
        raise ValueError("it isn't a WireGuard file (no [Interface] with PrivateKey and Address)")
    if not peer.get("publickey") or not peer.get("endpoint"):
        raise ValueError("it isn't a WireGuard file (no [Peer] with PublicKey and Endpoint)")
    return {"endpoint": peer["endpoint"], "address": iface["address"], "dns": iface.get("dns", "")}


def _label(name: str) -> str:
    """'wg-NL-FREE-12.conf' -> 'NL-FREE-12' (what the user sees)."""
    stem = Path(name).stem
    return re.sub(r"^(wg|proton|protonvpn)[-_ ]+", "", stem, flags=re.I) or stem


def list_confs() -> list[dict]:
    st = _state()
    exits = st.get("exits", {})
    out = []
    for p in sorted(CONF_DIR.glob("*.conf")) if CONF_DIR.is_dir() else []:
        try:
            info = parse_conf(p.read_text(encoding="utf-8", errors="replace"))
        except Exception:
            continue
        e = exits.get(p.name, {})
        out.append({"name": p.name, "label": _label(p.name), "endpoint": info["endpoint"],
                    "exit_ip": e.get("ip", ""), "exit_at": e.get("at", 0)})
    return out


def import_confs(paths: list[str]) -> tuple[list[str], list[str]]:
    """Copy the user's .conf files in. Returns (added names, problems in plain words)."""
    ensure_dirs()
    CONF_DIR.mkdir(parents=True, exist_ok=True)
    added, problems = [], []
    for raw in paths or []:
        p = Path(raw)
        try:
            if p.stat().st_size > MAX_CONF_BYTES:
                raise ValueError("it's too big for a WireGuard file")
            text = p.read_text(encoding="utf-8", errors="replace")
            parse_conf(text)
        except Exception as exc:
            problems.append(f"{p.name}: {exc}")
            continue
        safe = re.sub(r"[^A-Za-z0-9._#-]+", "-", p.stem).strip("-.")[:60] or f"wg-{int(time.time())}"
        dest = CONF_DIR / f"{safe}.conf"
        dest.write_text(text, encoding="utf-8")
        added.append(dest.name)
    if added and not _state().get("current"):
        set_current(added[0])
    return added, problems


def remove_conf(name: str) -> bool:
    p = CONF_DIR / Path(name).name
    if not p.is_file():
        return False
    p.unlink()
    st = _state()
    st.get("exits", {}).pop(p.name, None)
    if st.get("current") == p.name:
        st["current"] = ""
    _save_state(st)
    return True


# ---------------------------------------------------------------------------------------------------------------
# Which file is in use

def _state() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_state(st: dict) -> None:
    WG_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
    os.replace(tmp, STATE_FILE)


def current_conf() -> str:
    names = [c["name"] for c in list_confs()]
    cur = _state().get("current", "")
    if cur in names:
        return cur
    return names[0] if names else ""


def set_current(name: str) -> None:
    st = _state()
    st["current"] = Path(name).name
    _save_state(st)


def record_exit(name: str, ip: str) -> None:
    if not name or not ip:
        return
    st = _state()
    st.setdefault("exits", {})[name] = {"ip": ip, "at": int(time.time())}
    _save_state(st)


def next_conf(tried: list[str], is_burnt=lambda ip: False) -> str:
    """The next file to try after the current one: not tried in this round and not known to be used up."""
    confs = list_confs()
    names = [c["name"] for c in confs]
    if not names:
        return ""
    cur = current_conf()
    start = names.index(cur) + 1 if cur in names else 0
    order = names[start:] + names[:start]
    by_name = {c["name"]: c for c in confs}
    for n in order:
        if n in tried:
            continue
        ip = by_name[n].get("exit_ip", "")
        if ip and is_burnt(ip):
            continue   # its server already hit the limit today: skip it without even trying
        return n
    return ""


# ---------------------------------------------------------------------------------------------------------------
# Running wireproxy

@contextmanager
def _lock(wait: float = 15.0):
    """One process at a time starts/stops wireproxy (the keeper and the guard both may)."""
    WG_DIR.mkdir(parents=True, exist_ok=True)
    fh = open(LOCK_FILE, "a+")
    got = False
    deadline = time.time() + wait
    try:
        while True:
            try:
                if IS_WINDOWS:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                got = True
                break
            except OSError:
                if time.time() > deadline:
                    break
                time.sleep(0.2)
        yield got
    finally:
        if got:
            try:
                if IS_WINDOWS:
                    import msvcrt
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
        fh.close()


def _pid_info() -> dict:
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _proc_alive(pid: int) -> bool:
    try:
        import psutil
        p = psutil.Process(pid)
        return p.is_running() and "wireproxy" in p.name().lower()
    except Exception:
        return False


def socks_alive(timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", SOCKS_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b"\x05\x01\x00")
            r = s.recv(2)
            return len(r) == 2 and r[0] == 5 and r[1] == 0
    except Exception:
        return False


def running_conf() -> str:
    """The file wireproxy is running with right now ('' if it isn't running)."""
    info = _pid_info()
    pid = int(info.get("pid", 0) or 0)
    return str(info.get("conf", "")) if pid and _proc_alive(pid) else ""


def _stop_locked() -> None:
    info = _pid_info()
    pid = int(info.get("pid", 0) or 0)
    if pid and _proc_alive(pid):
        try:
            import psutil
            p = psutil.Process(pid)
            p.kill()
            p.wait(5)
        except Exception:
            pass
    PID_FILE.unlink(missing_ok=True)


def stop() -> None:
    with _lock() as got:
        if got:
            _stop_locked()


def ensure_running(download_if_missing: bool = False) -> tuple[bool, str]:
    """Keep wireproxy running with the current file. Restarts it when the file changed. (ok, message)."""
    want = current_conf()
    if not want:
        return False, "no WireGuard files yet"
    if running_conf() == want and socks_alive():
        return True, "running"
    exe = find_exe()
    if not exe and download_if_missing:
        ok, msg = download()
        if not ok:
            return False, msg
        exe = find_exe()
    if not exe:
        return False, "wireproxy isn't downloaded yet"
    with _lock() as got:
        if not got:
            return False, "busy"
        if running_conf() == want and socks_alive():
            return True, "running"
        _stop_locked()
        if socks_alive(0.5):
            return False, f"port {SOCKS_PORT} is used by another program"
        conf_path = CONF_DIR / want
        RUN_CONF.write_text(f"WGConfig = {conf_path}\n\n[Socks5]\nBindAddress = 127.0.0.1:{SOCKS_PORT}\n",
                            encoding="utf-8")
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        try:
            log = open(LOG_FILE, "ab")
            flags = (CREATE_NO_WINDOW | DETACHED_PROCESS) if IS_WINDOWS else 0
            proc = subprocess.Popen([str(exe), "-c", str(RUN_CONF)], stdout=log, stderr=log,
                                    stdin=subprocess.DEVNULL, creationflags=flags, close_fds=True,
                                    cwd=str(WG_DIR))
            log.close()
        except Exception as exc:
            return False, f"couldn't start wireproxy: {exc}"
        PID_FILE.write_text(json.dumps({"pid": proc.pid, "conf": want, "since": int(time.time())}),
                            encoding="utf-8")
        for _ in range(40):
            if socks_alive(0.5):
                _log(f"wireproxy up with {want} (pid {proc.pid})")
                return True, "started"
            if proc.poll() is not None:
                return False, "wireproxy stopped right away: the WireGuard file may be wrong (see wireproxy.log)"
            time.sleep(0.25)
        return False, "wireproxy started but its local port didn't answer"


def exit_ip(timeout: float = 8.0) -> str:
    """Public IP through the WireGuard tunnel ('' if it doesn't answer)."""
    if not socks_alive():
        return ""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return ""
    for ep in ("https://cloudflare.com/cdn-cgi/trace", "https://1.1.1.1/cdn-cgi/trace"):
        try:
            res = run_hidden([curl, "--socks5-hostname", f"127.0.0.1:{SOCKS_PORT}", "-s", "-m", str(int(timeout)),
                              ep], timeout=timeout + 2)
        except Exception:
            continue
        for line in (res.stdout or "").splitlines():
            if line.startswith("ip="):
                return line[3:].strip()
    return ""


def open_tunnel(host: str, port: int, timeout: float = 12.0) -> socket.socket:
    """TCP connection to host:port through the WireGuard tunnel (hostnames resolve inside the tunnel)."""
    from tor_manager import open_socks5_tunnel
    return open_socks5_tunnel(host, port, "127.0.0.1", SOCKS_PORT, timeout=timeout)


if __name__ == "__main__":
    import sys
    if "--download" in sys.argv:
        print(download())
    if "--start" in sys.argv:
        print(ensure_running(download_if_missing=True))
        print("exit IP:", exit_ip())
    if "--stop" in sys.argv:
        stop()
    for c in list_confs():
        print(c)
    print("current:", current_conf(), "running:", running_conf() or "-", "socks:", socks_alive())

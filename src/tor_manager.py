"""Tor portable manager and SOCKS5 client for openhop."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
from pathlib import Path

from ouc_common import (
    CREATE_NO_WINDOW,
    DEST_DIR,
    L,
    append_log,
    ensure_dirs,
    run_hidden,
)

TOR_DIR = DEST_DIR / "tor"
TOR_DATA_DIR = TOR_DIR / "data"
TOR_PID_FILE = TOR_DIR / "tor.pid"
TOR_LOG_FILE = DEST_DIR / "logs" / "tor.log"
TOR_DEFAULT_SOCKS_PORT = 9050
TOR_BROWSER_SOCKS_PORT = 9150

DETACHED_PROCESS = 0x00000008


def is_port_open(port: int, host: str = "127.0.0.1", timeout: float = 0.8) -> bool:
    """Check whether a local TCP port is answering."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except (OSError, socket.error):
        return False


def test_socks5_alive(port: int, host: str = "127.0.0.1", timeout: float = 1.2) -> bool:
    """Verify that a given port responds to a standard SOCKS5 handshake."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.sendall(b"\x05\x01\x00")
            resp = s.recv(2)
            return len(resp) == 2 and resp[0] == 5 and resp[1] == 0
    except Exception:
        return False


def find_tor_exe() -> Path | None:
    """Locate portable or installed tor.exe across system and portable paths."""
    candidates = [
        TOR_DIR / "tor" / "tor.exe",
        TOR_DIR / "Tor" / "tor.exe",
        TOR_DIR / "tor.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
        Path.home() / "Desktop" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
        Path.home() / "OneDrive" / "Desktop" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Tor Browser" / "Browser" / "TorBrowser" / "Tor" / "tor.exe",
    ]
    for c in candidates:
        if c.is_file():
            return c

    # System PATH lookup
    system_tor = shutil.which("tor.exe") or shutil.which("tor")
    if system_tor and os.path.isfile(system_tor):
        return Path(system_tor)

    # Recursive search if unzipped in custom subfolder
    if TOR_DIR.is_dir():
        for p in TOR_DIR.rglob("tor.exe"):
            if p.is_file():
                return p
    return None


def get_active_tor_target() -> tuple[str, int] | None:
    """Return (host, port) of the first live Tor SOCKS service (portable, Tor Browser, or daemon)."""
    # Prefer Tor Browser if user already has it running
    for p in (TOR_BROWSER_SOCKS_PORT, TOR_DEFAULT_SOCKS_PORT):
        if test_socks5_alive(p):
            return ("127.0.0.1", p)
    return None


def fetch_tor_bundle_url() -> list[str]:
    """Get list of mirror download URLs for Tor Expert Bundle."""
    urls = []
    api_url = "https://aus1.torproject.org/torbrowser/update_3/release/downloads.json"
    req = urllib.request.Request(api_url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            version = data.get("version")
            if version:
                urls.append(f"https://dist.torproject.org/torbrowser/{version}/tor-expert-bundle-windows-x86_64-{version}.tar.gz")
                urls.append(f"https://archive.org/download/tor-expert-bundle-{version}/tor-expert-bundle-windows-x86_64-{version}.tar.gz")
    except Exception:
        pass

    urls.extend([
        "https://archive.org/download/tor-expert-bundle-14.0.7/tor-expert-bundle-windows-x86_64-14.0.7.tar.gz",
        "https://tor.calyxinstitute.org/dist/torbrowser/14.0.7/tor-expert-bundle-windows-x86_64-14.0.7.tar.gz",
        "https://tor.eff.org/dist/torbrowser/14.0.7/tor-expert-bundle-windows-x86_64-14.0.7.tar.gz",
        "https://dist.torproject.org/torbrowser/14.0.7/tor-expert-bundle-windows-x86_64-14.0.7.tar.gz",
    ])
    return urls


def download_and_extract_tor() -> tuple[bool, str]:
    """Download portable Tor Expert Bundle with multi-mirror support and extract."""
    ensure_dirs()
    TOR_DIR.mkdir(parents=True, exist_ok=True)
    TOR_DATA_DIR.mkdir(parents=True, exist_ok=True)

    existing_exe = find_tor_exe()
    if existing_exe:
        return True, L(f"Tor already installed at {existing_exe}", f"Tor ya está instalado en {existing_exe}")

    bundle_urls = fetch_tor_bundle_url()
    tar_path = TOR_DIR / f"tor-expert-bundle-{os.getpid()}.tar.gz"   # per process: app and guard may both download

    downloaded = False
    last_err = ""
    for url in bundle_urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as src, open(tar_path, "wb") as dst:
                shutil.copyfileobj(src, dst)
            if tar_path.is_file() and tar_path.stat().st_size > 1024 * 1024:
                downloaded = True
                break
        except Exception as exc:
            last_err = str(exc)
            continue

    if not downloaded:
        return False, L(f"Couldn't download Tor: {last_err or 'Network timeout'}",
                        f"No se pudo descargar Tor: {last_err or 'la red no respondió'}")

    # Extract using system tar.exe or python tarfile fallback
    extracted = False
    tar_exe = shutil.which("tar.exe") or shutil.which("tar")
    if tar_exe:
        res = run_hidden([tar_exe, "-xf", str(tar_path), "-C", str(TOR_DIR)], timeout=30.0)
        if res.returncode == 0:
            extracted = True

    if not extracted:
        try:
            with tarfile.open(tar_path, "r:gz") as t:
                t.extractall(TOR_DIR)
            extracted = True
        except Exception as exc:
            return False, L(f"Couldn't unpack Tor: {exc}", f"No se pudo descomprimir Tor: {exc}")

    try:
        tar_path.unlink(missing_ok=True)
    except Exception:
        pass

    exe = find_tor_exe()
    if exe and exe.is_file():
        return True, L(f"Tor installed at {exe}", f"Tor instalado en {exe}")
    return False, L("Tor was unpacked but tor.exe wasn't found.", "Tor se descomprimió pero no aparece tor.exe.")


def is_portable_tor_running() -> bool:
    """Check if our managed portable tor.exe process is alive."""
    if not TOR_PID_FILE.is_file():
        return False
    try:
        pid = int(TOR_PID_FILE.read_text(encoding="utf-8").strip())
        import psutil
        proc = psutil.Process(pid)
        return proc.is_running() and "tor" in proc.name().lower()
    except Exception:
        return False


def start_portable_tor() -> tuple[bool, str]:
    """Start the portable Tor process in the background."""
    if test_socks5_alive(TOR_DEFAULT_SOCKS_PORT) or test_socks5_alive(TOR_BROWSER_SOCKS_PORT):
        target = get_active_tor_target()
        return True, L(f"Tor is already running on {target[0]}:{target[1]}",
                       f"Tor ya está en marcha en {target[0]}:{target[1]}")

    exe = find_tor_exe()
    if not exe:
        ok, msg = download_and_extract_tor()
        if not ok:
            return False, msg
        exe = find_tor_exe()
        if not exe:
            return False, L("tor.exe is missing after installing.", "Falta tor.exe después de instalar.")

    TOR_DATA_DIR.mkdir(parents=True, exist_ok=True)
    TOR_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)

    args = [
        str(exe),
        "--SocksPort",
        f"127.0.0.1:{TOR_DEFAULT_SOCKS_PORT}",
        "--ControlPort",
        "127.0.0.1:9051",
        "--CookieAuthentication",
        "0",
        "--DataDirectory",
        str(TOR_DATA_DIR),
        "--Log",
        f"notice file {TOR_LOG_FILE}",
    ]

    try:
        proc = subprocess.Popen(
            args,
            creationflags=CREATE_NO_WINDOW | DETACHED_PROCESS,
            close_fds=True,
        )
        TOR_PID_FILE.write_text(str(proc.pid), encoding="utf-8")
    except Exception as exc:
        return False, L(f"Couldn't start tor.exe: {exc}", f"No se pudo iniciar tor.exe: {exc}")

    # Wait for SOCKS port to open (up to 25s for first bootstrap)
    for _ in range(25):
        if test_socks5_alive(TOR_DEFAULT_SOCKS_PORT):
            return True, L(f"Tor started on 127.0.0.1:{TOR_DEFAULT_SOCKS_PORT}",
                           f"Tor iniciado en 127.0.0.1:{TOR_DEFAULT_SOCKS_PORT}")
        time.sleep(1.0)

    return False, L(f"Tor started (PID {proc.pid}) but port 9050 didn't answer in time.",
                    f"Tor arrancó (PID {proc.pid}) pero el puerto 9050 no respondió a tiempo.")


def stop_portable_tor() -> tuple[bool, str]:
    """Terminate the managed portable Tor process."""
    if not TOR_PID_FILE.is_file():
        return True, L("Tor isn't running.", "Tor no está en marcha.")
    try:
        pid = int(TOR_PID_FILE.read_text(encoding="utf-8").strip())
        import psutil
        proc = psutil.Process(pid)
        if "tor" in proc.name().lower():
            proc.kill()
        TOR_PID_FILE.unlink(missing_ok=True)
        return True, L(f"Tor stopped (PID {pid}).", f"Tor detenido (PID {pid}).")
    except Exception as exc:
        TOR_PID_FILE.unlink(missing_ok=True)
        return True, L(f"Tor stopped ({exc}).", f"Tor detenido ({exc}).")


def signal_tor_newnym(control_port: int = 9051) -> bool:
    """Send SIGNAL NEWNYM to Tor control port to build a fresh circuit and exit node."""
    try:
        with socket.create_connection(("127.0.0.1", control_port), timeout=1.2) as s:
            s.sendall(b'AUTHENTICATE ""\r\n')
            resp = s.recv(512)
            if b"250" in resp:
                s.sendall(b"SIGNAL NEWNYM\r\n")
                resp2 = s.recv(512)
                return b"250" in resp2
    except Exception:
        pass
    return False


TOR_CONTROL_PORT = 9051   # openhop's portable Tor (see start_portable_tor); Tor Browser uses 9151
TOR_EXCLUDE_MAX = 200     # most exit addresses we ask Tor to skip (the newest ones)


def _read_control_reply(sock: socket.socket, buf: bytearray) -> tuple[int, list[str]]:
    """One Tor control-port reply: (status code, text of its lines). Handles 250-, 250+ (data) and 250 lines."""
    lines: list[str] = []
    in_data = False
    while True:
        while b"\r\n" not in buf:
            if len(buf) > 1_000_000:
                raise ConnectionError("Tor control reply too long")
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("Tor closed the control connection")
            buf += chunk
        i = buf.index(b"\r\n")
        line = bytes(buf[:i]).decode("utf-8", errors="replace")
        del buf[:i + 2]
        if in_data:
            if line == ".":
                in_data = False
            else:
                lines.append(line)
            continue
        if len(line) < 4:
            return 0, lines + [line]
        lines.append(line[4:])
        if line[3] == "+":
            in_data = True
        elif line[3] == " ":
            try:
                return int(line[:3]), lines
            except ValueError:
                return 0, lines


def tor_control(commands: list[str], port: int = TOR_CONTROL_PORT,
                timeout: float = 3.0) -> list[tuple[int, list[str]]] | None:
    """Run control-port commands with the same login signal_tor_newnym() uses (no password).

    Returns one (code, lines) per command, or None when Tor isn't there or won't let us in.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as s:
            buf = bytearray()
            s.sendall(b'AUTHENTICATE ""\r\n')
            if _read_control_reply(s, buf)[0] != 250:
                return None
            out = []
            for cmd in commands:
                if "\r" in cmd or "\n" in cmd:
                    raise ValueError("bad control command")
                s.sendall(cmd.encode("ascii") + b"\r\n")
                out.append(_read_control_reply(s, buf))
            try:
                s.sendall(b"QUIT\r\n")
            except OSError:
                pass
            return out
    except Exception:
        return None


def owns_tor_control() -> bool:
    """True only when OpenCode goes through openhop's own portable Tor and port 9051 is that same Tor.

    Settings are only ever changed on this Tor: a Tor Browser or a system Tor belongs to the user (their
    browsing, other apps), and Tor Browser can save its settings to disk, so a change could stick there.
    """
    if not is_portable_tor_running():
        return False
    if get_active_tor_target() != ("127.0.0.1", TOR_DEFAULT_SOCKS_PORT):
        return False   # Tor Browser (9150) is the one in use: leave it alone
    try:
        pid = TOR_PID_FILE.read_text(encoding="utf-8").strip()
    except Exception:
        return False
    res = tor_control(["GETINFO process/pid"])
    if not res or res[0][0] != 250:
        return False
    for line in res[0][1]:
        if line.startswith("process/pid="):
            return line.split("=", 1)[1].strip() == pid
    return False


_relays_cache: dict = {"at": 0.0, "addrs": []}


def tor_relay_addresses(max_age: float = 3600.0) -> list[str]:
    """IPv4 addresses of the relays openhop's own Tor knows (from its consensus), cached for an hour.

    Turbo uses them to turn the shared list (hashes only) back into exits Tor can skip. Empty when it isn't
    openhop's own Tor.
    """
    now = time.time()
    if _relays_cache["addrs"] and now - _relays_cache["at"] < max_age:
        return list(_relays_cache["addrs"])
    if not owns_tor_control():
        return []
    res = tor_control(["GETINFO ns/all"], timeout=15.0)
    if not res or res[0][0] != 250:
        return []
    addrs: list[str] = []
    for line in res[0][1]:
        if line.startswith("r "):
            parts = line.split()
            if len(parts) >= 8:
                addrs.append(parts[6])
    if addrs:
        _relays_cache.update(at=now, addrs=sorted(set(addrs)))
    return list(_relays_cache["addrs"])


def set_tor_exclude_exits(addrs: list[str]) -> tuple[bool, int]:
    """Ask openhop's own Tor not to use these exit addresses (IPv4). Returns (done, how many).

    SETCONF only lives in Tor's memory (gone when Tor restarts); never SAVECONF. An empty list clears it.
    If Tor rejects the list, the option is cleared so Tor works exactly as before.
    """
    import ipaddress

    if not owns_tor_control():
        return False, 0
    clean: list[str] = []
    for a in addrs or []:
        try:
            ip = str(ipaddress.IPv4Address(str(a).strip()))
        except ValueError:
            continue
        if ip in clean:
            clean.remove(ip)
        clean.append(ip)
    clean = clean[-TOR_EXCLUDE_MAX:]
    if not clean:
        return _reset_exclude(), 0
    res = tor_control([f'SETCONF ExcludeExitNodes="{",".join(clean)}"'])
    if res and res[0][0] == 250:
        return True, len(clean)
    _reset_exclude()
    return False, 0


def _reset_exclude() -> bool:
    res = tor_control(["RESETCONF ExcludeExitNodes"])
    return bool(res and res[0][0] == 250)


def clear_tor_exclude_exits() -> bool:
    """Let openhop's own Tor use every exit again (only touches openhop's own Tor)."""
    if not owns_tor_control():
        return False
    return _reset_exclude()


def rotate_tor_circuit() -> tuple[bool, str]:
    """Rotate Tor circuit for a fresh exit node IP and reset quota."""
    # 1. Try Tor control port SIGNAL NEWNYM first (Tor Browser: 9151, System Tor: 9051)
    for port in (9051, 9151):
        if signal_tor_newnym(port):
            time.sleep(1.2)
            return True, L(f"New Tor IP (through control port {port}).", f"IP de Tor nueva (por el puerto de control {port}).")

    # 2. If control port not active, cycle portable Tor daemon if running
    if is_portable_tor_running():
        stop_portable_tor()
        time.sleep(1.0)
        return start_portable_tor()

    active = get_active_tor_target()
    if active:
        return True, L(f"Your own Tor is running on {active[0]}:{active[1]} (asked it for a new IP).",
                       f"Tu propio Tor está en marcha en {active[0]}:{active[1]} (se le ha pedido una IP nueva).")
    return start_portable_tor()


def open_socks5_tunnel(
    dest_host: str,
    dest_port: int,
    socks_host: str = "127.0.0.1",
    socks_port: int = 9050,
    timeout: float = 12.0,
) -> socket.socket:
    """Establish an upstream TCP tunnel through a SOCKS5 proxy server."""
    sock = socket.create_connection((socks_host, socks_port), timeout=timeout)
    # SOCKS5 greeting
    sock.sendall(b"\x05\x01\x00")
    resp = sock.recv(2)
    if len(resp) < 2 or resp[0] != 5 or resp[1] != 0:
        sock.close()
        raise ConnectionError("SOCKS5 proxy authentication failed or invalid protocol")

    # Connect command: support IPv4 (ATYP=1), IPv6 (ATYP=4), and Domain (ATYP=3)
    port_bytes = dest_port.to_bytes(2, "big")
    try:
        ip_v4 = socket.inet_aton(dest_host)
        req = b"\x05\x01\x00\x01" + ip_v4 + port_bytes
    except OSError:
        try:
            ip_v6 = socket.inet_pton(socket.AF_INET6, dest_host)
            req = b"\x05\x01\x00\x04" + ip_v6 + port_bytes
        except (OSError, AttributeError):
            host_bytes = dest_host.encode("idna")
            req = b"\x05\x01\x00\x03" + bytes([len(host_bytes)]) + host_bytes + port_bytes
    sock.sendall(req)

    reply = sock.recv(4)
    if len(reply) < 4 or reply[1] != 0:
        sock.close()
        code = reply[1] if len(reply) >= 2 else -1
        raise ConnectionError(f"SOCKS5 proxy connection failed (error code {code})")

    atyp = reply[3]
    to_read = 6 if atyp == 1 else (18 if atyp == 4 else 0)
    if atyp == 3:
        raw_len = sock.recv(1)
        if raw_len:
            to_read = raw_len[0] + 2
    read_so_far = 0
    while read_so_far < to_read:
        chunk = sock.recv(min(512, to_read - read_so_far))
        if not chunk:
            break
        read_so_far += len(chunk)

    sock.settimeout(None)
    return sock

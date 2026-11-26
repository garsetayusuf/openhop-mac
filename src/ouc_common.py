"""Shared paths, process helpers, WARP controls, and IPv6 utilities (Windows and Linux)."""

from __future__ import annotations

import contextlib
import ctypes
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Iterator

APP_NAME = "openhop"
VERSION = "3.8.0"
BRAND = "openhop"
# Before 3.0 the app was called "Opencode Unlimited". These names are only used to migrate old installs
# and to recognise (and clean up) what an old version left behind.
OLD_BRAND = "opencode-unlimited"
CREATE_NO_WINDOW = 0x08000000
IS_WINDOWS = sys.platform == "win32"
IS_LINUX = sys.platform.startswith("linux")
# Only Windows knows creationflags; elsewhere the flag must be 0.
NO_WINDOW_FLAGS = CREATE_NO_WINDOW if IS_WINDOWS else 0

IS_MACOS = sys.platform == "darwin"

if IS_WINDOWS:
    LOCAL_APPDATA = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
elif IS_MACOS:
    # MACOS PORT: on macOS the state dir is ~/Library/Application Support/openhop.
    # See src/ouc_macos.py for the launchd / WARP / OpenCode macOS paths.
    LOCAL_APPDATA = Path.home() / "Library" / "Application Support"
else:
    # XDG data home (~/.local/share): where the Linux version keeps its state, like %LOCALAPPDATA% on Windows.
    LOCAL_APPDATA = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
DEST_DIR = LOCAL_APPDATA / BRAND
OLD_DEST_DIR = LOCAL_APPDATA / OLD_BRAND
SCRIPTS_DIR = DEST_DIR / "scripts"
BIN_DIR = DEST_DIR / "bin"
GUARD_DIR = DEST_DIR / "guard"
KEEPER_DIR = DEST_DIR / "keeper"
LANE_DIR = DEST_DIR / "lane"

PAUSE_FILE = DEST_DIR / "keeper.pause"
GUARD_STATE_FILE = GUARD_DIR / "state.json"
GUARD_LOG_FILE = GUARD_DIR / "guard.log"
KEEPER_LOG_FILE = KEEPER_DIR / "keeper.log"
_OPENCODE_LOG_CANDIDATES = (
    Path.home() / ".local" / "share" / "opencode" / "log" / "opencode.log",
    Path.home() / "Library" / "Application Support" / "opencode" / "log" / "opencode.log",
)
OPENCODE_LOG_FILE = next((p for p in _OPENCODE_LOG_CANDIDATES if p.is_file()),
                         _OPENCODE_LOG_CANDIDATES[1] if sys.platform == "darwin" else _OPENCODE_LOG_CANDIDATES[0])
OPENCODE_DATA_DIR = Path.home() / ".local" / "share" / "opencode"


def opencode_db_path() -> Path:
    """OpenCode's chat database: the most recently used opencode*.db in its data folder.

    Usually 'opencode.db', but some OpenCode builds keep a database per release channel
    ('opencode-<channel>.db'). Picking the one written last means openhop reads the chats the user is
    really using. Falls back to 'opencode.db' (which may not exist) so callers can keep their is_file() check.
    """
    default = OPENCODE_DATA_DIR / "opencode.db"
    best, best_t = default, -1.0
    try:
        for db in OPENCODE_DATA_DIR.glob("opencode*.db"):
            if not db.is_file():
                continue
            t = db.stat().st_mtime
            wal = db.with_name(db.name + "-wal")
            if wal.is_file():
                t = max(t, wal.stat().st_mtime)
            if t > best_t:
                best, best_t = db, t
    except OSError:
        pass
    return best

TASK_KEEPER = "openhop-Keeper"
TASK_GUARD = "openhop-Guard"
MUTEX_KEEPER = "openhop-Keeper"
MUTEX_GUARD = "openhop-Guard"
FW_RULE_PREFIX = "OPENHOP_LEAKGUARD_"
OLD_TASK_KEEPER = "OpencodeUnlimited-Keeper"
OLD_TASK_GUARD = "OpencodeUnlimited-Guard"
OLD_MUTEX_KEEPER = "OpencodeUnlimitedKeeper"
OLD_MUTEX_GUARD = "OpencodeUnlimitedGuard"
OLD_FW_RULE_PREFIX = "OUC_LEAKGUARD_"
SERVICE_PROXY_PORT = int(os.environ.get("OUC_SERVICE_PROXY_PORT", "41987"))

if IS_WINDOWS:
    _WARP_CANDIDATES = (
        Path(r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"),
        Path(r"C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe"),
        LOCAL_APPDATA / "Cloudflare" / "Cloudflare WARP" / "warp-cli.exe",
    )
    _OPENCODE_CANDIDATES = (
        LOCAL_APPDATA / "Programs" / "ai.opencode.desktop" / "resources" / "opencode-cli.exe",
        Path.home() / ".opencode" / "bin" / "opencode.exe",
        LOCAL_APPDATA / "opencode" / "opencode.exe",
        Path.home() / ".bun" / "bin" / "opencode.exe",
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "npm" / "opencode.cmd",
    )
else:
    _WARP_CANDIDATES = (Path("/usr/bin/warp-cli"), Path("/bin/warp-cli"), Path("/usr/local/bin/warp-cli"))
    _OPENCODE_CANDIDATES = (
        Path.home() / ".opencode" / "bin" / "opencode",
        Path.home() / ".bun" / "bin" / "opencode",
        Path.home() / ".local" / "bin" / "opencode",
        Path.home() / ".npm-global" / "bin" / "opencode",
        Path("/usr/local/bin/opencode"),
        Path("/usr/bin/opencode"),
        Path("/opt/OpenCode/resources/opencode-cli"),
        Path("/usr/lib/opencode-desktop/resources/opencode-cli"),
    )

_FALLBACK_OPENCODE_AAAA = (
    "2606:4700::6810:7f60",
    "2606:4700::6810:8060",
    "2606:4700::6810:84e5",
    "2606:4700::6810:85e5",
    "2606:4700::6811:d209",
    "2606:4700::6812:a42",
    "2606:4700::6812:b42",
    "2606:4700:10::6814:2011",
    "2606:4700:10::6814:2111",
    "2606:4700:10::6814:2211",
    "2606:4700:10::6814:2311",
    "2606:4700:10::ac42:ad95",
    "2606:4700:10::ac42:ad96",
    "2606:4700:10::ac42:ad97",
    "2606:4700:10::ac42:ad98",
    "2606:4700:20::681a:641",
    "2606:4700:20::681a:741",
    "2606:4700:20::681a:841",
    "2606:4700:20::681a:941",
    "2606:4700:30::6818:101",
    "2606:4700:30::6818:201",
    "2606:4700:30::6818:301",
    "2606:4700:30::6818:401",
    "2606:4700:30::ac43:8001",
    "2606:4700:30::ac43:8002",
    "2606:4700:30::ac43:9001",
    "2606:4700:30::ac43:9002",
    "2606:4700:30::ac43:a001",
    "2606:4700:30::ac43:a002",
    "2606:4700:31::a29f:8001",
    "2606:4700:31::a29f:8002",
    "2606:4700:31::a29f:9001",
    "2606:4700:31::a29f:9002",
    "2606:4700:e0::ac40:9001",
    "2606:4700:e0::ac40:9002",
)

_IPV6_RE = re.compile(r"\b([0-9a-fA-F:]+:[0-9a-fA-F:]+)\b")


def is_own_path(path: str) -> bool:
    """True for files that belong to openhop (or the old Opencode Unlimited), e.g. our 'opencode' shims.

    Matches a whole folder name, so a real OpenCode install is never mistaken for ours."""
    parts = {p for p in re.split(r"[\\/]+", (path or "").lower()) if p}
    return BRAND in parts or OLD_BRAND in parts or any("opencode-infinite" in p for p in parts)


def _is_wrapper_script(path: Path) -> bool:
    """True for small shell scripts that only wrap OpenCode (ours, or the old Linux version's), not OpenCode."""
    try:
        if path.stat().st_size > 64 * 1024:
            return False
        head = path.read_bytes()[:4096].lower()
        return head.startswith(b"#!") and any(w in head for w in (b"openhop", b"opencode-unlimited", b"zen-lane", b"ouc_real_bin"))
    except Exception:
        return False


def _usable_opencode(path: Path | str) -> bool:
    p = Path(path)
    return p.is_file() and not is_own_path(str(p)) and not _is_wrapper_script(p) and (IS_WINDOWS or os.access(p, os.X_OK))


def ensure_dirs() -> None:
    """Create state and log directories if missing."""
    for d in (DEST_DIR, SCRIPTS_DIR, BIN_DIR, GUARD_DIR, KEEPER_DIR, LANE_DIR):
        d.mkdir(parents=True, exist_ok=True)


def run_hidden(
    args: list[str],
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    """Run a subprocess without spawning a visible Windows console window."""
    return subprocess.run(
        args,
        capture_output=True,
        text=True,
        errors="replace",
        timeout=timeout,
        creationflags=NO_WINDOW_FLAGS,
        stdin=subprocess.DEVNULL,
    )


def find_warp_cli() -> str | None:
    """Locate Cloudflare warp-cli.exe."""
    env_bin = os.environ.get("OUC_WARP_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin
    on_path = shutil.which("warp-cli")
    if on_path:
        return on_path
    for candidate in _WARP_CANDIDATES:
        if candidate.is_file():
            return str(candidate)
    return None


def find_opencode_cli() -> str | None:
    """Locate the real opencode CLI binary, skipping wrapper shims."""
    env_bin = os.environ.get("OUC_REAL_BIN", "").strip()
    if env_bin and Path(env_bin).is_file():
        return env_bin

    # 1. Check running processes first
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            try:
                name = (p.info["name"] or "").lower()
                exe = p.info["exe"] or ""
                # OpenCode Desktop's own window is also "OpenCode.exe" (in ai.opencode.desktop\); that's the GUI,
                # not the CLI, so only take the CLI binary from there (resources\opencode-cli.exe).
                parts = {p for p in re.split(r"[\\/]+", exe.lower()) if p}
                is_desktop_gui = name == "opencode.exe" and "ai.opencode.desktop" in parts and "resources" not in parts
                is_cli_name = "opencode-cli" in name or name == "opencode.exe" or (not IS_WINDOWS and name == "opencode")
                if is_cli_name and not is_own_path(exe) and not is_desktop_gui:
                    if _usable_opencode(exe):
                        return str(exe)
            except Exception:
                pass
    except Exception:
        pass

    # 2. Check standard candidate paths
    for candidate in _OPENCODE_CANDIDATES:
        if _usable_opencode(candidate):
            return str(candidate)

    # 3. Check AppData Roaming CLI versions
    appdata = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
    cli_dir = appdata / "ai.opencode.desktop" / "cli"
    if cli_dir.is_dir():
        for p in sorted(cli_dir.rglob("opencode-cli.exe"), reverse=True):
            if p.is_file():
                return str(p)

    # 4. PATH lookup
    for name in ("opencode.exe", "opencode"):
        found = shutil.which(name)
        if not found:
            continue
        if not _usable_opencode(found):
            continue
        return found
    if not IS_WINDOWS:
        # Linux: search PATH without our own bin folder (our 'opencode' shim comes first there)
        for d in os.environ.get("PATH", "").split(os.pathsep):
            if d and not is_own_path(d) and _usable_opencode(Path(d) / "opencode"):
                return str(Path(d) / "opencode")
    return None


def _is_desktop_exe(exe: str) -> bool:
    """True for OpenCode Desktop (Electron: resources\app.asar beside it), False for the CLI or anything else."""
    try:
        p = Path(exe)
        return p.is_file() and (p.parent / "resources" / "app.asar").is_file()
    except Exception:
        return False


def find_opencode_desktop() -> str | None:
    """Locate OpenCode Desktop GUI executable if installed."""
    if not IS_WINDOWS:
        return _find_opencode_desktop_linux()
    # 1. Check running processes first
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            try:
                name = (p.info["name"] or "").lower()
                exe = p.info["exe"] or ""
                # The CLI is ALSO called opencode.exe; only the Electron app has resourcespp.asar next to it.
                if name == "opencode.exe" and not is_own_path(exe) and _is_desktop_exe(exe):
                    return str(exe)
            except Exception:
                pass
    except Exception:
        pass

    candidates = (
        LOCAL_APPDATA / "Programs" / "ai.opencode.desktop" / "OpenCode.exe",
        LOCAL_APPDATA / "Programs" / "OpenCode" / "OpenCode.exe",
        LOCAL_APPDATA / "OpenCode" / "OpenCode.exe",
        Path(r"C:\Program Files\OpenCode\OpenCode.exe"),
        Path(r"C:\Program Files\ai.opencode.desktop\OpenCode.exe"),
        Path(r"C:\Program Files (x86)\OpenCode\OpenCode.exe"),
        Path(r"C:\Program Files (x86)\ai.opencode.desktop\OpenCode.exe"),
    )
    for c in candidates:
        if c.is_file() and _is_desktop_exe(str(c)):
            return str(c)

    # 3. Check Desktop and Start Menu shortcuts (.lnk)
    try:
        import win32com.client
        shell = win32com.client.Dispatch("WScript.Shell")
        shortcut_dirs = (
            Path.home() / "Desktop",
            Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "Microsoft" / "Windows" / "Start Menu" / "Programs",
        )
        for sdir in shortcut_dirs:
            if sdir.is_dir():
                for lnk in sdir.rglob("*opencode*.lnk"):
                    if any(w in lnk.name.lower() for w in ("unlimited", "infinite", "openhop")):
                        continue
                    try:
                        sc = shell.CreateShortCut(str(lnk))
                        target_path = sc.Targetpath
                        if target_path and target_path.lower().endswith("opencode.exe") and _is_desktop_exe(target_path):
                            return str(target_path)
                    except Exception:
                        pass
    except Exception:
        pass

    return None


# OpenCode Desktop on Linux: the .deb/.rpm install the Electron app under /opt or /usr/lib and put a
# launcher on PATH; the AppImage is a single file wherever the user saved it.
# The official .deb/.rpm (2.3.0) install /opt/OpenCode/ai.opencode (+ resources/app.asar, resources/opencode-cli).
_LINUX_DESKTOP_NAMES = ("ai.opencode", "opencode-desktop", "OpenCode", "opencode-electron", "ai.opencode.desktop")


def _find_opencode_desktop_linux() -> str | None:
    try:
        import psutil
        for p in psutil.process_iter(["name", "exe"]):
            try:
                exe = p.info.get("exe") or ""
                low = exe.lower()
                if exe and not is_own_path(exe) and "opencode" in low and "resources" not in low and _is_desktop_exe(exe):
                    return exe
            except Exception:
                pass
    except Exception:
        pass
    for name in ("opencode-desktop",):
        found = shutil.which(name)
        if found and not is_own_path(found):
            return found
    for d in (Path("/opt/OpenCode"), Path("/opt/opencode-desktop"), Path("/opt/opencode"), Path("/usr/lib/opencode-desktop"),
              Path("/usr/lib/OpenCode"), Path("/opt/ai.opencode.desktop")):
        for name in _LINUX_DESKTOP_NAMES + ("opencode",):
            c = d / name
            if c.is_file() and os.access(c, os.X_OK):
                return str(c)
    for d in (Path.home() / "Applications", Path.home() / "Downloads", Path.home() / ".local" / "bin", Path.home() / "Desktop"):
        try:
            for c in sorted(d.glob("*.AppImage")):
                if "opencode" in c.name.lower() and "desktop" in c.name.lower():
                    return str(c)
        except Exception:
            pass
    return None


def run_warp(
    args: list[str],
    warp_bin: str | None = None,
    timeout: float = 15.0,
) -> tuple[int, str]:
    """Invoke warp-cli.exe, probing --accept-tos first then falling back."""
    exe = warp_bin or find_warp_cli()
    if not exe:
        return 1, "warp-cli.exe not found"

    for attempt in ([exe, "--accept-tos", *args], [exe, *args]):
        try:
            res = run_hidden(attempt, timeout=timeout)
            out = (res.stdout or "") + ("\n" + res.stderr if res.stderr else "")
            if res.returncode == 0:
                return 0, out.strip()
        except Exception as exc:
            out = str(exc)
    return 1, out.strip()


def warp_mode(warp_bin: str | None = None) -> str:
    """Return current WARP operation_mode from settings JSON (e.g. 'warp')."""
    rc, out = run_warp(["-j", "settings", "list"], warp_bin=warp_bin, timeout=6.0)
    if rc == 0 and out.startswith("{"):
        try:
            data = json.loads(out)
            op = data.get("settings", {}).get("operation_mode") or data.get("operation_mode")
            if isinstance(op, str):
                return op
            if isinstance(op, dict):
                return ",".join(op.keys())
        except Exception:
            pass
    return ""


def get_warp_protocol(warp_bin: str | None = None) -> str:
    """Return current WARP protocol (e.g. 'wireguard' or 'masque') in lowercase."""
    rc, out = run_warp(["-j", "settings"], warp_bin=warp_bin, timeout=5.0)
    if rc == 0 and "{" in out:
        try:
            idx_start = out.find("{")
            idx_end = out.rfind("}")
            if idx_start != -1 and idx_end != -1:
                data = json.loads(out[idx_start : idx_end + 1])
                proto = (
                    data.get("settings", {})
                    .get("warp_tunnel_protocol", "")
                    .lower()
                    .strip()
                )
                if proto:
                    return proto
        except Exception:
            pass
    rc2, out2 = run_warp(["settings"], warp_bin=warp_bin, timeout=5.0)
    if rc2 == 0 and out2:
        for line in out2.splitlines():
            if "protocol" in line.lower():
                if "wireguard" in line.lower():
                    return "wireguard"
                if "masque" in line.lower():
                    return "masque"
    return ""


def ensure_warp_protocol(desired: str = "wireguard", warp_bin: str | None = None) -> bool:
    """Ensure WARP tunnel protocol is set to desired without touching it if already set."""
    want = "masque" if desired.lower().strip() == "masque" else "wireguard"
    current = get_warp_protocol(warp_bin=warp_bin)
    if current == want:
        return True
    rc, _ = run_warp(["tunnel", "protocol", "set", "MASQUE" if want == "masque" else "WireGuard"],
                     warp_bin=warp_bin, timeout=10.0)
    return rc == 0


# App-Only ("isolated") is the default WARP scope: only OpenCode goes through WARP (local SOCKS5 on :40000),
# the rest of the PC stays on the normal connection. "tun" = whole PC through WARP.
DEFAULT_WARP_SCOPE = "isolated"
WARP_PROXY_PORT = 40000


def current_warp_scope() -> str:
    """The configured WARP scope ('isolated' or 'tun'). Single source of truth for every module."""
    try:
        from proxy_chain import get_egress_config
        raw = str(get_egress_config().get("warp_scope", DEFAULT_WARP_SCOPE)).lower().strip()
        return "tun" if raw == "tun" else "isolated"
    except Exception:
        return DEFAULT_WARP_SCOPE


def preferred_warp_protocol(scope: str | None = None) -> str:
    """WARP's local proxy mode only works over MASQUE (WARP 2026.x rejects it on WireGuard with
    'Proxy mode only supports MASQUE', and the WARP tray app then flips the mode back to full TUN),
    so App-Only needs MASQUE; full TUN keeps WireGuard."""
    return "masque" if (scope or current_warp_scope()) == "isolated" else "wireguard"


def warp_settings(warp_bin: str | None = None) -> dict:
    """WARP's current settings ({} when warp-cli doesn't answer). Read-only."""
    rc, out = run_warp(["-j", "settings", "list"], warp_bin=warp_bin, timeout=6.0)
    if rc == 0 and "{" in out:
        try:
            data = json.loads(out[out.find("{"):out.rfind("}") + 1])
            got = data.get("settings", data)
            return got if isinstance(got, dict) else {}
        except Exception:
            pass
    return {}


def _setting_str(value) -> str:
    if isinstance(value, dict):
        return ",".join(value.keys()).lower()
    return str(value or "").lower()


def apply_warp_mode(scope: str | None = None, warp_bin: str | None = None) -> None:
    """Put WARP into the operation mode (and protocol) the given scope needs. Does not connect.

    Only writes what is different: every write is a "settings change" for WARP, which drops the tunnel
    (status "Disconnected, Reason: Settings Changed") even when the value was already right.
    """
    scope = scope or current_warp_scope()
    cur = warp_settings(warp_bin=warp_bin)
    mode_now = _setting_str(cur.get("operation_mode"))
    if scope == "isolated":
        ensure_warp_protocol("masque", warp_bin=warp_bin)
        if str(cur.get("proxy_port", "")) != str(WARP_PROXY_PORT):
            run_warp(["proxy", "port", str(WARP_PROXY_PORT)], warp_bin=warp_bin)
        if mode_now != "proxy":
            run_warp(["mode", "proxy"], warp_bin=warp_bin)
    else:
        if mode_now != "warp":
            run_warp(["mode", "warp"], warp_bin=warp_bin)


def warp_status(warp_bin: str | None = None) -> tuple[bool, str, str]:
    """Return (is_connected, mode_name, raw_text) for WARP."""
    rc, out = run_warp(["-j", "status"], warp_bin=warp_bin, timeout=8.0)
    if rc == 0 and "{" in out:
        try:
            idx_start = out.find("{")
            idx_end = out.rfind("}")
            if idx_start != -1 and idx_end != -1:
                data = json.loads(out[idx_start : idx_end + 1])
                status = str(data.get("status", "")).strip()
                connected = status.lower() == "connected"
                mode = ""
                reason = data.get("reason")
                if isinstance(reason, dict):
                    cur = (
                        reason.get("SettingsChanged", {}).get("current", {})
                        if isinstance(reason.get("SettingsChanged"), dict)
                        else {}
                    )
                    op_mode = cur.get("operation_mode")
                    if isinstance(op_mode, str):
                        mode = op_mode
                    elif isinstance(op_mode, dict):
                        mode = ",".join(op_mode.keys())
                if not mode:
                    mode = warp_mode(warp_bin=warp_bin)
                return connected, mode, status
        except Exception:
            pass

    rc2, out2 = run_warp(["status"], warp_bin=warp_bin, timeout=8.0)
    connected = False
    raw_status = ""
    for line in (out2 or "").splitlines():
        line_s = line.strip()
        if "status" in line_s.lower() or "connected" in line_s.lower():
            if not raw_status:
                raw_status = line_s
            if "connected" in line_s.lower() and "disconnected" not in line_s.lower():
                connected = True
                break
    if not raw_status and out2:
        raw_status = out2.splitlines()[0].strip()
    return connected, "", raw_status


_CF_V4_NETS = tuple(ipaddress.IPv4Network(n) for n in (
    "173.245.48.0/20", "103.21.244.0/22", "103.22.200.0/22", "103.31.4.0/22", "141.101.64.0/18",
    "108.162.192.0/18", "190.93.240.0/20", "188.114.96.0/20", "197.234.240.0/22", "198.41.128.0/17",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "131.0.72.0/22",
))


def is_cloudflare_v6(addr: str) -> bool:
    """Return True if the address belongs to Cloudflare/WARP ranges (IPv6, or IPv4 for IPv4-only WARP exits)."""
    try:
        ip = ipaddress.ip_address(addr.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 4:
        return any(ip in n for n in _CF_V4_NETS)
    if ip.is_link_local or ip.is_loopback or ip.is_multicast or ip.is_private:
        return False
    low = ip.exploded.lower()
    return low.startswith(("2606:", "2a09:"))


def get_warp_local_v6() -> str | None:
    """Return the active CloudflareWARP adapter global IPv6 address, or None."""
    if not IS_WINDOWS:
        try:
            import psutil
            import socket as _s
            for ifname, addrs in psutil.net_if_addrs().items():
                for a in addrs:
                    if a.family != _s.AF_INET6:
                        continue
                    cand = a.address.split("%", 1)[0]
                    if (ifname == "CloudflareWARP" or ifname.startswith("cloudflare")) and cand and not cand.startswith("fe80"):
                        return cand
                    if is_cloudflare_v6(cand):
                        return cand
        except Exception:
            pass
        return None
    # 1. Try directly querying the CloudflareWARP named interface
    try:
        res = run_hidden(
            ["netsh", "interface", "ipv6", "show", "addresses", "CloudflareWARP"],
            timeout=5.0,
        )
        if res.returncode == 0:
            for line in res.stdout.splitlines():
                m = _IPV6_RE.search(line)
                if not m:
                    continue
                cand = m.group(1).split("%", 1)[0]
                try:
                    ip = ipaddress.IPv6Address(cand)
                except ValueError:
                    continue
                if ip.is_link_local or ip.is_loopback or ip.is_multicast:
                    continue
                if is_cloudflare_v6(str(ip)) or ip.is_global:
                    return str(ip)
    except Exception:
        pass

    # 2. Localized Windows fallback: query all IPv6 addresses and look for Cloudflare ranges
    try:
        res_all = run_hidden(
            ["netsh", "interface", "ipv6", "show", "addresses"],
            timeout=5.0,
        )
        if res_all.returncode == 0:
            for line in res_all.stdout.splitlines():
                m = _IPV6_RE.search(line)
                if not m:
                    continue
                cand = m.group(1).split("%", 1)[0]
                try:
                    ip = ipaddress.IPv6Address(cand)
                except ValueError:
                    continue
                if ip.is_link_local or ip.is_loopback or ip.is_multicast:
                    continue
                if is_cloudflare_v6(str(ip)):
                    return str(ip)
    except Exception:
        pass
    return None


WARP_LEAK_FILE = LANE_DIR / "warp_leak.json"
# opencode.ai first: it's the exact host whose free limit we're dealing with.
_TRACE_ENDPOINTS = (
    "https://opencode.ai/cdn-cgi/trace",
    "https://cloudflare.com/cdn-cgi/trace",
    "https://1.1.1.1/cdn-cgi/trace",
)


def _parse_trace(text: str) -> dict:
    out: dict = {}
    for line in (text or "").splitlines():
        key, sep, val = line.partition("=")
        if sep and key in ("ip", "warp", "colo"):
            out[key] = val.strip()
    return out


def trace_warp_path(timeout: float = 6.0) -> dict:
    """Ask Cloudflare what it sees for traffic sent the way OpenCode's traffic goes in WARP mode.

    Returns {"ip", "warp", "colo", "path"}; "warp" is "on"/"plus" when it really went through WARP and
    "off" when it went out with the user's own IP. Empty dict when nothing answered.

    Why this exists: WARP's local proxy (port 40000) can keep saying "Connected" while it quietly sends
    traffic outside the tunnel, with the user's real IP. warp-cli status can't see that; only the trace can.
    (Note 2026-11-16: an earlier version of this check passed --noproxy "*" with -x, which disables the
    proxy, so it reported a "leak" that was really the direct path. Fixed in 3.3.4.)
    """
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return {}
    isolated = current_warp_scope() == "isolated"
    # Careful: curl's --noproxy "*" switches -x off too. Use it only for the direct (system) path, never
    # together with -x, or the check measures the user's own connection instead of WARP.
    via = ["-x", f"socks5h://127.0.0.1:{WARP_PROXY_PORT}"] if isolated else ["--noproxy", "*"]
    for ep in _TRACE_ENDPOINTS:
        try:
            res = run_hidden(
                [curl, *via, "-s", "-m", str(max(2, int(timeout))), ep],
                timeout=timeout + 2.0,
            )
        except Exception:
            continue
        info = _parse_trace(res.stdout)
        if info.get("ip"):
            info["path"] = "warp-proxy" if isolated else "system"
            return info
    return {}


def record_warp_path(info: dict) -> None:
    """Remember the last WARP path check (the proxy and the health panel read it)."""
    try:
        WARP_LEAK_FILE.write_text(
            json.dumps({
                "checked": time.time(),
                "warp": info.get("warp", ""),
                "leak": info.get("warp") == "off",
            }),
            encoding="utf-8",
        )
    except Exception:
        pass


def warp_path_leaking(max_age: float = 120.0) -> bool:
    """True when the last check (no older than max_age) saw WARP sending traffic out with the real IP."""
    try:
        data = json.loads(WARP_LEAK_FILE.read_text(encoding="utf-8"))
        return bool(data.get("leak")) and time.time() - float(data.get("checked", 0)) <= max_age
    except Exception:
        return False


EGRESS_EPOCH_FILE = LANE_DIR / "egress_epoch"


def bump_egress_epoch() -> None:
    """Tell the proxy that the exit IP just changed.

    OpenCode keeps its connection to opencode.ai open for minutes. Without this, the 'continue' sent after a
    new IP goes out through that old connection, on the old (used-up) IP, and hits the limit again at once.
    The proxy watches this file and closes OpenCode's open tunnels when it changes.
    """
    try:
        EGRESS_EPOCH_FILE.write_text(str(time.time()), encoding="utf-8")
    except Exception:
        pass


def next_free_reset(now: float | None = None) -> float:
    """When a used-up address is worth trying again.

    OpenCode's free-message counters are daily and reset between 00:00 and 01:00 UTC, each IP at its own
    minute (measured from the 429 retryAfterMs values, Nov 2026). So a used-up address stays used up until
    a bit after 01:00 UTC.
    """
    now = time.time() if now is None else now
    return (int(now // 86400) + 1) * 86400 + 3900


def trace_via_socks(host: str, port: int, timeout: float = 10.0) -> dict:
    """Cloudflare trace through a SOCKS5 proxy (used for the Tor exit). {"ip", "warp", "colo"} or {}."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return {}
    for ep in _TRACE_ENDPOINTS[:2]:
        try:
            res = run_hidden(
                [curl, "-x", f"socks5h://{host}:{port}", "-s", "-m", str(max(3, int(timeout))), ep],
                timeout=timeout + 2.0,
            )
        except Exception:
            continue
        info = _parse_trace(res.stdout)
        if info.get("ip"):
            return info
    return {}


def get_egress_v6(timeout: float = 6.0) -> str:
    """The IP OpenCode's traffic leaves with (IPv6 preferred; an IPv4 WARP exit is accepted).

    In WARP mode it only accepts an answer Cloudflare marks as warp=on. It never falls back to a plain
    'curl -6' / direct check: that measures the user's own connection, so a rotation would "succeed" on
    the user's real (already limited) IP and the limit would come straight back.
    """
    mode = "warp"
    try:
        from proxy_chain import get_egress_config
        mode = get_egress_config().get("mode", "warp")
    except Exception:
        pass
    if mode in ("warp", "clean"):
        info = trace_warp_path(timeout=timeout)
        if info:
            record_warp_path(info)
        if info.get("warp") in ("on", "plus"):
            return info.get("ip", "")
        return ""
    if mode == "vpn":
        # A VPN app (Norton, Proton VPN, ...): ask through its own adapter, never the plain connection.
        try:
            from proxy_chain import get_egress_config
            from vpn_detect import vpn_exit_ip
            return vpn_exit_ip(str(get_egress_config().get("vpn_id") or ""), timeout=timeout)
        except Exception:
            return ""
    return _get_egress_v6_other(timeout)


def _get_egress_v6_other(timeout: float = 6.0) -> str:
    """Egress check for the non-WARP modes (Tor / clean endpoint / custom): the old behaviour."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    endpoints = (
        "https://cloudflare.com/cdn-cgi/trace",
        "https://[2606:4700:4700::1111]/cdn-cgi/trace",
        "https://api64.ipify.org",
        "https://1.1.1.1/cdn-cgi/trace",
    )
    # Some WARP setups only give an IPv4 exit. Keep the first IPv4 seen and return it when no IPv6
    # shows up, instead of "" (which left the UI on "Checking IP" forever and made every rotation "fail").
    v4_seen: list[str] = []

    scope = current_warp_scope()
    mode = "warp"
    try:
        from proxy_chain import get_egress_config
        mode = get_egress_config().get("mode", "warp")
    except Exception:
        pass

    def _extract_ip(text: str) -> str:
        cand = text.strip()
        if "ip=" in text:
            cand = ""
            for line in text.splitlines():
                if line.startswith("ip="):
                    cand = line.split("=", 1)[1].strip()
                    break
        if not cand:
            return ""
        try:
            ip = ipaddress.ip_address(cand)
        except ValueError:
            return ""
        if ip.version == 6:
            return cand
        if not v4_seen:
            v4_seen.append(cand)
        return ""

    if curl:
        # 1. Isolated WARP scope: probe via SOCKS5 proxy port 40000
        if mode == "warp" and scope == "isolated":
            for ep in endpoints:
                try:
                    res = run_hidden(
                        [curl, "-x", "socks5h://127.0.0.1:40000", "-s", "-m", str(int(timeout)), ep],
                        timeout=timeout + 1.0,
                    )
                    cand = _extract_ip(res.stdout.strip())
                    if cand:
                        return cand
                except Exception:
                    pass

        # 2. Tor mode: probe via Tor SOCKS5
        elif mode == "tor":
            for p in (9050, 9150):
                for ep in endpoints:
                    try:
                        res = run_hidden(
                            [curl, "-x", f"socks5h://127.0.0.1:{p}", "-s", "-m", str(int(timeout)), ep],
                            timeout=timeout + 1.0,
                        )
                        cand = _extract_ip(res.stdout.strip())
                        if cand:
                            return cand
                    except Exception:
                        pass

        # 3. TUN scope or fallback: probe native IPv6 (-6)
        for ep in endpoints:
            try:
                res = run_hidden(
                    [curl, "-6", "-s", "-m", str(int(timeout)), ep],
                    timeout=timeout + 1.0,
                )
                cand = _extract_ip(res.stdout.strip())
                if cand:
                    return cand
            except Exception:
                pass

    # Direct urllib fallback (for TUN mode)
    if scope != "isolated":
        for ep in endpoints:
            try:
                req = urllib.request.Request(ep, headers={"User-Agent": "curl/8.0"})
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    text = resp.read().decode("utf-8", errors="replace").strip()
                    cand = _extract_ip(text)
                    if cand:
                        return cand
            except Exception:
                pass
    return v4_seen[0] if v4_seen else ""


def normalize_v6_64(addr: str) -> str:
    """The block the free limit is tracked by: an IPv6 address's /64 prefix, or an IPv4 address itself."""
    clean = (addr or "").strip().split("%", 1)[0]
    if not clean:
        return ""
    if ":" not in clean:
        try:
            return str(ipaddress.IPv4Address(clean))
        except ValueError:
            return ""
    try:
        net = ipaddress.IPv6Network(f"{clean}/64", strict=False)
        return str(net.network_address)
    except ValueError:
        return ""


def is_keeper_paused() -> bool:
    """Check if zen-guard or zen-hop currently holds a valid pause lock."""
    if not PAUSE_FILE.is_file():
        return False
    try:
        data = json.loads(PAUSE_FILE.read_text(encoding="utf-8"))
        until = float(data.get("until", 0))
        if time.time() < until:
            return True
        PAUSE_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    return False


@contextlib.contextmanager
def keeper_pause_ctx(reason: str, seconds: int = 180) -> Iterator[None]:
    """Temporarily pause warp-keeper ticks while rotating WARP registration."""
    ensure_dirs()
    payload = {"until": time.time() + seconds, "reason": reason, "pid": os.getpid()}
    try:
        PAUSE_FILE.write_text(json.dumps(payload), encoding="utf-8")
    except Exception:
        pass
    try:
        yield
    finally:
        try:
            PAUSE_FILE.unlink(missing_ok=True)
        except Exception:
            pass


RUN_DIR = DEST_DIR / "run"
_HELD_LOCKS: dict[str, int] = {}


def _lock_path(name: str) -> Path:
    return RUN_DIR / f"{name}.lock"


def acquire_mutex(name: str) -> int | None:
    """Acquire a named Windows mutex; returns handle if newly owned, else None.
    On Linux: an exclusive flock on ~/.local/share/openhop/run/<name>.lock, held until the process exits."""
    if not IS_WINDOWS:
        import fcntl
        RUN_DIR.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(_lock_path(name)), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        _HELD_LOCKS[name] = fd
        return fd
    kernel32 = ctypes.windll.kernel32
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, f"Local\\{name}")
    if not handle:
        return None
    err = kernel32.GetLastError()
    ERROR_ALREADY_EXISTS = 183
    if err == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return None
    return int(handle)


def is_mutex_held(name: str) -> bool:
    """True while some process holds the named mutex (i.e. that worker is running)."""
    if not IS_WINDOWS:
        if name in _HELD_LOCKS:
            return True
        import fcntl
        p = _lock_path(name)
        if not p.is_file():
            return False
        try:
            fd = os.open(str(p), os.O_RDONLY)
        except OSError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        except OSError:
            return True
        finally:
            os.close(fd)
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(0x00100000, False, f"Local\\{name}")
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def is_admin() -> bool:
    """Return True if the current process has Administrator privileges."""
    if not IS_WINDOWS:
        return os.geteuid() == 0
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def append_log(path: Path, message: str, max_bytes: int = 1_048_576) -> None:
    """Append timestamped log line with simple single-backup rotation."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_file() and path.stat().st_size > max_bytes:
            bak = path.with_suffix(path.suffix + ".1")
            if bak.is_file():
                bak.unlink()
            path.rename(bak)
    except Exception:
        pass
    stamp = time.strftime("%m-%d %H:%M:%S")
    try:
        with path.open("a", encoding="utf-8") as fh:
            fh.write(f"{stamp} {message}\n")
    except Exception:
        pass


def fetch_opencode_aaaa_pool() -> list[str]:
    """Gather live IPv6 addresses for opencode.ai via DoH, system DNS, and fallback."""
    pool: set[str] = set()

    for doh_url in (
        "https://1.1.1.1/dns-query?name=opencode.ai&type=AAAA",
        "https://1.0.0.1/dns-query?name=opencode.ai&type=AAAA",
        "https://dns.google/resolve?name=opencode.ai&type=AAAA",
    ):
        try:
            req = urllib.request.Request(doh_url, headers={"Accept": "application/dns-json"})
            with urllib.request.urlopen(req, timeout=3.5) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="replace"))
            for ans in data.get("Answer", []):
                if ans.get("type") == 28:
                    ip = str(ans.get("data", "")).strip()
                    ipaddress.IPv6Address(ip)
                    pool.add(ip)
        except Exception:
            pass

    import socket

    for host in ("opencode.ai", "api.opencode.ai"):
        try:
            for info in socket.getaddrinfo(host, 443, socket.AF_INET6, socket.SOCK_STREAM):
                ip = info[4][0].split("%", 1)[0]
                ipaddress.IPv6Address(ip)
                pool.add(ip)
        except Exception:
            pass

    pool.update(_FALLBACK_OPENCODE_AAAA)
    return sorted(pool)


def hop_once(warp_bin: str, max_reg_tries: int = 3) -> str:
    """Perform one WARP rotation and return the new external IPv6 ('' when it failed)."""
    new_ip = _hop_once_inner(warp_bin, max_reg_tries)
    if new_ip:
        bump_egress_epoch()
    return new_ip


def _hop_once_inner(warp_bin: str, max_reg_tries: int = 3) -> str:
    # 1. Gentle non-destructive rotation first: rotate cryptographic keys & reset endpoint.
    # This forces Cloudflare Anycast to negotiate a new peer session WITHOUT wiping the account/device registration!
    run_warp(["tunnel", "rotate-keys"], warp_bin=warp_bin)
    run_warp(["tunnel", "endpoint", "reset"], warp_bin=warp_bin)
    run_warp(["disconnect"], warp_bin=warp_bin)
    time.sleep(1.0)
    run_warp(["connect"], warp_bin=warp_bin)

    leaks = 0
    for _ in range(8):
        time.sleep(1.5)
        connected, _, _ = warp_status(warp_bin)
        if connected:
            v6 = get_egress_v6(timeout=5.0)
            if v6:
                return v6
            if warp_path_leaking(max_age=30):
                # "Connected", but traffic leaves with the real IP. Waiting won't fix it: reconnect.
                leaks += 1
                if leaks in (2, 5):
                    run_warp(["disconnect"], warp_bin=warp_bin)
                    time.sleep(1.0)
                    run_warp(["connect"], warp_bin=warp_bin)
                continue
            local_v6 = get_warp_local_v6()
            if local_v6 and is_cloudflare_v6(local_v6):
                return local_v6

    # 2. Only if the client is not connected AND registration is explicitly missing:
    connected, _, raw_status = warp_status(warp_bin)
    raw_lower = (raw_status or "").lower()
    if not connected and ("registration missing" in raw_lower or "not registered" in raw_lower):
        registered = False
        for attempt in range(max_reg_tries):
            rc, _ = run_warp(["registration", "new"], warp_bin=warp_bin)
            if rc == 0:
                registered = True
                break
            time.sleep(4)
        if not registered:
            return ""

    apply_warp_mode(current_warp_scope(), warp_bin=warp_bin)
    run_warp(["connect"], warp_bin=warp_bin)

    # MASQUE in proxy mode can sit in "Connecting" (e.g. "Validating SOCKS proxy configuration") for a while.
    # Giving up then made the next try rotate the keys again and start the handshake over, so the
    # reports showed "tunnel repair failed" several times in a row. Wait longer while it's still connecting.
    started = time.time()
    while True:
        time.sleep(2)
        connected, _, raw = warp_status(warp_bin)
        if connected:
            v6 = get_egress_v6(timeout=5.0)
            if v6:
                return v6
            local_v6 = get_warp_local_v6()
            if local_v6 and is_cloudflare_v6(local_v6):
                return local_v6
        waited = time.time() - started
        still_connecting = "connecting" in (raw or "").lower()
        if waited >= 45 or (waited >= 16 and not still_connecting):
            return ""


# ---------------------------------------------------------------------------
# Language: one setting for the whole app (app_settings.json "language": auto | en | es).
# "auto" = Spanish when Windows is in Spanish (any es-*), English otherwise.
# ---------------------------------------------------------------------------
LANGUAGES = ("auto", "en", "es")
_LANG_CACHE: dict = {"mtime": -1.0, "value": "auto"}


def system_is_spanish() -> bool:
    """True when the Windows display language (LANG/LC_* on Linux) is Spanish."""
    if IS_WINDOWS:
        try:
            return (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x0A
        except Exception:
            return False
    for var in ("LC_ALL", "LC_MESSAGES", "LANG", "LANGUAGE"):
        val = os.environ.get(var, "")
        if val:
            return val.lower().startswith("es")
    return False


def language_setting() -> str:
    """What the user picked: auto | en | es. Re-read whenever app_settings.json changes."""
    path = DEST_DIR / "app_settings.json"
    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = 0.0
    if mtime != _LANG_CACHE["mtime"]:
        value = "auto"
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict) and raw.get("language") in LANGUAGES:
                value = raw["language"]
        except Exception:
            pass
        _LANG_CACHE.update(mtime=mtime, value=value)
    return _LANG_CACHE["value"]


def forget_language() -> None:
    """Read app_settings.json again on the next call (after this process saved a new language)."""
    _LANG_CACHE["mtime"] = -1.0


def ui_lang() -> str:
    """The language openhop shows right now: "es" or "en"."""
    pick = language_setting()
    if pick in ("en", "es"):
        return pick
    return "es" if system_is_spanish() else "en"


def L(en: str, es: str) -> str:
    """Pick the text for the current language (user-visible messages only; logs stay in English)."""
    return es if ui_lang() == "es" else en

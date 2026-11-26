"""macOS counterparts of the Linux plumbing in ouc_linux.py.

macOS has no systemd: background jobs are launchd agents in
~/Library/LaunchAgents, managed with launchctl. There is no iptables:
this module deliberately provides no kill-switch — proxy mode fails
closed instead (see REVERSE_ENGINEERING.md section 4).

State lives under ~/Library/Application Support/openhop, the macOS
equivalent of ~/.local/share/opencode-unlimited on Linux and
%LOCALAPPDATA%\\openhop on Windows.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

HOME = Path.home()
APP_SUPPORT = HOME / "Library" / "Application Support"
DEST = APP_SUPPORT / "openhop"
STATE_DIR = DEST
GUARD_DIR = DEST / "guard"
LOG_DIR = HOME / "Library" / "Logs" / "openhop"

# OpenCode on macOS: Desktop app bundle, CLI, and per-user data.
OPENCODE_APP_PATHS = (
    Path("/Applications/OpenCode.app"),
    HOME / "Applications" / "OpenCode.app",
)
OPENCODE_BINARY_PATHS = (
    Path("/Applications/OpenCode.app/Contents/MacOS/OpenCode"),
    HOME / "Applications" / "OpenCode.app" / "Contents" / "MacOS" / "OpenCode",
    HOME / ".opencode" / "bin" / "opencode",
)
OPENCODE_ASAR_PATHS = (
    Path("/Applications/OpenCode.app/Contents/Resources/app.asar"),
    HOME / "Applications" / "OpenCode.app" / "Contents" / "Resources" / "app.asar",
)
# Electron app data on macOS. The Linux path
# ~/.config/ai.opencode.desktop/drafts.sqlite is kept as a fallback
# by callers; this is the first candidate on a Mac.
OPENCODE_DRAFTS_SQLITE_PATHS = (
    APP_SUPPORT / "ai.opencode.desktop" / "drafts.sqlite",
    HOME / ".config" / "ai.opencode.desktop" / "drafts.sqlite",
    HOME / ".local" / "share" / "opencode" / "drafts.sqlite",
)
OPENCODE_LOG_PATHS = (
    APP_SUPPORT / "opencode" / "log" / "opencode.log",
    HOME / ".local" / "share" / "opencode" / "log" / "opencode.log",
)

WARP_APP = Path("/Applications/Cloudflare WARP.app")
WARP_DAEMON = WARP_APP / "Contents" / "Resources" / "CloudflareWARP"


def find_warp_cli() -> str | None:
    """Return the warp-cli path on macOS, or None if WARP is missing.

    Cloudflare installs warp-cli on PATH with the macOS app; fall back
    to the app bundle's Resources folder.
    """
    env = os.environ.get("OUC_WARP_BIN")
    if env and Path(env).exists():
        return env
    found = shutil.which("warp-cli")
    if found:
        return found
    for candidate in (
        WARP_APP / "Contents" / "Resources" / "warp-cli",
        Path("/opt/homebrew/bin/warp-cli"),
        Path("/usr/local/bin/warp-cli"),
    ):
        if candidate.exists():
            return str(candidate)
    return None


def find_opencode_binary() -> str | None:
    for candidate in OPENCODE_BINARY_PATHS:
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    found = shutil.which("opencode")
    return found


def _launchctl(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["launchctl", *args],
            capture_output=True, text=True, errors="replace", timeout=timeout,
        )
    except Exception as exc:
        return subprocess.CompletedProcess(
            args=["launchctl", *args], returncode=1, stdout="", stderr=str(exc),
        )


def launchd_available() -> bool:
    return shutil.which("launchctl") is not None


def job_loaded(label: str) -> bool:
    """True if a launchd job with this label is loaded for this user."""
    res = _launchctl("list", label)
    return res.returncode == 0


def job_start(label: str) -> bool:
    plist = HOME / "Library" / "LaunchAgents" / f"{label}.plist"
    if not plist.exists():
        return False
    uid = str(os.getuid())
    res = _launchctl("bootstrap", f"gui/{uid}", str(plist))
    return res.returncode == 0 or job_loaded(label)


def job_stop(label: str) -> bool:
    uid = str(os.getuid())
    res = _launchctl("bootout", f"gui/{uid}/{label}")
    return res.returncode == 0 or not job_loaded(label)


# --- kill-switch stub -------------------------------------------------------
# macOS has no iptables. There is intentionally no pf-based kill-switch
# in this port: in proxy mode OpenCode cannot connect at all if the
# proxy is down, so traffic fails closed instead of leaking.
# These stubs exist so code importing ouc_firewall-style helpers does
# not crash on macOS; they always report "not installed, by design".

def is_killswitch_installed() -> bool:
    return False


def killswitch_status() -> str:
    return (
        "not installed by design on macOS (proxy mode fails closed; "
        "no pf kill-switch is installed by this port)"
    )

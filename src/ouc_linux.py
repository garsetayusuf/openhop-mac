"""MACOS PORT: this file is Linux-only (systemd). macOS uses ouc_macos.py."""
"""Linux counterparts of the Windows plumbing: systemd user services instead of Scheduled Tasks, a PATH line in
the shell start-up files instead of the user PATH in the registry, .desktop files instead of the Run key and the
Start menu. Every function is safe to call repeatedly and never needs root.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from ouc_common import BIN_DIR, DEST_DIR, SCRIPTS_DIR, append_log

CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))
DATA_HOME = Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share"))
UNIT_DIR = CONFIG_HOME / "systemd" / "user"
UNITS = {"keeper": "openhop-keeper.service", "guard": "openhop-guard.service"}
_WORKER_SCRIPT = {"keeper": "warp_keeper.py", "guard": "zen_guard.py"}
APP_DIR = DEST_DIR / "app"            # the installed app (install.sh unpacks it here)
VENV_PY = DEST_DIR / "venv" / "bin" / "python"
DESKTOP_FILE = DATA_HOME / "applications" / "openhop.desktop"
AUTOSTART_FILE = CONFIG_HOME / "autostart" / "openhop.desktop"
ICON_FILE = DATA_HOME / "icons" / "hicolor" / "256x256" / "apps" / "openhop.png"
LOG = DEST_DIR / "logs" / "linux.log"

_PATH_BEGIN = "# >>> openhop >>>"
_PATH_END = "# <<< openhop <<<"


def _log(msg: str) -> None:
    append_log(LOG, msg)


def python_exe() -> str:
    """The Python that runs openhop: the installer's venv if present, else the one running now."""
    return str(VENV_PY) if VENV_PY.is_file() else sys.executable


# ------------------------------------------------------------------------------------------- systemd --user
def _systemctl(*args: str, timeout: float = 15.0) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, errors="replace",
                              timeout=timeout, stdin=subprocess.DEVNULL)
    except Exception as exc:  # no systemd, or no user session bus
        return subprocess.CompletedProcess(args, 127, "", str(exc))


def systemd_available() -> bool:
    return shutil.which("systemctl") is not None and _systemctl("is-system-running", timeout=5.0).returncode in (0, 1)


def unit_text(name: str) -> str:
    script = SCRIPTS_DIR / _WORKER_SCRIPT[name]
    desc = "keeps WARP connected and runs the local proxy for OpenCode" if name == "keeper" \
        else "watches for the free limit, gets a new IP and resumes the chat"
    return (
        "[Unit]\n"
        f"Description=openhop {name}: {desc} (openhop.cfd)\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={shlex.quote(python_exe())} {shlex.quote(str(script))}\n"
        f"WorkingDirectory={shlex.quote(str(SCRIPTS_DIR))}\n"
        "Environment=PYTHONUNBUFFERED=1\n"
        # exit 0 = another copy already runs (it holds the lock): don't loop. Crashes restart.
        "Restart=on-failure\n"
        "RestartSec=10\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def write_units() -> bool:
    try:
        UNIT_DIR.mkdir(parents=True, exist_ok=True)
        changed = False
        for name, unit in UNITS.items():
            p = UNIT_DIR / unit
            text = unit_text(name)
            if not p.is_file() or p.read_text(encoding="utf-8") != text:
                p.write_text(text, encoding="utf-8")
                changed = True
        if changed:
            _systemctl("daemon-reload")
        return True
    except Exception as exc:
        _log(f"writing systemd units failed: {exc}")
        return False


def unit_exists(name: str) -> bool:
    return (UNIT_DIR / UNITS[name]).is_file()


def unit_enabled(name: str) -> bool:
    return unit_exists(name) and _systemctl("is-enabled", UNITS[name], timeout=5.0).stdout.strip() == "enabled"


def unit_active(name: str) -> bool:
    return unit_exists(name) and _systemctl("is-active", UNITS[name], timeout=5.0).stdout.strip() in ("active", "activating")


def start_unit(name: str) -> bool:
    if not unit_exists(name):
        return False
    return _systemctl("start", UNITS[name]).returncode == 0


def stop_unit(name: str) -> None:
    if unit_exists(name):
        _systemctl("stop", UNITS[name])


def set_units_enabled(enable: bool) -> tuple[bool, str]:
    """'Protect from sign-in': start keeper + guard with the user's session (and keep them running)."""
    if enable:
        if not write_units():
            return False, "Couldn't write the background service files."
        res = _systemctl("enable", *UNITS.values())
        if res.returncode != 0:
            return False, "systemd refused to turn on the background services: " + (res.stderr or "").strip()[:200]
        return True, "Protection now starts by itself every time you sign in."
    if any(unit_exists(n) for n in UNITS):
        _systemctl("disable", *[u for n, u in UNITS.items() if unit_exists(n)])
    return True, "Protection no longer starts by itself when you sign in."


def remove_units() -> None:
    names = [u for n, u in UNITS.items() if unit_exists(n)]
    if names:
        _systemctl("disable", "--now", *names)
    for u in UNITS.values():
        (UNIT_DIR / u).unlink(missing_ok=True)
    _systemctl("daemon-reload")


# ------------------------------------------------------------------------------------------- PATH
def _rc_files() -> list[Path]:
    home = Path.home()
    files = [home / ".profile", home / ".bashrc"]
    if (home / ".zshrc").is_file() or os.environ.get("SHELL", "").endswith("zsh"):
        files.append(home / ".zshrc")
    return files


def _fish_file() -> Path:
    return CONFIG_HOME / "fish" / "conf.d" / "openhop.fish"


def _path_block() -> str:
    return (f"{_PATH_BEGIN}\n"
            "# openhop: puts its 'opencode' (which goes through openhop) before the real one. openhop.cfd\n"
            f'case ":$PATH:" in *":{BIN_DIR}:"*) ;; *) export PATH="{BIN_DIR}:$PATH" ;; esac\n'
            f"{_PATH_END}\n")


def _strip_block(text: str) -> str:
    out, skip = [], False
    for line in text.splitlines(keepends=True):
        if line.strip() == _PATH_BEGIN:
            skip = True
            continue
        if skip and line.strip() == _PATH_END:
            skip = False
            continue
        if not skip:
            out.append(line)
    return "".join(out)


def update_shell_path(add: bool) -> bool:
    ok = True
    for f in _rc_files():
        try:
            old = f.read_text(encoding="utf-8") if f.is_file() else ""
            new = _strip_block(old)
            if add:
                new = (new.rstrip("\n") + "\n\n" if new.strip() else "") + _path_block()
            if new != old and (add or f.is_file()):
                f.write_text(new, encoding="utf-8")
        except Exception as exc:
            ok = False
            _log(f"PATH update in {f} failed: {exc}")
    fish = _fish_file()
    try:
        if add and fish.parent.parent.is_dir():
            fish.parent.mkdir(parents=True, exist_ok=True)
            fish.write_text(f"# openhop (openhop.cfd)\nfish_add_path --prepend --path {BIN_DIR}\n", encoding="utf-8")
        elif not add:
            fish.unlink(missing_ok=True)
    except Exception:
        pass
    return ok


def shell_path_has_bin() -> bool:
    for f in _rc_files():
        try:
            if _PATH_BEGIN in f.read_text(encoding="utf-8"):
                return True
        except Exception:
            pass
    return False


# ------------------------------------------------------------------------------------------- shims
def write_shims() -> None:
    """Small shell scripts in ~/.local/share/openhop/bin: 'opencode' goes through openhop, plus the openhop tools."""
    py = shlex.quote(python_exe())
    s = lambda f: shlex.quote(str(SCRIPTS_DIR / f))  # noqa: E731
    app = shlex.quote(str(APP_DIR / "openhop_app.py")) if (APP_DIR / "openhop_app.py").is_file() \
        else shlex.quote(str(Path(__file__).resolve().parent / "openhop_app.py"))
    shims = {
        "opencode": f"exec {py} {s('zen_lane.py')} \"$@\"",
        "opencode-desktop": f"exec {py} {s('zen_lane.py')} --ouc-desktop \"$@\"",
        "openhop": f"exec {py} {app} \"$@\"",
        "openhop-hop": f"exec {py} {s('zen_hop.py')} \"$@\"",
        "openhop-doctor": f"exec {py} {s('doctor.py')} \"$@\"",
        "openhop-keeper": f"exec {py} {s('warp_keeper.py')} \"$@\"",
        "openhop-guard": f"exec {py} {s('zen_guard.py')} \"$@\"",
        "openhop-uninstall": f"exec {py} {app} --uninstall \"$@\"",
    }
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    for name, line in shims.items():
        p = BIN_DIR / name
        p.write_text(f"#!/bin/sh\n# openhop (openhop.cfd)\n{line}\n", encoding="utf-8")
        p.chmod(0o755)


# ------------------------------------------------------------------------------------------- menu entry / autostart
def _desktop_entry(hidden_start: bool) -> str:
    exec_line = f"{BIN_DIR / 'openhop'}" + (" --tray" if hidden_start else "")
    lines = [
        "[Desktop Entry]",
        "Type=Application",
        "Name=openhop",
        "GenericName=OpenCode helper",
        "Comment=Keeps OpenCode working on the free plan · openhop.cfd",
        f"Exec={exec_line}",
        f"Icon={ICON_FILE if ICON_FILE.is_file() else 'openhop'}",
        "Terminal=false",
        "Categories=Network;Development;Utility;",
        "StartupNotify=false",
        "StartupWMClass=openhop",
    ]
    if hidden_start:
        lines += ["X-GNOME-Autostart-enabled=true", "NoDisplay=true"]
    return "\n".join(lines) + "\n"


def write_menu_entry() -> None:
    try:
        DESKTOP_FILE.parent.mkdir(parents=True, exist_ok=True)
        DESKTOP_FILE.write_text(_desktop_entry(False), encoding="utf-8")
        if shutil.which("update-desktop-database"):
            subprocess.run(["update-desktop-database", str(DESKTOP_FILE.parent)], capture_output=True, timeout=10)
    except Exception as exc:
        _log(f"menu entry failed: {exc}")


def autostart_enabled() -> bool:
    return AUTOSTART_FILE.is_file()


def set_autostart(enable: bool) -> bool:
    try:
        if enable:
            AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
            AUTOSTART_FILE.write_text(_desktop_entry(True), encoding="utf-8")
        else:
            AUTOSTART_FILE.unlink(missing_ok=True)
        return True
    except Exception as exc:
        _log(f"autostart change failed: {exc}")
        return False


# ------------------------------------------------------------------------------------------- desktop helpers
_TERMINALS = (
    ("x-terminal-emulator", ["-e"]),
    ("gnome-terminal", ["--"]),
    ("konsole", ["-e"]),
    ("xfce4-terminal", ["-x"]),
    ("kgx", ["--"]),
    ("tilix", ["-e"]),
    ("mate-terminal", ["-x"]),
    ("lxterminal", ["-e"]),
    ("kitty", []),
    ("alacritty", ["-e"]),
    ("wezterm", ["start", "--"]),
    ("foot", []),
    ("xterm", ["-e"]),
)


def open_terminal(argv: list[str], title: str = "OpenCode (openhop)") -> bool:
    """Open the user's terminal running argv. False if no terminal app was found."""
    for term, flag in _TERMINALS:
        path = shutil.which(term)
        if not path:
            continue
        try:
            subprocess.Popen([path, *flag, *argv], start_new_session=True, close_fds=True,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def open_path(path: str) -> bool:
    for opener in ("xdg-open", "gio", "kde-open"):
        exe = shutil.which(opener)
        if not exe:
            continue
        args = [exe, "open", path] if opener == "gio" else [exe, path]
        try:
            subprocess.Popen(args, start_new_session=True, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


def notify_send(title: str, body: str, urgent: bool = False) -> bool:
    exe = shutil.which("notify-send")
    if not exe:
        return False
    icon = str(ICON_FILE) if ICON_FILE.is_file() else "dialog-information"
    try:
        subprocess.Popen([exe, "-a", "openhop", "-i", icon, "-u", "critical" if urgent else "normal", title, body],
                         start_new_session=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False

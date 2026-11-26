"""OpenHop Control Center — macOS entry point (pywebview 5, Cocoa/WKWebView).

A thin native shell around the existing macOS backend. It never reimplements
that backend: launchd jobs com.openhop.keeper / com.openhop.guard, the
warp-cli proxy on 127.0.0.1:40000, and the CLI shims in ~/.local/bin
(opencode, openhop-hop, openhop-doctor) are only invoked here.
"""
from __future__ import annotations

import ipaddress
import os
import subprocess
import sys
import time
from pathlib import Path

import webview

HOME = Path.home()
BIN = HOME / ".local" / "bin"
DOCTOR = BIN / "openhop-doctor"
HOP = BIN / "openhop-hop"
LANE = BIN / "opencode"
LOG_DIR = HOME / "Library" / "Application Support" / "openhop" / "logs"
STARTUP_LOG = LOG_DIR / "control-center.log"
LAUNCH_AGENTS = HOME / "Library" / "LaunchAgents"
TITLE = "OpenHop Control Center"

_CRITICAL = (
    "not connected", "warp not connected", "no external egress", "offline",
    "egress check failed", "tunnel down", "proxy down", "connection failed",
    "warp tunnel: disconnected",
)
_TUNNEL_HINTS = ("warp tunnel", "warp verified", "socks5", "proxy", "egress")


def _log(message: str) -> None:
    """Best-effort startup log, capped at 256 KB."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        if STARTUP_LOG.is_file() and STARTUP_LOG.stat().st_size > 256 * 1024:
            STARTUP_LOG.write_text("", encoding="utf-8")
        with STARTUP_LOG.open("a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%m-%d %H:%M:%S')} [{os.getpid()}] {message}\n")
    except Exception:
        pass


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value)


def _clip(value, limit: int = 2000) -> str:
    text = _text(value).strip()
    return text if len(text) <= limit else text[:limit] + " ..."


def _mentions(line: str, needles) -> bool:
    low = line.lower()
    return any(needle in low for needle in needles)


def _extract_ip(line: str) -> str:
    """Pull the address out of 'egress is cloudflare (warp verified): <ip>'."""
    if "verified):" in line:
        tail = line.split("verified):", 1)[1]
    else:
        tail = line.rsplit(":", 1)[-1]
    token = tail.strip().split()[0].split("/")[0] if tail.strip() else ""
    try:
        ipaddress.ip_address(token.split("%")[0])
    except ValueError:
        return ""
    return token


def _ui_dirs() -> list[Path]:
    """HTML locations for a source run and for the frozen PyInstaller bundle."""
    dirs: list[Path] = []
    if getattr(sys, "frozen", False):
        meipass = getattr(sys, "_MEIPASS", None)
        if meipass:
            dirs.append(Path(meipass) / "ui")
        exe = Path(sys.executable)
        dirs.extend((exe.parent / "Resources" / "ui", exe.parent / "ui"))
    dirs.append(Path(__file__).resolve().parent / "ui")
    return dirs


def _load_html() -> str:
    tried: list[str] = []
    for folder in _ui_dirs():
        candidate = folder / "control_center.html"
        tried.append(str(candidate))
        try:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")
        except Exception as exc:
            _log(f"could not read {candidate}: {exc!r}")
    _log("control_center.html not found at: " + ", ".join(tried))
    return (
        "<!doctype html><meta charset='utf-8'><title>OpenHop</title>"
        "<body style='background:#0a0d13;color:#f0efe8;font-family:system-ui;padding:40px'>"
        "<h1>OpenHop Control Center</h1>"
        "<p>The bundled ui/control_center.html could not be found.</p></body>"
    )


class ControlApi:
    """pywebview 5 exposes these public methods to JS as Promises of plain dicts."""

    def __init__(self) -> None:
        self._env = self._make_env()

    @staticmethod
    def _make_env() -> dict[str, str]:
        try:
            env = os.environ.copy()
            bindir = str(BIN)
            parts = [p for p in env.get("PATH", "").split(os.pathsep) if p and p != bindir]
            env["PATH"] = os.pathsep.join([bindir, *parts])
            return env
        except Exception:
            return dict(os.environ)

    def _run(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        """Run an argument list with a timeout; always return, never raise into JS."""
        try:
            return subprocess.run(
                argv, capture_output=True, text=True, errors="replace",
                timeout=timeout, env=self._env,
            )
        except subprocess.TimeoutExpired as exc:
            return subprocess.CompletedProcess(
                argv, 124, _text(exc.stdout),
                _text(exc.stderr) + f"\ntimed out after {timeout:g}s",
            )
        except Exception as exc:
            return subprocess.CompletedProcess(argv, 127, "", f"{exc!r}")

    @staticmethod
    def _doctor_view(proc) -> dict:
        out = _text(proc.stdout)
        lines = [ln.strip() for ln in out.splitlines()]
        good = [ln[1:].strip() for ln in lines if ln.startswith("\u2713")]
        bad = [ln[1:].strip() for ln in lines if ln.startswith("\u2717")]

        ip = ""
        for line in good:
            if "egress" in line.lower():
                ip = _extract_ip(line)
                if ip:
                    break
        tunnel = any(_mentions(line, _TUNNEL_HINTS) for line in good)
        critical = any(_mentions(line, _CRITICAL) for line in bad)

        if ip and not bad:
            state, label = "ok", "Everything is working"
        elif critical or not (ip or tunnel):
            state, label = "error", "Not connected"
        else:
            state, label = "warn", "Degraded"
        return {
            "state": state,
            "label": label,
            "ip": ip,
            "connection": "WARP proxy \u00b7 Cloudflare" if ip else "Disconnected",
            "detail": _clip(out.strip() or _text(proc.stderr).strip() or "Doctor returned no output."),
        }

    def _doctor(self, timeout: float = 60):
        return self._run([str(DOCTOR)], timeout)

    def status(self) -> dict:
        try:
            return self._doctor_view(self._doctor())
        except Exception as exc:
            return {
                "state": "error", "label": "Not connected", "ip": "",
                "connection": "Disconnected", "detail": _clip(f"Status check failed: {exc!r}"),
            }

    def launch(self) -> dict:
        fh = None
        sink = subprocess.DEVNULL
        try:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            fh = (LOG_DIR / "lane.log").open("a", encoding="utf-8", buffering=1)
            sink = fh
        except Exception:
            pass
        try:
            subprocess.Popen(
                [str(LANE)], stdin=subprocess.DEVNULL, stdout=sink,
                stderr=subprocess.STDOUT, start_new_session=True,
                close_fds=True, env=self._env,
            )
            result = {"ok": True, "message": "OpenCode launched \u2014 your chat is ready where you left it."}
        except Exception as exc:
            result = {"ok": False, "message": _clip(f"Could not launch OpenCode: {exc}")}
        finally:
            if fh is not None:
                fh.close()
        return result

    def hop(self) -> dict:
        proc = self._run([str(HOP)], 120)
        combined = _text(proc.stdout)
        if proc.stderr:
            combined = (combined + "\n" + _text(proc.stderr)).strip()
        ok = proc.returncode == 0
        fallback = "Hop finished." if ok else f"Hop failed (exit {proc.returncode})."
        return {"ok": ok, "message": _clip(combined or fallback)}

    def fix(self) -> dict:
        notes: list[str] = []
        try:
            before = self._doctor()
            notes.append("[doctor before]")
            notes.append(_clip(_text(before.stdout) or _text(before.stderr), 1200))

            try:
                uid = str(os.getuid())
            except Exception:
                uid = "501"
            for job in ("keeper", "guard"):
                label = f"com.openhop.{job}"
                probe = self._run(["launchctl", "print", f"gui/{uid}/{label}"], 15)
                if probe.returncode == 0:
                    notes.append(f"\u2713 {label}: loaded")
                    continue
                plist = LAUNCH_AGENTS / f"{label}.plist"
                if not plist.is_file():
                    notes.append(f"\u2717 {label}: not loaded and {plist.name} is missing")
                    continue
                boot = self._run(["launchctl", "bootstrap", f"gui/{uid}", str(plist)], 15)
                notes.append(
                    f"\u2713 {label}: bootstrapped" if boot.returncode == 0
                    else f"\u2717 {label}: bootstrap failed - {_clip(boot.stderr, 300)}"
                )

            after = self._doctor()
            notes.append("[doctor after]")
            notes.append(_clip(_text(after.stdout) or _text(after.stderr), 1200))
            ok = self._doctor_view(after)["state"] != "error"
        except Exception as exc:
            notes.append(f"Repair failed: {exc!r}")
            ok = False
        return {"ok": ok, "message": _clip("\n".join(notes))}

    def quit(self) -> dict:
        try:
            webview.destroy_window()
        except Exception:
            pass
        return {"ok": True, "message": " quitting"}


def main() -> int:
    _log(f"starting ({sys.executable})")
    html = _load_html()
    api = ControlApi()
    try:
        webview.create_window(
            TITLE, html=html, js_api=api, width=1000, height=760,
            min_size=(880, 640), background_color="#0a0d13", text_select=True,
        )
        _log("window created; starting webview (Cocoa/WKWebView auto-selected)")
        webview.start()
    except Exception as exc:
        _log(f"webview failed: {exc!r}")
        return 1
    _log("webview stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

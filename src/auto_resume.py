"""Auto-Resume module for openhop.

Automatically detects when a rate limit is bypassed, clears client-side database
lockouts (free-limit / go-upsell), reloads the OpenCode interface, and dispatches
a 'continue' action to the active OpenCode session so the model keeps generating autonomously.
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

_SRC = Path(__file__).resolve().parent
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ouc_common import GUARD_DIR, LANE_DIR, SERVICE_PROXY_PORT, _is_desktop_exe, find_opencode_cli, opencode_db_path

AUTO_RESUME_FLAG = LANE_DIR / "auto_resume.flag"


def is_auto_resume_enabled() -> bool:
    """Return True if auto-resume is enabled (default is enabled)."""
    if AUTO_RESUME_FLAG.is_file():
        try:
            return AUTO_RESUME_FLAG.read_text(encoding="utf-8").strip() != "0"
        except Exception:
            return True
    return True


def set_auto_resume(enable: bool) -> None:
    """Enable or disable the auto-resume feature."""
    LANE_DIR.mkdir(parents=True, exist_ok=True)
    AUTO_RESUME_FLAG.write_text("1\n" if enable else "0\n", encoding="utf-8")


def clear_opencode_free_limit() -> int:
    """Delete client-side free-limit and go-upsell lockout rows from drafts.sqlite.

    OpenCode Desktop stores rate limit timestamps in drafts.sqlite (table 'state').
    Even after IP rotation, the UI blocks user prompts until this timestamp is purged.
    """
    candidates = [
        Path.home() / "Library" / "Application Support" / "ai.opencode.desktop" / "drafts.sqlite",  # MACOS PORT
        Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming")) / "ai.opencode.desktop" / "drafts.sqlite",
        Path.home() / ".config" / "ai.opencode.desktop" / "drafts.sqlite",
        Path.home() / ".local" / "share" / "opencode" / "drafts.sqlite",
    ]
    deleted = 0
    for db_path in candidates:
        if not db_path.is_file():
            continue
        try:
            conn = sqlite3.connect(str(db_path), timeout=3.0)
            cur = conn.cursor()
            cur.execute("DELETE FROM state WHERE key IN ('free-limit', 'go-upsell') OR key LIKE '%free%limit%' OR key LIKE '%quota%exceeded%' OR key LIKE '%upsell%'")
            deleted += cur.rowcount
            conn.commit()
            conn.close()
        except Exception:
            pass
    return deleted


def get_latest_session_info() -> tuple[str, str, str] | None:
    """Query opencode.db for the most recently active non-archived session.

    Returns (session_id, directory, title) or None.
    """
    db_path = opencode_db_path()
    if not db_path.is_file():
        return None
    try:
        conn = sqlite3.connect(str(db_path), timeout=3.0)
        cur = conn.cursor()
        cur.execute(
            "SELECT id, directory, title FROM session_v2 "
            "WHERE time_archived IS NULL "
            "ORDER BY time_updated DESC LIMIT 1"
        )
        row = cur.fetchone()
        conn.close()
        if row and row[0]:
            return str(row[0]), str(row[1] or ""), str(row[2] or "")
    except Exception:
        pass
    return None


def send_continue_keystroke(reload_first: bool = True) -> bool:
    """Send reload (^r) and 'continue{ENTER}' to active OpenCode Desktop or Terminal window."""
    if sys.platform != "win32":
        try:
            import shutil
            if shutil.which("xdotool"):
                if reload_first:
                    subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "opencode", "key", "--clearmodifiers", "ctrl+r"], capture_output=True, timeout=2.0)
                    time.sleep(1.2)
                subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "opencode", "type", "--clearmodifiers", "continue"], capture_output=True, timeout=2.0)
                subprocess.run(["xdotool", "search", "--onlyvisible", "--class", "opencode", "key", "--clearmodifiers", "Return"], capture_output=True, timeout=2.0)
                return True
        except Exception:
            pass
        return False

    # 1. Native Windows Win32 API window focus and key dispatch
    try:
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        target_hwnds = []

        def _enum_cb(hwnd, _):
            if user32.IsWindowVisible(hwnd):
                length = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value.lower()
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pname = ""
                try:
                    import psutil
                    pname = psutil.Process(pid.value).name().lower()
                except Exception:
                    pass
                if "opencode" in pname or "opencode" in title:
                    target_hwnds.append((hwnd, buf.value, pname))
            return True

        CB_TYPE = ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
        user32.EnumWindows(CB_TYPE(_enum_cb), 0)

        for hwnd, title, pname in target_hwnds:
            # SW_RESTORE = 9
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.3)
            # If requested, send Ctrl+R to reload UI (reloading drops the free-limit block screen)
            if reload_first and ("desktop" in pname or "opencode.exe" in pname):
                ps_reload = """
                Add-Type -AssemblyName System.Windows.Forms
                [System.Windows.Forms.SendKeys]::SendWait("^r")
                """
                subprocess.run(
                    ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_reload],
                    capture_output=True,
                    creationflags=0x08000000,
                    timeout=3.0,
                )
                time.sleep(1.2)

            ps_send = """
            Add-Type -AssemblyName System.Windows.Forms
            [System.Windows.Forms.SendKeys]::SendWait("continue{ENTER}")
            """
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_send],
                capture_output=True,
                creationflags=0x08000000,
                timeout=4.0,
            )
            return True
    except Exception:
        pass

    # 2. PowerShell WScript.Shell AppActivate fallback
    ps = """
    $ws = New-Object -ComObject WScript.Shell
    $targets = @("OpenCode", "opencode", "ai.opencode")
    $sent = $false
    foreach ($t in $targets) {
        if ($ws.AppActivate($t)) {
            Start-Sleep -Milliseconds 300
            $ws.SendKeys("^r")
            Start-Sleep -Milliseconds 1200
            $ws.SendKeys("continue{ENTER}")
            $sent = $true
            break
        }
    }
    if ($sent) { Write-Output "SENT" } else { Write-Output "NOT_FOUND" }
    """
    try:
        CREATE_NO_WINDOW = 0x08000000
        res = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
            capture_output=True,
            text=True,
            errors="replace",
            timeout=5.0,
            creationflags=CREATE_NO_WINDOW,
        )
        return "SENT" in res.stdout
    except Exception:
        return False


_SESSION_ID_RE = re.compile(r"\bsessionID=(ses_[A-Za-z0-9]+)")


def session_id_in(line: str) -> str:
    """The OpenCode session a log line belongs to ('' if none). Wall errors carry 'sessionID=ses_…'."""
    m = _SESSION_ID_RE.search(line or "")
    return m.group(1) if m else ""


def _session_directory(session_id: str) -> str:
    db_path = opencode_db_path()
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=3.0)
        try:
            row = conn.execute("SELECT directory FROM session_v2 WHERE id = ?", (session_id,)).fetchone()
        finally:
            conn.close()
        return str(row[0] or "") if row else ""
    except Exception:
        return ""


# A 'continue' process older than this has long finished its job: 'opencode run' stays open after the chat
# goes idle (seen: 9 hours), so without this every chat that ever hit the limit keeps one CLI process alive.
_CONTINUE_MAX_AGE = 30 * 60


def _stop_stale_continue(oc: str, session_id: str = "") -> None:
    """End our earlier 'continue' processes: the one for this session (it would pile up) and any old one."""
    try:
        import psutil
        exe_name = Path(oc).name.lower()
        now = time.time()
        for p in psutil.process_iter(["name", "cmdline", "create_time"]):
            try:
                cmd = p.info.get("cmdline") or []
                ours = cmd[-1:] == ["continue"] or (cmd[-1:] and (cmd[-1].startswith("openhop: the previous chat") or "the work continues in the chat" in cmd[-1].lower()))
                if (p.info.get("name") or "").lower() != exe_name or "run" not in cmd or not ours:
                    continue
                old = now - float(p.info.get("create_time") or now) > _CONTINUE_MAX_AGE
                if old or (session_id and session_id in cmd):
                    p.kill()
            except Exception:
                pass
    except Exception:
        pass


def _run_env() -> dict:
    """Environment for 'opencode run': always through openhop's proxy.

    Normally 'opencode run' hands the message to OpenCode's background service, which already uses the
    proxy. If it runs on its own instead (older OpenCode, or the service isn't up), it must not go out
    with the user's own IP: that is the IP that just hit the limit.
    """
    env = os.environ.copy()
    proxy_url = f"http://127.0.0.1:{SERVICE_PROXY_PORT}"
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env[k] = proxy_url
    for k in ("NO_PROXY", "no_proxy"):
        env[k] = "127.0.0.1,localhost,::1"
    return env


# ---------------------------------------------------------------- did the 'continue' really arrive?
# Launching 'opencode run' isn't the same as the chat getting the message: the CLI can fail (old CLI, a
# different OpenCode install, the session isn't in its database, …) and before 3.7.2 openhop still said
# "sent 'continue'" and "Fixed: the chat continued by itself". Now each launch writes its output to
# guard\continue\<session>.log, and a background check looks for the 'continue' in OpenCode's database.

CONTINUE_DIR = GUARD_DIR / "continue"
RESUME_RESULT_FILE = GUARD_DIR / "resume_result.json"
VERIFY_SECONDS = 45          # how long OpenCode gets to store the 'continue'
RESULT_KEEP_SECONDS = 30 * 60
_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")
_latest_job: dict[str, float] = {}   # sessionID -> since_ms of the newest 'continue' launched for it


def _guard_log(msg: str) -> None:
    try:
        from ouc_common import GUARD_LOG_FILE, append_log
        append_log(GUARD_LOG_FILE, msg)
    except Exception:
        pass


def continue_arrived(session_id: str, since_ms: float) -> bool | None:
    """True when the chat got a new user message after since_ms, False when it didn't, None when we can't tell
    (no database, or this chat isn't in it: then openhop isn't reading the database OpenCode really uses)."""
    db = opencode_db_path()
    if not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        try:
            if not con.execute("SELECT 1 FROM session_v2 WHERE id = ?", (session_id,)).fetchone():
                return None
            row = con.execute(
                "SELECT 1 FROM session_message WHERE session_id = ? AND type = 'user' AND time_created >= ? LIMIT 1",
                (session_id, int(since_ms))).fetchone()
        finally:
            con.close()
        return bool(row)
    except Exception:
        return None


def _log_reason(path: Path) -> str:
    """The last meaningful line(s) the 'opencode run' process printed, for the guard log and the report."""
    try:
        text = path.read_bytes()[-4000:].decode("utf-8", errors="replace")
    except Exception:
        return ""
    lines = [_ANSI_RE.sub("", ln).strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return " | ".join(lines[-3:])[:300]


def _load_results() -> dict:
    try:
        import json
        data = json.loads(RESUME_RESULT_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_result(sid: str, ok: bool | None, reason: str = "") -> None:
    import json
    now = time.time()
    data = {k: v for k, v in _load_results().items()
            if isinstance(v, dict) and now - float(v.get("at", 0) or 0) < RESULT_KEEP_SECONDS}
    data[sid] = {"at": now, "ok": ok, "reason": reason}
    try:
        RESUME_RESULT_FILE.parent.mkdir(parents=True, exist_ok=True)
        RESUME_RESULT_FILE.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass


def failed_resumes(max_age: float = 15 * 60) -> dict[str, dict]:
    """Chats whose last 'continue' from openhop didn't arrive, and that nobody has written to since.

    {sessionID: {"at": time, "reason": text}}. A chat drops out as soon as it gets any new user message
    (the user typed 'continue' themselves, or a later 'continue' from openhop worked).
    """
    now = time.time()
    out = {}
    for sid, v in _load_results().items():
        if not isinstance(v, dict) or v.get("ok") is not False:
            continue
        at = float(v.get("at", 0) or 0)
        if now - at > max_age:
            continue
        if continue_arrived(sid, at * 1000) is True:
            continue
        out[sid] = {"at": at, "reason": str(v.get("reason") or "")}
    return out


def _verify(jobs: list[tuple[str, subprocess.Popen, float, Path, bool]]) -> None:
    """Background check: did each 'continue' land in its chat? Logs the answer; retries a failed one once."""
    left = {sid: job for sid, *job in jobs}
    deadline = time.time() + VERIFY_SECONDS
    while left and time.time() < deadline:
        time.sleep(2.0)
        for sid in list(left):
            proc, since_ms, log_path, retried = left[sid]
            if _latest_job.get(sid, since_ms) > since_ms:
                left.pop(sid)   # a newer 'continue' replaced this one (and stopped its process): its own check answers
                continue
            got = continue_arrived(sid, since_ms)
            if got is True:
                _guard_log(f"[auto-resume] 'continue' arrived in chat {sid[:12]}… ✓")
                _save_result(sid, True)
                left.pop(sid)
            elif got is None:
                _guard_log(f"[auto-resume] couldn't check whether 'continue' arrived in chat {sid[:12]}… "
                           f"(that chat isn't in {opencode_db_path().name})")
                _save_result(sid, None)
                left.pop(sid)
            elif proc.poll() is not None and time.time() - since_ms / 1000 > 6:
                # The CLI is done and the chat still has nothing new: it failed (wait a moment for the db write).
                time.sleep(2.0)
                if continue_arrived(sid, since_ms) is True:
                    continue
                left.pop(sid)
                _failed(sid, proc, log_path, retried)
    for sid, (proc, since_ms, log_path, retried) in left.items():
        if _latest_job.get(sid, since_ms) <= since_ms:
            _failed(sid, proc, log_path, retried, timed_out=True)


def _failed(sid: str, proc: subprocess.Popen, log_path: Path, retried: bool, timed_out: bool = False) -> None:
    code = proc.poll()
    reason = _log_reason(log_path) or (
        f"OpenCode didn't take it within {VERIFY_SECONDS} s" if timed_out else f"the OpenCode CLI stopped (code {code})")
    if not retried and not timed_out:
        _guard_log(f"[auto-resume] 'continue' didn't arrive in chat {sid[:12]}… ({reason}) — trying once more")
        resume_sessions([sid], _retry=True)
        return
    _guard_log(f"[auto-resume] 'continue' did NOT arrive in chat {sid[:12]}…: {reason}")
    _save_result(sid, False, reason)


def _resume_cli() -> tuple[str | None, str]:
    cli = find_opencode_cli()
    if not cli:
        return None, "OpenCode CLI not found"
    if _is_desktop_exe(cli):
        return None, "OpenCode Desktop was selected instead of the CLI"
    return cli, ""


def resume_sessions(session_ids: list[str], _retry: bool = False) -> tuple[int, list[str]]:
    """Send 'continue' to each listed session through OpenCode's background service.

    'opencode run --session <id>' talks to the same background service OpenCode Desktop uses, so every
    tab that hit the limit carries on (not just the one in front), and the Desktop window updates by itself.
    Whether it really arrived is checked in the background (see _verify).
    """
    targets = dict.fromkeys(sid for sid in session_ids if sid)
    oc, reason = _resume_cli()
    if not oc:
        for sid in targets:
            _save_result(sid, False, reason)
        return 0, [reason]
    sent, notes, jobs = 0, [], []
    try:
        CONTINUE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass
    for sid in targets:
        directory = _session_directory(sid)
        cwd = directory if (directory and os.path.isdir(directory)) else None
        _stop_stale_continue(oc, sid)
        log_path = CONTINUE_DIR / f"{re.sub(r'[^A-Za-z0-9_]', '', sid)[:40]}.log"
        try:
            out = open(log_path, "wb")
        except OSError:
            out = subprocess.DEVNULL
        try:
            kwargs = {"cwd": cwd, "close_fds": True, "env": _run_env(),
                      "stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000 | 0x00000008
            else:
                kwargs["start_new_session"] = True
            since_ms = time.time() * 1000 - 1500
            proc = subprocess.Popen([oc, "run", "--session", sid, "continue"], **kwargs)
            _latest_job[sid] = since_ms
            jobs.append((sid, proc, since_ms, log_path, _retry))
            sent += 1
        except Exception as exc:
            _save_result(sid, False, str(exc))
            notes.append(f"{sid[:12]}…: {exc}")
        finally:
            if out is not subprocess.DEVNULL:
                out.close()   # the child has its own handle
    if jobs:
        import threading
        threading.Thread(target=_verify, args=(jobs,), name="continue-check", daemon=True).start()
    return sent, notes


def resend_failed() -> tuple[int, str]:
    """The app's 'Send again' button: retry 'continue' for the chats where it didn't arrive."""
    sids = list(failed_resumes())
    if not sids:
        return 0, "nothing to resend"
    for sid in sids:
        _save_result(sid, None)   # out of the "didn't arrive" list until the new check answers
    sent, notes = resume_sessions(sids)
    return sent, "; ".join(notes)


def resume_active_session(session_ids: list[str] | None = None) -> tuple[bool, str]:
    """Resume OpenCode after a new IP: every session in session_ids, or the latest one if none are known."""
    if not is_auto_resume_enabled():
        return False, "Auto-resume is disabled."

    # Step 1: Wipe client-side rate limit lockouts from drafts.sqlite
    cleared = clear_opencode_free_limit()
    details = []
    if cleared > 0:
        details.append(f"cleared {cleared} rate limit entries from database")

    if session_ids:
        sent, notes = resume_sessions(session_ids)
        details.append(f"started 'continue' for {sent} of {len(set(session_ids))} chat(s), awaiting confirmation")
        return bool(sent), " · ".join([*details, *notes])

    oc, reason = _resume_cli()
    sess = get_latest_session_info()
    if not oc:
        if sess:
            _save_result(sess[0], False, reason)
        return False, reason
    cli_dispatched = False

    # Step 2: Attempt CLI session resume if a session exists
    if sess:
        sess_id, directory, title = sess
        cmd = [oc, "run", "--session", sess_id, "continue"]
        cwd = directory if (directory and os.path.isdir(directory)) else None
        _stop_stale_continue(oc, sess_id)
        try:
            kwargs = {"cwd": cwd, "close_fds": True, "env": _run_env()}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000 | 0x00000008
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen(cmd, **kwargs)
            cli_dispatched = True
            details.append(f"dispatched CLI resume to session {sess_id[:8]}... ({title or 'active'})")
        except Exception as exc:
            _save_result(sess_id, False, str(exc))
            details.append(f"CLI resume attempt failed: {exc}")
    else:
        try:
            kwargs = {"close_fds": True, "env": _run_env()}
            if sys.platform == "win32":
                kwargs["creationflags"] = 0x08000000 | 0x00000008
            else:
                kwargs["start_new_session"] = True
            subprocess.Popen([oc, "run", "--continue", "continue"], **kwargs)
            cli_dispatched = True
            details.append("dispatched generic CLI continue")
        except Exception:
            pass

    return cli_dispatched, " · ".join(details) if details else "No chat resumed."

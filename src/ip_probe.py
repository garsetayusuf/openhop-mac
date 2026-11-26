"""Test a new IP in openhop's own hidden chat before sending 'continue' to the user's chats.

Since 2026-11-16 almost every new shared IP (WARP, Tor) arrives with its free messages already used up by other
people. Before 3.8 openhop sent 'continue' to every stopped chat after each new IP, so a chat could collect dozens
of 'continue' messages in a row (and the model saw all of them). Now, after a new IP, openhop first sends one small
message to a chat of its own:

  - it answers  -> the IP works: send 'continue' to the user's chats (once each);
  - free limit  -> the IP is used up: the user's chats aren't touched, the guard tries the next address;
  - can't tell  -> (no CLI, no answer in time, another error) send 'continue' as before, so it never does worse.

The test chat is a normal OpenCode chat (same model as the stopped chat, normal agent and tools), created with
--archived so it stays out of the chat list, in its own folder (guard\\ip-test). It goes through OpenCode's
background service and openhop's proxy like every other chat: the real IP is never used.

Made-up session ids and tool-less chats got a different limit in the 2026-11-19 tests (back at exactly 00:00 UTC
on every IP), so this chat is created by OpenCode itself and reused. If two different IPs ever report the very same
"back at" time, the test chat itself is limited: that answer is ignored and a new test chat is made next time.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

from ouc_common import GUARD_DIR, SERVICE_PROXY_PORT, find_opencode_cli, opencode_db_path

STATE_FILE = GUARD_DIR / "ip_probe.json"
PROBE_DIR = GUARD_DIR / "ip-test"          # the test chat's folder (nothing is written there)
LOG_FILE = GUARD_DIR / "ip_probe.log"      # what the last 'opencode run' printed
TITLE = "openhop · IP test"
MESSAGE = "hola (openhop IP test: reply only with ok)"
WAIT_SECONDS = 45          # longest wait for the answer or the limit error
MAX_MESSAGES = 40          # then a new test chat, so it never grows big
SAME_BACK_SECONDS = 5      # two IPs with "back at" times this close = the chat's own limit, not the IP's

WORKS, SPENT, UNKNOWN = "works", "spent", "unknown"


def _load() -> dict:
    try:
        d = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d: dict) -> None:
    try:
        STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def test_chats() -> list[str]:
    """The test chats openhop used lately (their limit errors in opencode.log aren't the user's)."""
    return [str(s) for s in (_load().get("recent") or []) if s]


def last_back_at() -> float:
    """When the address the last test found used up gets its free messages back (epoch seconds, 0 = unknown)."""
    try:
        return float((_load().get("last_spent") or {}).get("back_at") or 0)
    except (TypeError, ValueError):
        return 0.0


def _env() -> dict:
    env = os.environ.copy()
    proxy_url = f"http://127.0.0.1:{SERVICE_PROXY_PORT}"
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env[k] = proxy_url
    for k in ("NO_PROXY", "no_proxy"):
        env[k] = "127.0.0.1,localhost,::1"
    env["PWD"] = str(PROBE_DIR)
    return env


def _last_seq(sid: str) -> int:
    """The chat's newest row number before the test message (older answers must not count)."""
    db = opencode_db_path()
    if not sid or not db.is_file():
        return -1
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        try:
            row = con.execute("SELECT MAX(seq) FROM session_message WHERE session_id = ?", (sid,)).fetchone()
        finally:
            con.close()
        return int(row[0]) if row and row[0] is not None else -1
    except Exception:
        return -1


def _answer(sid: str, since_ms: float, after_seq: int = -1) -> tuple[str, float, str] | None:
    """(verdict, back_at, note) from the first answer row after since_ms (and after row after_seq),
    or None while there's none yet."""
    db = opencode_db_path()
    if not sid or not db.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        try:
            rows = con.execute(
                "SELECT time_created, data FROM session_message WHERE session_id = ? AND type = 'assistant' "
                "AND time_created >= ? AND seq > ? ORDER BY seq", (sid, int(since_ms), after_seq)).fetchall()
        finally:
            con.close()
    except Exception:
        return None
    for created, data in rows:
        try:
            d = json.loads(data or "{}")
        except Exception:
            continue
        err = d.get("error") or {}
        if err:
            if err.get("type") == "provider.free-limit":
                ms = float(err.get("retryAfterMs", d.get("retryAfterMs")) or 0)
                return SPENT, (float(created or 0) + ms) / 1000 if ms > 0 else 0.0, ""
            return UNKNOWN, 0.0, str(err.get("type") or err.get("message") or "another error")[:120]
        if d.get("content") or int((d.get("tokens") or {}).get("output", 0) or 0) > 0:
            return WORKS, 0.0, ""
    return None


def _kill(proc: subprocess.Popen) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass


def check_ip(model: str, ip: str = "", wait: float = WAIT_SECONDS) -> tuple[str, str]:
    """Send the test message through the new IP. Returns (WORKS | SPENT | UNKNOWN, plain-words note)."""
    if not model:
        return UNKNOWN, "no model to test with"
    oc = find_opencode_cli()
    if not oc:
        return UNKNOWN, "OpenCode CLI not found"
    st = _load()
    day = time.strftime("%Y-%m-%d", time.gmtime())
    sid = str(st.get("sid") or "")
    if (not sid or st.get("model") != model or st.get("day") != day
            or int(st.get("count", 0) or 0) >= MAX_MESSAGES or st.get("drop")):
        sid = ""
    try:
        PROBE_DIR.mkdir(parents=True, exist_ok=True)
    except OSError:
        return UNKNOWN, "can't make the test chat's folder"

    cmd = [oc, "run", "--format", "json", "--model", model]
    cmd += ["--session", sid] if sid else ["--archived", "--title", TITLE]
    cmd.append(MESSAGE)
    try:
        out = open(LOG_FILE, "wb")
    except OSError:
        out = subprocess.DEVNULL
    kwargs = {"cwd": str(PROBE_DIR), "close_fds": True, "env": _env(), "stdin": subprocess.DEVNULL,
              "stdout": out, "stderr": subprocess.STDOUT}
    if sys.platform == "win32":
        kwargs["creationflags"] = 0x08000000 | 0x00000008   # no window, detached
    else:
        kwargs["start_new_session"] = True
    after_seq = _last_seq(sid)
    since_ms = time.time() * 1000 - 1500
    try:
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        return UNKNOWN, f"couldn't start OpenCode: {exc}"
    finally:
        if out is not subprocess.DEVNULL:
            out.close()

    found, ended_at = None, 0.0
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(1.0)
        if not sid:
            try:
                m = re.search(r'"sessionID"\s*:\s*"(ses_[A-Za-z0-9]+)"', LOG_FILE.read_text("utf-8", errors="replace"))
            except OSError:
                m = None
            if m:
                sid = m.group(1)
                st.update(sid=sid, model=model, day=day, count=0, drop=False)
                st["recent"] = ([sid] + [s for s in (st.get("recent") or []) if s != sid])[:5]
                _save(st)
        found = _answer(sid, since_ms, after_seq)
        if found:
            break
        if proc.poll() is not None:
            ended_at = ended_at or time.time()
            if time.time() - ended_at > 3:   # the CLI is done and nothing was stored
                break
    _kill(proc)
    st = {**_load(), **{k: st[k] for k in ("sid", "model", "day", "drop") if k in st}}
    st["count"] = int(st.get("count", 0) or 0) + 1
    if not found:
        _save(st)
        try:
            tail = [ln.strip() for ln in LOG_FILE.read_text("utf-8", errors="replace").splitlines() if ln.strip()][-1:]
        except OSError:
            tail = []
        return UNKNOWN, ("no answer in time" if proc.poll() is None or not tail else tail[0][:160])

    verdict, back_at, note = found
    if verdict == SPENT:
        last = st.get("last_spent") or {}
        if (ip and last.get("ip") and last.get("ip") != ip and back_at and last.get("back_at")
                and abs(float(last["back_at"]) - back_at) <= SAME_BACK_SECONDS):
            # Different IPs, same "back at": that's the test chat's own limit. Ignore it; new chat next time.
            st["drop"] = True
            st.pop("last_spent", None)
            _save(st)
            return UNKNOWN, "the test chat itself is limited (a new one is made next time)"
        st["last_spent"] = {"ip": ip, "back_at": back_at}
        _save(st)
        clock = time.strftime("%H:%M", time.localtime(back_at)) if back_at else "?"
        return SPENT, f"its free messages are used up (back around {clock})"
    st.pop("last_spent", None)
    _save(st)
    return verdict, note or ("it answered" if verdict == WORKS else "")

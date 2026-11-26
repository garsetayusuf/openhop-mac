"""Move a chat that hit OpenCode's free limit to a NEW chat that carries on with the work.

Since 2026-11-16 OpenCode's free tier counts the limit per chat, not only per IP: a chat that hit it stays
blocked on every new IP (seen on WARP, Tor, Proton and Norton), while a brand-new chat on the very same IP
works (tested: 40 messages in a row). Forks don't help: OpenCode sends the ORIGINAL chat's id as the
"session affinity" key for forks and for sub-agent chats (parentID ?? fork.sessionID ?? id).

So when a chat hits the limit, openhop:
  1. reads that chat from OpenCode's database (read-only): the first request, the latest summary OpenCode
     wrote when it compacted the chat, and the latest messages + tools used;
  2. writes that into a short Markdown file (guard\\handoff\\<chat>.md);
  3. starts a new chat in the same folder, with the same model and agent, through OpenCode's own background
     service (so OpenCode Desktop shows it), with that file attached and a "carry on" message.

Nothing here talks to the internet by itself: the new chat goes through openhop's proxy like any other.
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

# Off in 3.8: the 2026-11-19 log/report analysis showed real chats hit the per-IP cap, not a per-chat one
# (docs/research-chat-limit/2026-11-19-findings-per-ip.md), so a new chat doesn't help. OPENHOP_NEWCHAT=1 turns it on.
AVAILABLE = os.environ.get("OPENHOP_NEWCHAT") == "1"
HANDOFF_DIR = GUARD_DIR / "handoff"
MAP_FILE = GUARD_DIR / "chat_moves.json"     # {new chat: {"from": old chat, "at": time, "root": first chat}}
# The message openhop types into the new chat. Also how _stop_stale_continue() recognises our processes.
POINTER_PREFIX = "openhop: "
POINTER_MSG = (
    "openhop: este chat ha llegado al límite gratis de OpenCode, que ahora cuenta cada chat por separado. "
    "El trabajo sigue en el chat «{title}»: búscalo en la lista de chats. No hagas nada más aquí, responde solo OK.\n"
    "openhop: this chat hit OpenCode's free limit, which now counts each chat on its own. The work continues in "
    "the chat «{title}»: find it in the chat list. Don't do anything else here, just reply OK."
)
POINTER_MARK = "the work continues in the chat"   # lower-case piece of POINTER_MSG, to recognise it
CONTINUE_MSG = ("openhop: the previous chat reached OpenCode's free limit, so the work continues here. "
                "Read the attached file and carry on from where it stopped.")
FIRST_MAX = 4000
SUMMARY_MAX = 24000
RECENT_MAX = 24000
WAIT_NEW_CHAT = 45.0


# ------------------------------------------------------------------------------------------- reading the chat
def _db() -> sqlite3.Connection | None:
    db = opencode_db_path()
    if not db.is_file():
        return None
    try:
        return sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
    except Exception:
        return None


def _json(text) -> dict:
    try:
        d = json.loads(text or "{}")
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def root_chat(session_id: str) -> str:
    """Sub-agent chats share their parent's limit key: the chat to move is the top-level one."""
    con = _db()
    if con is None:
        return session_id
    try:
        sid = session_id
        for _ in range(10):
            row = con.execute("SELECT parent_id FROM session_v2 WHERE id = ?", (sid,)).fetchone()
            if not row or not row[0]:
                return sid
            sid = str(row[0])
        return sid
    except Exception:
        return session_id
    finally:
        con.close()


def answers(session_id: str) -> int:
    """How many real answers the chat got (assistant rows with content and no error). -1 if unknown.

    A chat openhop opened that hit the limit before its first answer never used its own quota: the address is
    the problem, so it needs a new IP, not yet another new chat (seen on used-up Tor exits, 2026-11-19).
    """
    con = _db()
    if con is None:
        return -1
    try:
        n = 0
        for (data,) in con.execute("SELECT data FROM session_message WHERE session_id = ? AND type = 'assistant'",
                                   (session_id,)):
            try:
                j = json.loads(data or "{}")
            except Exception:
                continue
            if not j.get("error") and j.get("content"):
                n += 1
        return n
    except Exception:
        return -1
    finally:
        con.close()


def chat_info(session_id: str) -> dict | None:
    """{title, directory, agent, model (provider/model#variant)} or None when the chat isn't in the database."""
    con = _db()
    if con is None:
        return None
    try:
        row = con.execute("SELECT title, directory, agent, model, time_archived FROM session_v2 WHERE id = ?",
                          (session_id,)).fetchone()
    except Exception:
        row = None
    finally:
        con.close()
    if not row:
        return None
    title, directory, agent, model, archived = row
    m = _json(model)
    model_arg = ""
    if m.get("providerID") and m.get("id"):
        model_arg = f"{m['providerID']}/{m['id']}"
        if m.get("variant") and m.get("variant") != "default":
            model_arg += f"#{m['variant']}"
    return {"title": str(title or ""), "directory": str(directory or ""), "agent": str(agent or ""),
            "model": model_arg, "archived": bool(archived)}


def _short(text: str, n: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def _tool_line(part: dict) -> str:
    name = str(part.get("name") or "tool")
    st = part.get("state") if isinstance(part.get("state"), dict) else {}
    inp = st.get("input") if isinstance(st.get("input"), dict) else {}
    what = ""
    for key in ("description", "command", "filePath", "path", "pattern", "url", "query", "prompt"):
        if inp.get(key):
            what = str(inp[key])
            break
    what = re.sub(r"\s+", " ", what)
    return f"{name}: {_short(what, 140)}" if what else name


def _messages(con: sqlite3.Connection, session_id: str) -> list[tuple[int, str, dict]]:
    rows = con.execute("SELECT seq, type, data FROM session_message WHERE session_id = ? ORDER BY seq",
                       (session_id,)).fetchall()
    return [(int(seq), str(typ), _json(data)) for seq, typ, data in rows]


def build_handoff(session_id: str, original_request: str = "") -> str:
    """The Markdown the new chat gets. Empty string when the chat can't be read."""
    info = chat_info(session_id)
    con = _db()
    if info is None or con is None:
        return ""
    try:
        msgs = _messages(con, session_id)
    except Exception:
        return ""
    finally:
        con.close()
    if not msgs:
        return ""

    first = original_request
    if not first:
        for _, typ, d in msgs:
            if typ == "user" and str(d.get("text") or "").strip():
                first = str(d["text"])
                break

    summary, summary_seq = "", -1
    for seq, typ, d in msgs:
        if typ == "compaction" and str(d.get("summary") or "").strip():
            summary, summary_seq = str(d["summary"]), seq

    # Latest part of the chat (after the last summary), newest kept first when it's too long.
    blocks: list[str] = []
    last_thought = ""
    for seq, typ, d in msgs:
        if seq <= summary_seq:
            continue
        if typ == "user":
            text = str(d.get("text") or "").strip()
            if text and text.lower() not in ("continue", CONTINUE_MSG.lower()) and POINTER_MARK not in text.lower():
                blocks.append(f"**User:** {_short(text, 3000)}")
        elif typ == "assistant":
            texts, tools = [], []
            for part in d.get("content") or []:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text" and str(part.get("text") or "").strip():
                    texts.append(str(part["text"]).strip())
                elif part.get("type") == "tool":
                    tools.append(_tool_line(part))
            for part in d.get("content") or []:
                if isinstance(part, dict) and part.get("type") == "reasoning" and str(part.get("text") or "").strip():
                    last_thought = str(part["text"]).strip()
            if texts:
                blocks.append(f"**Assistant:** {_short(' '.join(texts), 2500)}")
            if tools:
                line = "; ".join(_short(t, 160) for t in tools)
                if blocks and blocks[-1].startswith("*Tools used:* ") and len(blocks[-1]) < 1500:
                    blocks[-1] += "; " + line   # one line for a run of tool calls
                else:
                    blocks.append("*Tools used:* " + line)
        elif typ == "synthetic":
            text = str(d.get("text") or "")
            m = re.search(r'description="([^"]*)"', text)
            if m:
                blocks.append(f"*Sub-agent finished:* {_short(m.group(1), 200)}")
    recent: list[str] = []
    size = 0
    for b in reversed(blocks):
        if size + len(b) > RECENT_MAX:
            recent.append("*(older messages left out)*")
            break
        recent.append(b)
        size += len(b)
    recent.reverse()

    title = info["title"] or "the previous chat"
    out = [
        "# Continuing an earlier chat",
        "",
        f"openhop opened this chat because the previous one (\"{title}\") reached OpenCode's free usage limit. "
        "That chat stays blocked until the daily reset, but new chats work, so the work continues here.",
        "",
        "**What to do:** pick up exactly where the previous chat stopped. Don't start over and don't redo "
        "finished steps: look at the files in this folder to see the current state, then carry on with the "
        "next step. Keep answering in the language the user was using.",
        "",
        "## What the user asked at the start",
        "",
        _short(first, FIRST_MAX) or "(not found)",
    ]
    if summary:
        out += ["", "## Summary of the work so far (written by OpenCode when it compacted the chat)", "",
                _short(summary, SUMMARY_MAX)]
    if recent:
        out += ["", "## The latest part of the previous chat", ""] + [r + "\n" for r in recent]
    if last_thought:
        out += ["## The assistant's last thought before the limit", "", _short(last_thought, 2000)]
    return "\n".join(out).rstrip() + "\n"


# ------------------------------------------------------------------------------------------ starting the chat
def _load_map() -> dict:
    try:
        d = json.loads(MAP_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save_map(d: dict) -> None:
    now = time.time()
    d = {k: v for k, v in d.items() if isinstance(v, dict) and now - float(v.get("at", 0) or 0) < 3 * 86400}
    try:
        MAP_FILE.write_text(json.dumps(d), encoding="utf-8")
    except OSError:
        pass


def moved_from(new_sid: str) -> dict | None:
    """When new_sid is a chat openhop opened to continue another one: {"from", "at", "root", "request"}."""
    v = _load_map().get(new_sid)
    return v if isinstance(v, dict) else None


def already_moved(old_sid: str) -> str:
    """The chat old_sid was already moved to ('' if none)."""
    for new, v in _load_map().items():
        if isinstance(v, dict) and v.get("from") == old_sid:
            return new
    return ""


def latest_chat(session_id: str) -> str:
    """Follow the moves forward: the chat where session_id's work goes on now (itself if it was never moved)."""
    sid = session_id
    for _ in range(20):
        nxt = already_moved(sid)
        if not nxt:
            return sid
        sid = nxt
    return sid


def post_pointer(old_sid: str, new_sid: str) -> bool:
    """Leave a note in the old chat saying where the work continues, so whoever looks at it in OpenCode sees it.

    It's a normal message (OpenCode has no way to add a note without one), sent through the background service
    so Desktop shows it at once. The old chat is limited, so it just ends with OpenCode's limit error; if its
    limit came back, the note tells the model to only reply OK, so two chats never work on the same thing.
    """
    info = chat_info(old_sid)
    new_info = chat_info(new_sid) or {}
    oc = find_opencode_cli()
    if not info or info.get("archived") or not oc:
        return False
    directory = info.get("directory") or ""
    if not directory or not os.path.isdir(directory):
        return False
    directory = os.path.normpath(directory)
    title = str(new_info.get("title") or "continued")
    cmd = [oc, "run", "--format", "json", "--session", old_sid]
    if info.get("model"):
        cmd += ["--model", info["model"]]
    cmd.append(POINTER_MSG.format(title=title))
    env = _run_env()
    env["PWD"] = directory
    safe = re.sub(r"[^A-Za-z0-9_]", "", old_sid)[:40]
    try:
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        out = open(HANDOFF_DIR / f"{safe}.pointer.log", "wb")
    except OSError:
        return False
    try:
        kwargs = {"cwd": directory, "close_fds": True, "env": env, "stdin": subprocess.DEVNULL, "stdout": out,
                  "stderr": subprocess.STDOUT}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 | 0x00000008   # no window, detached
        else:
            kwargs["start_new_session"] = True
        subprocess.Popen(cmd, **kwargs)
        return True
    except Exception:
        return False
    finally:
        out.close()


def recent_moves(max_age: float = 30 * 60) -> list[dict]:
    """Moves of the last max_age seconds, newest first: [{"new", "old", "title", "at"}] (title of the new chat)."""
    now = time.time()
    out = []
    for new, v in _load_map().items():
        if not isinstance(v, dict) or now - float(v.get("at", 0) or 0) > max_age:
            continue
        info = chat_info(new) or {}
        out.append({"new": new, "old": str(v.get("from") or ""), "title": str(info.get("title") or ""),
                    "at": float(v.get("at", 0) or 0)})
    return sorted(out, key=lambda x: -x["at"])


def _run_env() -> dict:
    env = os.environ.copy()
    proxy_url = f"http://127.0.0.1:{SERVICE_PROXY_PORT}"
    for k in ("HTTPS_PROXY", "HTTP_PROXY", "https_proxy", "http_proxy"):
        env[k] = proxy_url
    for k in ("NO_PROXY", "no_proxy"):
        env[k] = "127.0.0.1,localhost,::1"
    return env


def _new_title(title: str) -> str:
    title = (title or "Chat").strip()
    m = re.search(r"\(continued(?: (\d+))?\)$", title)
    if m:
        n = int(m.group(1) or 1) + 1
        return title[: m.start()].rstrip() + f" (continued {n})"
    return f"{title} (continued)"


def move_to_new_chat(session_id: str) -> tuple[str, str]:
    """Start a new chat that carries on with session_id's work. Returns (new chat id or '', note)."""
    sid = root_chat(session_id)
    done = already_moved(sid)
    if done:
        return done, "already moved"
    info = chat_info(sid)
    if info is None:
        return "", "that chat isn't in OpenCode's database"
    if info.get("archived"):
        return "", "that chat is archived"
    oc = find_opencode_cli()
    if not oc:
        return "", "OpenCode CLI not found"
    prev = moved_from(sid) or {}
    text = build_handoff(sid, original_request=str(prev.get("request") or ""))
    if not text:
        return "", "couldn't read that chat"
    try:
        HANDOFF_DIR.mkdir(parents=True, exist_ok=True)
        for old in sorted(HANDOFF_DIR.glob("*.md"), key=lambda p: p.stat().st_mtime)[:-30]:
            old.unlink(missing_ok=True)
    except OSError:
        pass
    safe = re.sub(r"[^A-Za-z0-9_]", "", sid)[:40]
    md = HANDOFF_DIR / f"{safe}.md"
    log_path = HANDOFF_DIR / f"{safe}.log"
    md.write_text(text, encoding="utf-8")

    cmd = [oc, "run", "--format", "json", "--title", _new_title(info["title"]), "--file", str(md)]
    if info["model"]:
        cmd += ["--model", info["model"]]
    if info["agent"]:
        cmd += ["--agent", info["agent"]]
    cmd.append(CONTINUE_MSG)
    directory = info["directory"]
    if not directory or not os.path.isdir(directory):
        return "", "that chat's folder doesn't exist any more"
    directory = os.path.normpath(directory)
    env = _run_env()
    env["PWD"] = directory   # the OpenCode CLI takes the chat's folder from PWD when it's set (Git Bash, shells)
    try:
        out = open(log_path, "wb")
    except OSError:
        return "", "can't write the log file"
    try:
        kwargs = {"cwd": directory, "close_fds": True, "env": env, "stdin": subprocess.DEVNULL, "stdout": out, "stderr": subprocess.STDOUT}
        if sys.platform == "win32":
            kwargs["creationflags"] = 0x08000000 | 0x00000008   # no window, detached
        else:
            kwargs["start_new_session"] = True
        proc = subprocess.Popen(cmd, **kwargs)
    except Exception as exc:
        return "", f"couldn't start OpenCode: {exc}"
    finally:
        out.close()

    # The new chat's id is in the first JSON events OpenCode prints.
    deadline = time.time() + WAIT_NEW_CHAT
    new_sid = ""
    while time.time() < deadline and not new_sid:
        time.sleep(1.0)
        try:
            m = re.search(r'"sessionID"\s*:\s*"(ses_[A-Za-z0-9]+)"', log_path.read_text("utf-8", errors="replace"))
        except OSError:
            m = None
        if m and m.group(1) != sid:
            new_sid = m.group(1)
        elif proc.poll() is not None:
            break
    if not new_sid:
        try:
            tail = log_path.read_text("utf-8", errors="replace").strip().splitlines()[-2:]
        except OSError:
            tail = []
        return "", "OpenCode didn't open the new chat" + (f" ({' | '.join(tail)[:200]})" if tail else "")

    first_req = str(prev.get("request") or "")
    if not first_req:
        con = _db()
        if con is not None:
            try:
                for _, typ, d in _messages(con, sid):
                    if typ == "user" and str(d.get("text") or "").strip():
                        first_req = _short(str(d["text"]), FIRST_MAX)
                        break
            except Exception:
                pass
            finally:
                con.close()
    m = _load_map()
    m[new_sid] = {"from": sid, "at": time.time(), "root": str(prev.get("root") or sid), "request": first_req}
    _save_map(m)
    post_pointer(sid, new_sid)   # the user is looking at the old chat: tell them there where the work went
    return new_sid, "ok"


if __name__ == "__main__":   # manual use: python chat_handoff.py <ses_…> [--print]
    if len(sys.argv) > 2 and sys.argv[2] == "--print":
        print(build_handoff(sys.argv[1]))
    elif len(sys.argv) > 1:
        print(move_to_new_chat(sys.argv[1]))

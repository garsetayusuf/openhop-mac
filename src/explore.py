"""Explorar on this PC: talks to https://openhop.cfd/api/explore and installs what the user picks into OpenCode.

Only text add-ons exist (the server refuses anything else), and this module only ever writes these files:
- skill:   <project>/.agents/skills/<slug>/SKILL.md   or  ~/.agents/skills/<slug>/SKILL.md
- command: <project>/.opencode/commands/<slug>.md     or  ~/.config/opencode/commands/<slug>.md
- rules:   a marked block at the end of <project>/AGENTS.md  or  ~/.config/opencode/AGENTS.md
- prompt:  nothing (the text is put in the chat box by the page)

"<project>" must be a folder OpenCode itself knows as a project (its opencode.db), so the page can't make this write
anywhere else. Existing files that openhop didn't put there are never overwritten. What was installed is remembered
in %LOCALAPPDATA%\\openhop\\explore\\installed.json so it can be taken out again.
"""

from __future__ import annotations

import json
import os
import re
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from ouc_common import DEST_DIR, VERSION, append_log, opencode_db_path

EXPLORE_URL = "https://openhop.cfd/api/explore"
STATE_DIR = DEST_DIR / "explore"
INSTALLED_FILE = STATE_DIR / "installed.json"
LOG_FILE = DEST_DIR / "logs" / "explore.log"
MAX_ANSWER = 256 * 1024
ITEM_ID_RE = re.compile(r"^x_[A-Za-z0-9]{10}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
KINDS = ("skill", "command", "rules", "prompt")

_lock = threading.RLock()


def _base() -> str:
    # For local testing only: OPENHOP_EXPLORE_URL=http://127.0.0.1:8799/api/explore (never https elsewhere).
    url = os.environ.get("OPENHOP_EXPLORE_URL", "")
    if url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:"):
        return url.rstrip("/")
    return EXPLORE_URL


def _log(msg: str) -> None:
    append_log(LOG_FILE, msg)


# ---------------------------------------------------------------------------------------------- server
def _call(method: str, path: str, body: dict | None = None, auth: bool = False, timeout: float = 15.0) -> dict:
    """{"ok": True, ...} or {"ok": False, "code", "message"}; never raises."""
    headers = {"User-Agent": f"openhop/{VERSION}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth:
        try:
            from rank import _load_key
            key = _load_key()
        except Exception:
            key = ""
        if not key:
            return {"ok": False, "code": "auth", "message": "To publish, join the ranking first (Ranking tab in openhop)."}
        headers["Authorization"] = f"Bearer {key}"
    try:
        req = urllib.request.Request(_base() + path, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as exc:
        try:
            out = json.loads(exc.read(MAX_ANSWER + 1).decode("utf-8"))
            if isinstance(out, dict):
                out["ok"] = False
                out.setdefault("code", f"http_{exc.code}")
                return out
        except Exception:
            pass
        return {"ok": False, "code": f"http_{exc.code}", "message": "openhop.cfd answered with an error."}
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        _log(f"{method} {path.split('?')[0]}: offline ({exc})")
        return {"ok": False, "code": "offline", "message": "Can't reach openhop.cfd right now."}
    if len(raw) > MAX_ANSWER:
        return {"ok": False, "code": "bad_answer", "message": "openhop.cfd sent too much."}
    try:
        out = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"ok": False, "code": "bad_answer", "message": "openhop.cfd sent something unexpected."}
    return out if isinstance(out, dict) else {"ok": False, "code": "bad_answer", "message": ""}


def browse(kind: str = "", q: str = "", sort: str = "new", page: int = 0) -> dict:
    params = {"sort": "top" if sort == "top" else "new", "page": max(0, min(50, int(page or 0)))}
    if kind in KINDS:
        params["kind"] = kind
    if q:
        params["q"] = str(q)[:40]
    return _call("GET", "/list?" + urllib.parse.urlencode(params))


def item(item_id: str) -> dict:
    if not ITEM_ID_RE.match(item_id or ""):
        return {"ok": False, "code": "not_found", "message": "That item isn't there any more."}
    return _call("GET", "/item?id=" + item_id)


def mine() -> dict:
    return _call("GET", "/mine", auth=True)


def submit(data: dict) -> dict:
    keys = ("kind", "title", "summary", "text", "tags", "lang")
    return _call("POST", "/submit", {k: data.get(k) for k in keys if k in data}, auth=True)


def delete(item_id: str) -> dict:
    return _call("POST", "/delete", {"id": item_id}, auth=True)


def report(item_id: str, reason: str = "") -> dict:
    return _call("POST", "/report", {"id": item_id, "reason": str(reason or "")[:200]})


_popular: dict = {"at": 0.0, "value": None}


def popular() -> dict:
    """The free models people used most on openhop this week (the ranking's public "everyone" box), for the model
    picker's "most used" labels. Cached for 10 minutes; no key needed."""
    now = time.time()
    if _popular["value"] is not None and now - _popular["at"] < 600:
        return _popular["value"]
    out: dict = {"ok": False, "models": []}
    try:
        req = urllib.request.Request("https://openhop.cfd/api/rank/board?period=week",
                                     headers={"User-Agent": f"openhop/{VERSION}", "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read(MAX_ANSWER).decode("utf-8"))
        models = ((data or {}).get("community") or {}).get("models") or []
        out = {"ok": True, "models": [{"id": str(m.get("id", ""))[:80], "tokens": int(m.get("tokens", 0) or 0)}
                                      for m in models[:8] if isinstance(m, dict)]}
    except Exception as exc:
        _log(f"popular: {exc}")
    _popular.update(at=now, value=out)
    return out


def account() -> dict:
    """Whether this PC can publish (it's in the ranking) and under which name."""
    try:
        import rank
        st = rank.status() if hasattr(rank, "status") else {}
        joined = bool(rank._load_key())
        return {"ok": True, "joined": joined, "handle": (st or {}).get("handle", "") if joined else ""}
    except Exception:
        return {"ok": True, "joined": False, "handle": ""}


# ---------------------------------------------------------------------------------------------- where things go
def opencode_projects() -> list[str]:
    """The project folders OpenCode knows (its own database, read-only)."""
    OPENCODE_DB = opencode_db_path()
    if not OPENCODE_DB.is_file():
        return []
    try:
        con = sqlite3.connect(f"file:{OPENCODE_DB.as_posix()}?mode=ro", uri=True, timeout=3.0)
        try:
            rows = con.execute("SELECT worktree FROM project WHERE worktree IS NOT NULL AND worktree <> ''").fetchall()
        finally:
            con.close()
    except Exception:
        return []
    return [str(r[0]) for r in rows]


def _norm(p: str) -> str:
    return os.path.normcase(os.path.normpath(str(p).replace("/", os.sep)))


def project_dir(raw: str) -> Path | None:
    """`raw` if it's an existing folder OpenCode has as a project, else None."""
    if not raw or not isinstance(raw, str) or len(raw) > 500:
        return None
    want = _norm(raw)
    for w in opencode_projects():
        if _norm(w) == want:
            p = Path(os.path.normpath(w.replace("/", os.sep)))
            return p if p.is_dir() else None
    return None


def _home() -> Path:
    return Path.home()


def target(kind: str, slug: str, scope: str, proj: Path | None) -> Path | None:
    if not SLUG_RE.match(slug or ""):
        return None
    if scope == "project":
        if proj is None:
            return None
        base = {"skill": proj / ".agents" / "skills" / slug / "SKILL.md",
                "command": proj / ".opencode" / "commands" / f"{slug}.md",
                "rules": proj / "AGENTS.md"}
    else:
        cfg = _home() / ".config" / "opencode"
        base = {"skill": _home() / ".agents" / "skills" / slug / "SKILL.md",
                "command": cfg / "commands" / f"{slug}.md",
                "rules": cfg / "AGENTS.md"}
    return base.get(kind)


def _yaml_line(s: str) -> str:
    return json.dumps(str(s), ensure_ascii=False)  # a JSON string is a valid YAML double-quoted scalar


def file_text(it: dict) -> str:
    """What goes in the file: the item's text, with the small header OpenCode needs when the author left it out."""
    text = str(it.get("text", "")).replace("\r\n", "\n").strip() + "\n"
    has_header = text.startswith("---\n")
    if it["kind"] == "skill" and not has_header:
        return f"---\nname: {it['slug']}\ndescription: {_yaml_line(it.get('summary', ''))}\n---\n\n{text}"
    if it["kind"] == "command" and not has_header:
        return f"---\ndescription: {_yaml_line(it.get('summary', ''))}\n---\n\n{text}"
    return text


def _marks(item_id: str) -> tuple[str, str]:
    return (f"<!-- openhop explorar {item_id}: start (remove from openhop) -->",
            f"<!-- openhop explorar {item_id}: end -->")


def _rules_without(text: str, item_id: str) -> str:
    start, end = _marks(item_id)
    pat = re.compile(r"\n*" + re.escape(start) + r".*?" + re.escape(end) + r"\n?", re.S)
    return pat.sub("\n", text).rstrip() + ("\n" if text.strip() else "")


# ---------------------------------------------------------------------------------------------- installed list
def _read_installed() -> dict:
    try:
        data = json.loads(INSTALLED_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_installed(data: dict) -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = INSTALLED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(INSTALLED_FILE)


def installed() -> dict:
    data = _read_installed()
    items = [{**v, "id": k.split("|")[0], "present": Path(v.get("path", "")).exists()} for k, v in data.items()]
    items.sort(key=lambda x: -float(x.get("at", 0) or 0))
    return {"ok": True, "items": items}


def _key(item_id: str, scope: str, proj: Path | None) -> str:
    return f"{item_id}|{scope}|{_norm(str(proj)) if proj else ''}"


# ---------------------------------------------------------------------------------------------- install / remove
def install(item_id: str, scope: str = "project", project: str = "", overwrite: bool = False) -> dict:
    """Download one item and put its file in place. {"ok", "path", "message"} or {"ok": False, "code", "message"}."""
    scope = "global" if scope == "global" else "project"
    proj = project_dir(project) if scope == "project" else None
    if scope == "project" and proj is None:
        return {"ok": False, "code": "project", "message": "Open a project in OpenCode first."}
    got = item(item_id)
    if not got.get("ok"):
        return got
    it = got.get("item") or {}
    kind, slug = it.get("kind"), it.get("slug", "")
    if kind not in KINDS or not SLUG_RE.match(slug or "") or not isinstance(it.get("text"), str):
        return {"ok": False, "code": "bad_answer", "message": "openhop.cfd sent something unexpected."}
    if kind == "prompt":
        _call("POST", "/installed", {"id": item_id})
        return {"ok": True, "prompt": it["text"], "message": "Prompt ready in the chat box."}
    path = target(kind, slug, scope, proj)
    if path is None:
        return {"ok": False, "code": "bad_request", "message": "Can't install that here."}
    key = _key(item_id, scope, proj)
    with _lock:
        records = _read_installed()
        ours = {r.get("path") for r in records.values()}
        try:
            if kind == "rules":
                old = path.read_text(encoding="utf-8") if path.is_file() else ""
                start, end = _marks(item_id)
                block = f"{start}\n<!-- {it.get('title', '')} · @{it.get('handle', '')} -->\n{file_text(it).strip()}\n{end}\n"
                kept = _rules_without(old, item_id)
                new = (kept.rstrip() + "\n\n" if kept.strip() else "") + block
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(new, encoding="utf-8", newline="\n")
            else:
                if path.exists() and str(path) not in ours and not overwrite:
                    return {"ok": False, "code": "exists", "path": str(path),
                            "message": "You already have one with that name. openhop won't replace it."}
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(file_text(it), encoding="utf-8", newline="\n")
        except OSError as exc:
            _log(f"install {item_id} -> {path}: {exc}")
            return {"ok": False, "code": "write", "message": "Couldn't save the file (the folder may be read-only)."}
        records[key] = {"kind": kind, "scope": scope, "project": str(proj or ""), "path": str(path),
                        "title": it.get("title", ""), "handle": it.get("handle", ""), "at": time.time()}
        _write_installed(records)
    _call("POST", "/installed", {"id": item_id})
    _log(f"installed {item_id} ({kind}, {scope}) -> {path}")
    return {"ok": True, "path": str(path), "kind": kind,
            "message": "Installed. OpenCode picks it up in a new chat."}


def uninstall(item_id: str, scope: str = "project", project: str = "") -> dict:
    scope = "global" if scope == "global" else "project"
    with _lock:
        records = _read_installed()
        key = next((k for k, v in records.items() if k.split("|")[0] == item_id and v.get("scope") == scope
                    and (scope == "global" or _norm(v.get("project", "")) == _norm(project or ""))), None)
        if not key:
            return {"ok": False, "code": "not_found", "message": "That isn't installed here."}
        rec = records[key]
        path = Path(rec.get("path", ""))
        try:
            if rec.get("kind") == "rules":
                if path.is_file():
                    left = _rules_without(path.read_text(encoding="utf-8"), item_id)
                    if left.strip():
                        path.write_text(left, encoding="utf-8", newline="\n")
                    else:
                        path.unlink()
            elif path.is_file():
                path.unlink()
                if rec.get("kind") == "skill":
                    try:
                        path.parent.rmdir()  # only if openhop's SKILL.md was all there was
                    except OSError:
                        pass
        except OSError as exc:
            _log(f"uninstall {item_id} {path}: {exc}")
            return {"ok": False, "code": "write", "message": "Couldn't remove the file."}
        del records[key]
        _write_installed(records)
    return {"ok": True, "message": "Removed."}

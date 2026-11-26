"""Announcements and maintenance mode, set by the openhop dev team.

The app reads https://openhop.cfd/api/notices every few minutes:
    {"v": 1, "updated": ts,
     "maintenance": {"mode": "off|notice|block_soft|block", "title", "message", "title_es", "message_es",
                     "until", "link", "min_version", "max_version"},
     "notices": [{"id", "style", "title", "body", "title_es", "body_es", "link", "link_label",
                  "link_label_es", "show": "banner|popup|both", "dismissible", "start", "end",
                  "min_version", "max_version"}]}

Everything from the server is treated as untrusted: plain text only (the UI never renders it as HTML),
fixed lists for styles and modes, https links only, lengths capped. If the server can't be reached, the
last copy is used for a while and then dropped, so openhop never stays blocked because of a stale copy.

Maintenance modes:
  notice      a popup when the app opens + a banner.
  block_soft  openhop is paused, with a small "continue anyway" (asks once per maintenance).
  block       openhop is paused. "Paused" = the window shows the maintenance screen and Zen Guard stops
              rotating IPs. The proxy keeps running, so OpenCode itself still connects (it just won't
              get new IPs): cutting it would break OpenCode for everyone wired to openhop.
"""

from __future__ import annotations

import ctypes
import json
import re
import threading
import time
import urllib.request

from ouc_common import DEST_DIR, VERSION, append_log

NOTICES_URL = "https://openhop.cfd/api/notices"
CACHE_FILE = DEST_DIR / "notices.json"
STATE_FILE = DEST_DIR / "notices_state.json"   # dismissed notices, "continue anyway" choices
LOG_FILE = DEST_DIR / "logs" / "notices.log"
# The dev team's own PCs: this file makes maintenance not apply here (announcements still show), so the team can
# keep using and testing openhop while users see the maintenance screen.
SKIP_FILE = DEST_DIR / "skip_maintenance.flag"
REFRESH_EVERY = 5 * 60
TRUST_CACHE_FOR = 6 * 3600     # an old copy older than this is ignored (fail open)
MAX_BYTES = 64 * 1024

MODES = ("off", "notice", "block_soft", "block")
STYLES = ("stripes", "info", "success", "warning", "danger", "plain")
SHOWS = ("banner", "popup", "both")
MAX_NOTICES = 10

_lock = threading.Lock()
_VER_RE = re.compile(r"^\d{1,4}(\.\d{1,4}){0,3}$")
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,40}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f​-‏‪-‮⁦-⁩]")


# ---------------------------------------------------------------- validation
def _text(value, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return _CTRL_RE.sub("", value).strip()[:limit]


def _link(value) -> str:
    if not isinstance(value, str):
        return ""
    v = value.strip()
    if len(v) > 300 or not v.startswith("https://") or any(c in v for c in " \t\r\n\"'<>\\`"):
        return ""
    return v


def _num(value) -> int:
    try:
        n = int(value)
    except (TypeError, ValueError):
        return 0
    return n if 0 <= n < 10_000_000_000 else 0


def _ver(value) -> str:
    return value.strip() if isinstance(value, str) and _VER_RE.match(value.strip()) else ""


def sanitize(raw) -> dict:
    """Keep only well-formed fields. Unknown modes/styles fall back to the safest choice."""
    out = {"v": 1, "updated": 0, "maintenance": {"mode": "off"}, "notices": []}
    if not isinstance(raw, dict):
        return out
    out["updated"] = _num(raw.get("updated"))
    m = raw.get("maintenance")
    if isinstance(m, dict):
        mode = m.get("mode") if m.get("mode") in MODES else "off"
        out["maintenance"] = {
            "mode": mode,
            "title": _text(m.get("title"), 120), "message": _text(m.get("message"), 600),
            "title_es": _text(m.get("title_es"), 120), "message_es": _text(m.get("message_es"), 600),
            "until": _num(m.get("until")), "link": _link(m.get("link")),
            "min_version": _ver(m.get("min_version")), "max_version": _ver(m.get("max_version")),
        }
    items = raw.get("notices")
    if isinstance(items, list):
        for n in items[:MAX_NOTICES]:
            if not isinstance(n, dict) or not isinstance(n.get("id"), str) or not _ID_RE.match(n["id"]):
                continue
            item = {
                "id": n["id"],
                "style": n.get("style") if n.get("style") in STYLES else "info",
                "title": _text(n.get("title"), 120), "body": _text(n.get("body"), 600),
                "title_es": _text(n.get("title_es"), 120), "body_es": _text(n.get("body_es"), 600),
                "link": _link(n.get("link")),
                "link_label": _text(n.get("link_label"), 40), "link_label_es": _text(n.get("link_label_es"), 40),
                "show": n.get("show") if n.get("show") in SHOWS else "banner",
                "dismissible": n.get("dismissible") is not False,
                "start": _num(n.get("start")), "end": _num(n.get("end")),
                "min_version": _ver(n.get("min_version")), "max_version": _ver(n.get("max_version")),
            }
            if item["title"] or item["body"]:
                out["notices"].append(item)
    return out


def _vtuple(v: str) -> tuple[int, ...]:
    return tuple(int(p) for p in v.split(".")) if v else ()


def _for_version(item: dict, version: str = VERSION) -> bool:
    cur = _vtuple(_ver(version) or "0")
    lo, hi = item.get("min_version", ""), item.get("max_version", "")
    return (not lo or cur >= _vtuple(lo)) and (not hi or cur <= _vtuple(hi))


def spanish() -> bool:
    """Show the Spanish text? Follows openhop's language setting (auto = Windows language)."""
    try:
        from ouc_common import ui_lang
        return ui_lang() == "es"
    except Exception:
        try:
            return (ctypes.windll.kernel32.GetUserDefaultUILanguage() & 0x3FF) == 0x0A
        except Exception:
            return False


def _localize(item: dict, keys: tuple[str, ...], es: bool) -> dict:
    out = dict(item)
    for k in keys:
        if es and item.get(f"{k}_es"):
            out[k] = item[f"{k}_es"]
        out.pop(f"{k}_es", None)
    return out


# ---------------------------------------------------------------- storage
def _read_json(path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json(path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except Exception as exc:
        append_log(LOG_FILE, f"couldn't save {path.name}: {exc}")


def fetch(timeout: float = 10.0) -> bool:
    """Download, check and cache the current notices. Returns True on success."""
    try:
        req = urllib.request.Request(NOTICES_URL, headers={"User-Agent": f"openhop/{VERSION}",
                                                           "Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if resp.status != 200 or not resp.geturl().startswith("https://openhop.cfd/"):
                raise ValueError(f"unexpected answer ({resp.status})")
            body = resp.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError("answer too big")
        data = sanitize(json.loads(body.decode("utf-8")))
    except Exception as exc:
        append_log(LOG_FILE, f"couldn't read notices: {exc}")
        return False
    with _lock:
        old = _read_json(CACHE_FILE).get("data") or {}
        _write_json(CACHE_FILE, {"fetched": int(time.time()), "data": data})
    if (old.get("maintenance") or {}).get("mode") != data["maintenance"]["mode"]:
        append_log(LOG_FILE, f"maintenance mode: {data['maintenance']['mode']}")
    return True


def _cached() -> dict:
    with _lock:
        c = _read_json(CACHE_FILE)
    if time.time() - _num(c.get("fetched")) > TRUST_CACHE_FOR:
        return sanitize({})
    return sanitize(c.get("data"))


def _maint_key(m: dict) -> str:
    """Identifies one maintenance, so "continue anyway" isn't remembered into the next one."""
    return f"{m.get('mode')}|{m.get('title')}|{m.get('message')}|{m.get('until')}"


# ---------------------------------------------------------------- what the app shows
def current(version: str = VERSION) -> dict:
    """What the window should show right now (already localized, filtered and with dismissals applied)."""
    data = _cached()
    es = spanish()
    state = _read_json(STATE_FILE)
    dismissed = set(state.get("dismissed") or [])
    now = time.time()

    m = data["maintenance"]
    if m["mode"] != "off" and (not _for_version(m, version) or SKIP_FILE.is_file()):
        m = {"mode": "off"}
    m = _localize(m, ("title", "message"), es)
    m["overridden"] = m["mode"] == "block_soft" and state.get("override") == _maint_key(data["maintenance"])

    notices = []
    for n in data["notices"]:
        if (n["start"] and now < n["start"]) or (n["end"] and now > n["end"]) or not _for_version(n, version):
            continue
        if n["dismissible"] and n["id"] in dismissed:
            continue
        notices.append(_localize(n, ("title", "body", "link_label"), es))
    return {"maintenance": m, "notices": notices, "lang": "es" if es else "en"}


def blocked(version: str = VERSION) -> bool:
    """True while maintenance says openhop must not rotate (read by Zen Guard every tick)."""
    m = current(version)["maintenance"]
    return m["mode"] == "block" or (m["mode"] == "block_soft" and not m.get("overridden"))


def dismiss(notice_id: str) -> None:
    if not isinstance(notice_id, str) or not _ID_RE.match(notice_id):
        return
    with _lock:
        state = _read_json(STATE_FILE)
        ids = [i for i in (state.get("dismissed") or []) if isinstance(i, str)][-50:]
        if notice_id not in ids:
            ids.append(notice_id)
        state["dismissed"] = ids
        _write_json(STATE_FILE, state)


def continue_anyway() -> bool:
    """The user chose "continue anyway" during a block_soft maintenance (remembered for this one only)."""
    data = _cached()
    if data["maintenance"]["mode"] != "block_soft":
        return False
    with _lock:
        state = _read_json(STATE_FILE)
        state["override"] = _maint_key(data["maintenance"])
        _write_json(STATE_FILE, state)
    append_log(LOG_FILE, "user chose to continue during maintenance")
    return True


def start_background(on_change=None) -> None:
    """Refresh every few minutes; call on_change(current()) when what's shown changes."""
    def loop() -> None:
        last = ""
        while True:
            fetch()
            try:
                cur = current()
                key = json.dumps(cur, sort_keys=True)
                if on_change and key != last:
                    last = key
                    on_change(cur)
            except Exception as exc:
                append_log(LOG_FILE, f"notices loop error: {exc}")
            time.sleep(REFRESH_EVERY)

    threading.Thread(target=loop, name="openhop-notices", daemon=True).start()

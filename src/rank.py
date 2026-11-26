"""openhop ranking (optional, in-app only): uploads token counts and new-IP counts to https://openhop.cfd/api/rank.

Nothing is sent until the user joins from the Ranking tab. What is sent, per OpenCode message (free OpenCode Zen
provider only): a hash of the message id, its times, the model id and its token counts. Never chat text, titles,
projects, folders or file names. The server answers how far it got (`mark`) and when to upload again (`next_in`).

Files in %LOCALAPPDATA%\\openhop\\rank\\:
    key.bin      this PC's ranking key, encrypted with Windows DPAPI (only this Windows user can read it)
    state.json   handle, account/device ids, next upload time, last result (no key in here)

The personal "fun stats" (streak, favourite hour, speed, ...) are computed here from opencode.db and never uploaded.
"""

from __future__ import annotations

import ctypes
import hashlib
import json
import os
import socket
import sqlite3
import sys
import threading
import time
import urllib.error
import urllib.request
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

from ouc_common import DEST_DIR, GUARD_STATE_FILE, VERSION, append_log, opencode_db_path

RANK_URL = "https://openhop.cfd/api/rank"
RANK_DIR = DEST_DIR / "rank"
KEY_FILE = RANK_DIR / "key.bin"
STATE_FILE = RANK_DIR / "state.json"
LOG_FILE = DEST_DIR / "logs" / "rank.log"

PROVIDER = "opencode"          # only the free OpenCode Zen provider counts (what openhop protects)
BATCH = 300                    # server max per upload
MAX_BATCHES_PER_SYNC = 25      # a heavy week of backfill is ~6000 messages
SETTLE_MS = 2 * 60 * 1000      # leave the newest 2 minutes alone: OpenCode may still be writing them
MAX_ANSWER = 512 * 1024
STATS_CACHE_S = 300
QUIJOTE_TOKENS = 500_000       # roughly one Don Quixote

_lock = threading.RLock()
_stats_cache: tuple[float, dict] | None = None


def _base() -> str:
    # For local testing only: OPENHOP_RANK_URL=http://127.0.0.1:8799/api/rank (never https elsewhere).
    url = os.environ.get("OPENHOP_RANK_URL", "")
    if url.startswith("http://127.0.0.1:") or url.startswith("http://localhost:"):
        return url.rstrip("/")
    return RANK_URL


# ---------------------------------------------------------------- key storage (DPAPI)
class _Blob(ctypes.Structure):
    _fields_ = [("cbData", ctypes.c_uint32), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi(data: bytes, protect: bool) -> bytes:
    if sys.platform != "win32":
        return data  # Linux: the file is chmod 600 instead
    crypt = ctypes.windll.crypt32
    kernel = ctypes.windll.kernel32
    buf = ctypes.create_string_buffer(data, len(data))
    src = _Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    out = _Blob()
    entropy_raw = b"openhop-rank-key"
    ebuf = ctypes.create_string_buffer(entropy_raw, len(entropy_raw))
    entropy = _Blob(len(entropy_raw), ctypes.cast(ebuf, ctypes.POINTER(ctypes.c_char)))
    fn = crypt.CryptProtectData if protect else crypt.CryptUnprotectData
    ok = fn(ctypes.byref(src), None, ctypes.byref(entropy), None, None, 0x1, ctypes.byref(out))  # UI_FORBIDDEN
    if not ok:
        raise OSError("DPAPI failed")
    try:
        return ctypes.string_at(out.pbData, out.cbData)
    finally:
        kernel.LocalFree(out.pbData)


def _save_key(token: str) -> None:
    RANK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = KEY_FILE.with_suffix(".tmp")
    tmp.write_bytes(_dpapi(token.encode("ascii"), True))
    if sys.platform != "win32":
        os.chmod(tmp, 0o600)
    tmp.replace(KEY_FILE)


def _load_key() -> str:
    try:
        token = _dpapi(KEY_FILE.read_bytes(), False).decode("ascii")
        return token if token.startswith("ohr_") and len(token) == 47 else ""
    except Exception:
        return ""


def _read_state() -> dict:
    try:
        data = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_state(data: dict) -> None:
    try:
        RANK_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(STATE_FILE)
    except Exception as exc:
        append_log(LOG_FILE, f"couldn't save state: {exc}")


def _forget_local() -> None:
    for p in (KEY_FILE, STATE_FILE):
        try:
            p.unlink()
        except FileNotFoundError:
            pass
        except Exception as exc:
            append_log(LOG_FILE, f"couldn't delete {p.name}: {exc}")


# ---------------------------------------------------------------- HTTP
def _call(method: str, path: str, body: dict | None = None, auth: bool = True, timeout: float = 20.0) -> dict:
    """{"ok": True, ...} or {"ok": False, "code": "...", "message": "..."}; never raises."""
    headers = {"User-Agent": f"openhop/{VERSION}", "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if auth:
        key = _load_key()
        if not key:
            return {"ok": False, "code": "auth", "message": "Not in the ranking on this PC."}
        headers["Authorization"] = f"Bearer {key}"
    url = _base() + path
    try:
        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(MAX_ANSWER + 1)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(MAX_ANSWER + 1)
        except Exception:
            raw = b""
        try:
            out = json.loads(raw.decode("utf-8"))
            if isinstance(out, dict):
                out["ok"] = False
                out.setdefault("code", f"http_{exc.code}")
                return out
        except Exception:
            pass
        return {"ok": False, "code": f"http_{exc.code}", "message": "The ranking server answered with an error."}
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        append_log(LOG_FILE, f"{method} {path}: offline ({exc})")
        return {"ok": False, "code": "offline", "message": "Can't reach openhop.cfd right now."}
    if len(raw) > MAX_ANSWER:
        return {"ok": False, "code": "bad_answer", "message": "The ranking server sent too much."}
    try:
        out = json.loads(raw.decode("utf-8"))
    except Exception:
        return {"ok": False, "code": "bad_answer", "message": "The ranking server sent something unexpected."}
    return out if isinstance(out, dict) else {"ok": False, "code": "bad_answer", "message": ""}


def _label() -> str:
    return "Linux PC" if sys.platform.startswith("linux") else "Windows PC"


# ---------------------------------------------------------------- OpenCode data (read only)
def _connect(db: Path | None = None):
    path = Path(db) if db else opencode_db_path()
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
    conn.execute("PRAGMA query_only = ON")
    return conn


_MSG_SQL = """
SELECT id,
       json_extract(data, '$.model.providerID'), json_extract(data, '$.model.id'),
       json_extract(data, '$.time.created'), json_extract(data, '$.time.firstToken'), json_extract(data, '$.time.completed'),
       json_extract(data, '$.tokens.input'), json_extract(data, '$.tokens.output'),
       json_extract(data, '$.tokens.cache.read'), json_extract(data, '$.tokens.cache.write')
FROM session_message
WHERE time_updated > ? AND json_type(data, '$.tokens') = 'object'
"""


def _int(v) -> int:
    try:
        n = int(v or 0)
        return n if n > 0 else 0
    except Exception:
        return 0


def _messages_since(mark_ms: int, now_ms: int, db: Path | None = None) -> list[list]:
    """Completed free-provider messages after `mark_ms`, oldest first, in the upload format."""
    conn = _connect(db)
    if conn is None:
        return []
    out = []
    try:
        for (mid, prov, model, created, first, done, t_in, t_out, cr, cw) in conn.execute(_MSG_SQL, (int(mark_ms),)):
            if prov != PROVIDER or not mid:
                continue
            done, created = _int(done), _int(created)
            if not done or done <= mark_ms or done > now_ms - SETTLE_MS or created <= 0 or done < created:
                continue
            model = str(model or "unknown")[:64]
            model = "".join(ch for ch in model if ch.isalnum() or ch in "._:/@+-") or "unknown"
            h = hashlib.sha256(f"openhop-rank:{mid}".encode("utf-8")).hexdigest()[:24]
            first = _int(first)
            if first and not (created <= first <= done):
                first = 0
            out.append([h, created, first, done, model, _int(t_in), _int(t_out), _int(cr), _int(cw)])
    finally:
        conn.close()
    out.sort(key=lambda m: m[3])
    return out


def _hops_total() -> int:
    """Every new address tried. The ranking keeps this one, so nobody's number on the board goes down."""
    try:
        return max(0, int(json.loads(GUARD_STATE_FILE.read_text(encoding="utf-8")).get("hops", 0) or 0))
    except Exception:
        return 0


def _good_hops_total() -> int:
    """New addresses that really worked (shown apart; older guards don't count it yet: 0)."""
    try:
        return max(0, int(json.loads(GUARD_STATE_FILE.read_text(encoding="utf-8")).get("good_hops", 0) or 0))
    except Exception:
        return 0


# ---------------------------------------------------------------- public API (used by openhop_app.py)
def joined() -> bool:
    return bool(_read_state().get("handle")) and KEY_FILE.is_file()


def status() -> dict:
    st = _read_state()
    return {
        "joined": joined(),
        "handle": st.get("handle", ""),
        "paused": bool(st.get("paused")),
        "last_sync": st.get("last_sync", 0),
        "last_result": st.get("last_result", ""),
        "next_at": st.get("next_at", 0),
        "review": st.get("status") == "review",
    }


def join(handle: str) -> dict:
    with _lock:
        if joined():
            return {"ok": False, "code": "already", "message": "This PC is already in the ranking."}
        r = _call("POST", "/join", {"handle": str(handle or "")[:40], "label": _label()}, auth=False)
        if not r.get("ok") or not r.get("token"):
            return r
        _save_key(r["token"])
        _write_state({"handle": r.get("handle", handle), "account": r.get("account", ""), "device": r.get("device", ""),
                      "joined_at": int(time.time()), "next_at": 0})
        append_log(LOG_FILE, f"joined the ranking as @{r.get('handle', handle)}")
    sync(force=True)
    return {"ok": True, "handle": r.get("handle", handle)}


def link_claim(code: str) -> dict:
    with _lock:
        if joined():
            return {"ok": False, "code": "already", "message": "This PC is already in the ranking."}
        r = _call("POST", "/link/claim", {"code": str(code or "")[:20], "label": _label()}, auth=False)
        if not r.get("ok") or not r.get("token"):
            return r
        _save_key(r["token"])
        _write_state({"handle": r.get("handle", ""), "account": r.get("account", ""), "device": r.get("device", ""),
                      "joined_at": int(time.time()), "next_at": 0})
        append_log(LOG_FILE, f"linked this PC to @{r.get('handle', '')}")
    sync(force=True)
    return {"ok": True, "handle": r.get("handle", "")}


def link_start() -> dict:
    return _call("POST", "/link/start", {})


def board(kind: str = "nocache", period: str = "week") -> dict:
    kind = kind if kind in ("nocache", "cache", "ips") else "nocache"
    period = period if period in ("week", "all") else "week"
    return _call("GET", f"/board?kind={kind}&period={period}", auth=joined())


def me() -> dict:
    r = _call("GET", "/me")
    if r.get("ok"):
        st = _read_state()
        if r.get("handle") and r["handle"] != st.get("handle"):
            st["handle"] = r["handle"]
            _write_state(st)
    elif r.get("code") == "auth" and KEY_FILE.is_file():
        _forget_local()  # removed from another PC or the account is gone: this PC is out
    return r


def set_handle(handle: str) -> dict:
    r = _call("POST", "/handle", {"handle": str(handle or "")[:40]})
    if r.get("ok"):
        st = _read_state()
        st["handle"] = r.get("handle", handle)
        _write_state(st)
    return r


def remove_device(device_id: str) -> dict:
    r = _call("POST", "/device/remove", {"id": str(device_id or "")[:32]})
    if r.get("ok") and device_id == _read_state().get("device"):
        _forget_local()
    return r


def leave() -> dict:
    r = _call("POST", "/leave", {})
    if r.get("ok") or r.get("code") == "auth":
        _forget_local()
        append_log(LOG_FILE, "left the ranking")
        return {"ok": True}
    return r


def set_paused(paused: bool) -> dict:
    st = _read_state()
    st["paused"] = bool(paused)
    _write_state(st)
    return {"ok": True, "paused": bool(paused)}


def sync(force: bool = False, db: Path | None = None) -> dict:
    """Upload what's new (in batches) when it's time. Safe to call often."""
    with _lock:
        st = _read_state()
        if not joined() or (st.get("paused") and not force):
            return {"ok": False, "code": "off"}
        now = time.time()
        if not force and now < float(st.get("next_at", 0) or 0):
            return {"ok": True, "skipped": True}
        mark = int(st.get("mark", 0) or 0)
        total = {"msgs": 0, "nocache": 0, "cache": 0, "ips": 0}
        r: dict = {}
        for i in range(MAX_BATCHES_PER_SYNC):
            msgs = _messages_since(mark, int(time.time() * 1000), db)[:BATCH]
            r = _call("POST", "/upload", {"msgs": msgs, "hops": _hops_total()})
            if not r.get("ok"):
                break
            for k in total:
                total[k] += int((r.get("accepted") or {}).get(k, 0) or 0)
            got = r.get("mark")
            # 0 is a real answer ("start from the beginning"), not a missing one.
            new_mark = int(got) if isinstance(got, int) and not isinstance(got, bool) and got >= 0 else mark
            if len(msgs) < BATCH or new_mark <= mark:
                mark = new_mark
                break
            mark = new_mark
        st = _read_state()
        if r.get("ok"):
            st["mark"] = mark
            st["status"] = r.get("status", "ok")
            st["last_sync"] = int(time.time())
            st["last_result"] = "ok"
            st["next_at"] = time.time() + max(300, min(24 * 3600, int(r.get("next_in", 7200) or 7200)))
            append_log(LOG_FILE, f"uploaded {total['msgs']} messages, {total['nocache']} tokens, {total['ips']} new IPs")
        else:
            code = r.get("code", "error")
            st["last_result"] = code
            st["next_at"] = time.time() + (900 if code in ("offline", "unavailable", "busy") else 3600)
            if code == "auth":
                _forget_local()
                return r
        _write_state(st)
        return {"ok": bool(r.get("ok")), "accepted": total, "code": r.get("code", "")}


def start_background() -> None:
    """Checks every 5 minutes whether an upload is due (the server sets the pace)."""
    def loop() -> None:
        time.sleep(30)
        while True:
            try:
                sync()
            except Exception as exc:
                append_log(LOG_FILE, f"sync error: {exc}")
            time.sleep(300)

    threading.Thread(target=loop, name="openhop-rank", daemon=True).start()


# ---------------------------------------------------------------- fun stats (local only)
def _hour_kind(hour: int) -> str:
    if hour < 6:
        return "owl"
    if hour < 10:
        return "early"
    if hour < 19:
        return "office"
    return "evening"


def fun_stats(db: Path | None = None) -> dict:
    """Personal stats from this PC's OpenCode history (free provider). Cached for 5 minutes."""
    global _stats_cache
    if db is None and _stats_cache and time.time() - _stats_cache[0] < STATS_CACHE_S:
        return _stats_cache[1]
    out = {"ok": False}
    try:
        conn = _connect(db)
    except Exception:
        conn = None
    if conn is None:
        return out
    days: dict = defaultdict(int)
    hours: Counter = Counter()
    models: Counter = Counter()
    speeds: list[float] = []
    longest = 0
    t_in = t_out = t_cr = t_cw = 0
    msgs = 0
    week_start = (datetime.now() - timedelta(days=datetime.now().weekday())).date()
    week_nocache = 0
    try:
        for (_mid, prov, model, created, first, done, i, o, cr, cw) in conn.execute(_MSG_SQL, (0,)):
            if prov != PROVIDER:
                continue
            i, o, cr, cw = _int(i), _int(o), _int(cr), _int(cw)
            done, first = _int(done), _int(first)
            if not done:
                continue
            msgs += 1
            t_in, t_out, t_cr, t_cw = t_in + i, t_out + o, t_cr + cr, t_cw + cw
            local = datetime.fromtimestamp(done / 1000)
            days[local.date()] += i + o
            hours[local.hour] += 1
            models[str(model or "unknown")] += i + o
            longest = max(longest, o)
            if first and done > first and o > 50:
                speeds.append(o / ((done - first) / 1000))
            if local.date() >= week_start:
                week_nocache += i + o
    except Exception as exc:
        append_log(LOG_FILE, f"stats error: {exc}")
        return out
    finally:
        conn.close()
    if not msgs:
        return {"ok": True, "empty": True, "hops": _hops_total(), "good_hops": _good_hops_total()}
    today = datetime.now().date()
    streak = 0
    d = today if today in days else today - timedelta(days=1)
    while d in days:
        streak += 1
        d -= timedelta(days=1)
    best_day = max(days.items(), key=lambda kv: kv[1])
    fav_hour = hours.most_common(1)[0][0]
    speeds.sort()
    nocache = t_in + t_out
    out = {
        "ok": True,
        "empty": False,
        "msgs": msgs,
        "nocache": nocache,
        "cache": nocache + t_cr + t_cw,
        "week_nocache": week_nocache,
        "cache_pct": round(100 * t_cr / max(1, t_in + t_cr + t_cw)),
        "speed": round(speeds[len(speeds) // 2]) if speeds else 0,
        "longest": longest,
        "streak": streak,
        "days_active": len(days),
        "best_day": {"date": best_day[0].isoformat(), "tokens": best_day[1]},
        "fav_hour": fav_hour,
        "hour_kind": _hour_kind(fav_hour),
        "fav_model": models.most_common(1)[0][0],
        "quijotes": round(nocache / QUIJOTE_TOKENS, 1),
        "hops": _hops_total(),
        "good_hops": _good_hops_total(),
    }
    if db is None:
        _stats_cache = (time.time(), out)
    return out

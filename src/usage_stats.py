"""Local-only usage counters, adapted from upstream openhop's usage_stats.py.

The upstream module sends an anonymous daily summary to openhop.cfd. This Mac port
keeps the event model but never uploads: all data stays in guard/usage_days.json.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from ouc_common import GUARD_DIR, opencode_db_path

USAGE_FILE = GUARD_DIR / "usage_days.json"

EVENTS = ("hop", "good", "spent", "untested", "rewall", "limit", "cont", "pause")
MODES = ("warp", "tor", "vpn", "custom")
BACK_BUCKETS = ((5 * 60, "5m"), (30 * 60, "30m"), (2 * 3600, "2h"),
                (6 * 3600, "6h"), (12 * 3600, "12h"), (10 ** 9, "more"))
KEEP_DAYS = 10
MAX_PER_HOUR = 100_000
PROVIDER = "opencode"

_lock = threading.Lock()


def utc_day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts, timezone.utc).strftime("%Y-%m-%d")


def day_start(day: str) -> float:
    return datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()


def _read() -> dict:
    try:
        data = json.loads(USAGE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _write(data: dict) -> None:
    try:
        USAGE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = USAGE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")
        tmp.replace(USAGE_FILE)
    except Exception:
        pass


def _prune(data: dict, now: float) -> None:
    oldest = utc_day(now - KEEP_DAYS * 86400)
    for day in [d for d in (data.get("days") or {}) if d < oldest]:
        data["days"].pop(day, None)


def note(event: str, mode: str = "", n: int = 1, back_in: float | None = None,
         now: float | None = None) -> None:
    """Count one event in the current UTC hour. Never raises — the guard must keep running."""
    if event not in EVENTS or n <= 0:
        return
    try:
        now = time.time() if now is None else now
        hour = datetime.fromtimestamp(now, timezone.utc).hour
        with _lock:
            data = _read()
            day = data.setdefault("days", {}).setdefault(utc_day(now), {})
            arr = day.setdefault("h", {}).setdefault(event, [0] * 24)
            arr[hour] = min(MAX_PER_HOUR, int(arr[hour]) + int(n))
            if event == "hop" and mode in MODES:
                modes = day.setdefault("modes", {})
                modes[mode] = min(MAX_PER_HOUR, int(modes.get(mode, 0)) + int(n))
            if event == "spent" and back_in and back_in > 0:
                label = next(lbl for lim, lbl in BACK_BUCKETS if back_in <= lim)
                back = day.setdefault("back", {})
                back[label] = min(MAX_PER_HOUR, int(back.get(label, 0)) + 1)
            _prune(data, now)
            _write(data)
    except Exception:
        pass


_MSG_SQL = """
SELECT session_id, json_extract(data, '$.model.providerID'), json_extract(data, '$.time.completed'),
json_extract(data, '$.tokens.input'), json_extract(data, '$.tokens.output')
FROM session_message
WHERE time_updated >= ? AND json_type(data, '$.tokens') = 'object'
"""


def _num(v) -> int:
    try:
        n = int(v or 0)
        return n if n > 0 else 0
    except Exception:
        return 0


def messages_of_day(day: str, db: Path | None = None) -> dict:
    """Local counts only: OpenCode answers, chats and tokens for a UTC day."""
    start = int(day_start(day) * 1000)
    end = start + 86_400_000
    out = {"msgs": [0] * 24, "chats": 0, "max_chats_hour": 0, "tok": 0}
    path = Path(db) if db else opencode_db_path()
    if not path.is_file():
        return out
    chats = set()
    per_hour = [set() for _ in range(24)]
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=2.0)
        conn.execute("PRAGMA query_only = ON")
        for sid, prov, done, tin, tout in conn.execute(_MSG_SQL, (start,)):
            done = _num(done)
            if prov != PROVIDER or not (start <= done < end):
                continue
            h = (done - start) // 3_600_000
            out["msgs"][h] += 1
            out["tok"] += _num(tin) + _num(tout)
            if sid:
                chats.add(str(sid))
                per_hour[h].add(str(sid))
        conn.close()
    except Exception:
        pass
    out["chats"] = len(chats)
    out["max_chats_hour"] = max((len(s) for s in per_hour), default=0)
    return out


def summary(day: str | None = None, db: Path | None = None) -> dict:
    """Current local totals for the control center. No network call is made."""
    day = day or utc_day()
    data = _read()
    d = (data.get("days") or {}).get(day) or {}
    hours = {k: [int(x) for x in v][:24] for k, v in (d.get("h") or {}).items() if k in EVENTS}
    try:
        m = messages_of_day(day, db)
    except Exception:
        m = {"msgs": [0] * 24, "chats": 0, "tok": 0}
    return {
        "day": day,
        "events": {k: sum(v) for k, v in hours.items()},
        "hours": hours,
        "modes": d.get("modes") or {},
        "back": d.get("back") or {},
        "messages": sum(m["msgs"]),
        "chats": m["chats"],
        "tokens": m["tok"],
    }

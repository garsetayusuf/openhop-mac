"""Zen Guard for Windows: watches OpenCode's log for quota walls and rotates /64."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

# Ensure local script and bundled directories are resolvable
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
_SCRIPTS = Path(os.environ.get("LOCALAPPDATA", "")) / "openhop" / "scripts"
if _SCRIPTS.is_dir() and str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

from ouc_common import (
    DEST_DIR,
    GUARD_LOG_FILE,
    GUARD_STATE_FILE,
    MUTEX_GUARD,
    OPENCODE_LOG_FILE,
    acquire_mutex,
    opencode_db_path,
    append_log,
    ensure_dirs,
    find_warp_cli,
    get_egress_v6,
    get_warp_local_v6,
    hop_once,
    bump_egress_epoch,
    keeper_pause_ctx,
    next_free_reset,
    normalize_v6_64,
    trace_via_socks,
    warp_path_leaking,
)

POLL_SECONDS = 10
COOLDOWN_SECONDS = 75
# If the limit comes back this soon after a new IP, that IP was already used up (shared addresses: WARP,
# Tor and proxy IPs are used by many people, and each one only has so many free messages per day).
# Then we look for the next address right away instead of waiting the normal cooldown, up to
# FAILED_HOPS_MAX in a row, and only then pause.
REWALL_SECONDS = 180
FAILED_HOPS_MAX = 15  # at busy hours most shared WARP /64s are already used up (seen: 7 in a row)
HUNT_GAP_SECONDS = 12   # minimum gap between two quick tries (lets the 'continue' come back)
BACKOFF_SECONDS = 3 * 60
TOR_TRIES = 4
HEARTBEAT_FILE = GUARD_LOG_FILE.parent / "heartbeat.txt"
KICK_FILE = GUARD_LOG_FILE.parent / "kick"   # written by the app's "Try now" / "Try Tor": end the pause now
RESCUE_FILE = GUARD_LOG_FILE.parent / "rescue"  # written by the app's "chats are stopped at the limit" fix
SETTINGS_FILE = DEST_DIR / "app_settings.json"  # read directly: the worker must not import the UI modules


def _usage(event: str, n: int = 1, **kw) -> None:
    """Count one usage event locally (guard/usage_days.json). Never raises, never uploads."""
    try:
        import usage_stats
        usage_stats.note(event, n=n, **kw)
    except Exception:
        pass


def _heartbeat() -> None:
    """Cheap liveness signal the app checks: a running but hung guard stops writing this."""
    try:
        HEARTBEAT_FILE.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass
MAX_HOP_TRIES = 8
BLACKLIST_MAX = 200
MAX_READ_BYTES = 50_000_000


def _default_state(offset: int = 0) -> dict:
    return {
        "blacklist": [],
        "offset": offset,
        "last_hop": 0,
        "hops": 0,
        "current": "",
        "pending": {},   # chats that hit the limit and still need a 'continue': {sessionID: first seen}
        "resumed": {},   # when each chat last got a 'continue' from us: {sessionID: time}
        "failed_hops": 0,   # new IPs that ran into the limit again almost immediately
        "backoff_until": 0, # while set, don't rotate: it would just loop
        "hop_counted": 0,   # last rotation already counted as a failed hop
        "backoff_logged": 0,
        "pause_started": 0, # when the current rotation pause began
        "day_key": 0,       # next_free_reset() of the free-limit day day_hops counts
        "day_hops": 0,      # new addresses tried since the last daily reset (for the "Used IPs" summary)
    }


def _count_new_ip(state: dict, now: float | None = None) -> None:
    """Count one more new address for today's summary. "Today" = since the last daily reset (~01:05 UTC)."""
    _usage("hop", mode=_egress_mode())
    day = int(next_free_reset(now))
    if int(state.get("day_key", 0) or 0) != day:
        state["day_key"] = day
        state["day_hops"] = 0
    state["day_hops"] = int(state.get("day_hops", 0) or 0) + 1


def _count_good_ip(state: dict, verified: bool) -> None:
    """One more new address that really worked: what the app and the ranking show as "new IPs" (state "hops"
    counts every address tried, used-up ones too). Once per address. verified=False (no IP test: the setting is
    off, or the test couldn't tell) is taken back if the limit comes back right after this IP (_note_wall)."""
    hop = float(state.get("last_ok_hop", 0) or 0)
    if float(state.get("good_hop_at", 0) or 0) != hop:
        state["good_hop_at"] = hop
        state["good_hops"] = int(state.get("good_hops", 0) or 0) + 1
        _usage("good" if verified else "untested")
        if not verified:
            state["good_unverified"] = hop
            return
    if verified:
        state.pop("good_unverified", None)


def _uncount_unverified_ip(state: dict) -> None:
    """The limit came back right after the IP change: an untested address didn't work after all."""
    hop = float(state.get("good_unverified", 0) or 0)
    if hop and hop == float(state.get("last_ok_hop", 0) or 0):
        state["good_hops"] = max(0, int(state.get("good_hops", 0) or 0) - 1)
        state.pop("good_unverified", None)
        state.pop("good_hop_at", None)


def _backfill_good_hops(state: dict) -> None:
    """Guards older than this didn't count the addresses that worked: estimate it once from guard.log
    (every address tried, minus the ones the limit came back on right away)."""
    if "good_hops" in state:
        return
    failed = 0
    try:
        with GUARD_LOG_FILE.open("r", encoding="utf-8", errors="replace") as f:
            failed = sum(1 for line in f if "after the last IP change (failed attempt" in line)
    except OSError:
        pass
    state["good_hops"] = max(0, int(state.get("hops", 0) or 0) - failed)
    _save_state(state)


def _load_state() -> dict:
    if not GUARD_STATE_FILE.is_file():
        offset = OPENCODE_LOG_FILE.stat().st_size if OPENCODE_LOG_FILE.is_file() else 0
        state = _default_state(offset)
        _save_state(state)
        return state
    try:
        data = json.loads(GUARD_STATE_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return {**_default_state(), **data}
    except Exception:
        pass
    return _default_state()


def _save_state(state: dict) -> None:
    try:
        GUARD_STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    except Exception:
        pass


PENDING_MAX_AGE = 30 * 60   # forget chats that hit the limit longer ago than this
STUCK_LOOKBACK = 30 * 60    # at start-up, chats that ended on the free limit this recently still need help


def _stuck_sessions(max_age: int = STUCK_LOOKBACK, min_age: int = 0) -> list[str]:
    """Chats whose last run ended on the free limit in the last max_age seconds.

    The guard only watches opencode.log for *new* lines, so a limit hit while it wasn't running (openhop
    installed on a PC that was already at the limit, or during an update/restart) was never fixed: the
    chats stayed stuck and the panel said "Everything is working". OpenCode writes an 'idle' row when a
    run ends; if a chat's latest one is the free-limit error, that chat is still waiting.
    """
    db = opencode_db_path()
    if not db.is_file():
        return []
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        try:
            rows = con.execute(
                # The chat's very latest row (any type) must be that 'idle': if anything came after it,
                # a new run already started (a 'continue' went through) and the chat isn't stuck.
                "SELECT m.session_id, m.data FROM session_message m JOIN ("
                "  SELECT session_id, MAX(seq) AS s FROM session_message WHERE session_id IN ("
                "    SELECT DISTINCT session_id FROM session_message WHERE type = 'idle' AND time_created >= ?"
                "  ) GROUP BY session_id"
                ") last ON m.session_id = last.session_id AND m.seq = last.s"
                " WHERE m.type = 'idle' AND m.time_created >= ? AND m.time_created <= ?"
                # archived chats (put away by the user, or openhop's own test runs) don't need help
                " AND m.session_id NOT IN (SELECT id FROM session_v2 WHERE time_archived IS NOT NULL)",
                ((time.time() - max_age) * 1000, (time.time() - max_age) * 1000,
                 (time.time() - min_age) * 1000)).fetchall()
        finally:
            con.close()
    except Exception:
        return []  # older OpenCode without this table: nothing to do
    out = [str(sid) for sid, data in rows if sid and "provider.free-limit" in str(data or "")]
    if not _chat_move_enabled():
        return out        # chats moved earlier still get help like any other chat
    try:
        import chat_handoff as ch
        out = [sid for sid in out if not ch.already_moved(ch.root_chat(sid))]   # its work goes on in a new chat
    except Exception:
        pass
    return out


def _rescue_stuck_at_start(state: dict, warp_bin: str | None, asked: bool = False) -> None:
    """Fix a free limit that happened before the guard was running (see _stuck_sessions).

    asked=True: the user pressed the fix for "chats are stopped at the free limit" in the app. Then the
    normal cooldown doesn't apply and a rotation pause is ended, like "Try now".
    """
    try:
        now = int(time.time())
        if _maintenance_block() or _egress_mode() == "vpn":
            return
        if asked:
            if now < int(state.get("backoff_until", 0) or 0):
                state["backoff_until"] = 0
                state["failed_hops"] = 0
                state["backoff_logged"] = 0
                state["hop_counted"] = 0
        elif now < int(state.get("backoff_until", 0) or 0):
            return
        stuck = _stuck_sessions()
        if not asked and stuck and now - int(state.get("last_hop", 0) or 0) < COOLDOWN_SECONDS:
            _move_limited_chats(state, stuck, now)
            return
        if not stuck:
            if asked:
                append_log(GUARD_LOG_FILE, "asked to help chats stopped at the free limit, but none are stopped now")
            return
        if asked:
            append_log(GUARD_LOG_FILE, f"the user asked to help {len(stuck)} chat(s) stopped at the free limit — "
                                       f"getting a new IP and sending them 'continue'")
        else:
            append_log(GUARD_LOG_FILE, f"{len(stuck)} chat(s) were already stuck on the free limit when the guard "
                                       f"started — getting a new IP and sending them 'continue'")
        stuck, handled = _move_limited_chats(state, stuck, now)
        if handled and not stuck:
            return
        pending = state.setdefault("pending", {})
        for sid in stuck:
            pending.setdefault(sid, now)
        _save_state(state)
        _rotate(state, warp_bin)
    except Exception as exc:
        append_log(GUARD_LOG_FILE, f"start-up check failed: {exc}")


def _resumable(state: dict) -> list[str]:
    """Chats that hit the limit and haven't had a 'continue' since the last new IP."""
    last_hop = float(state.get("last_ok_hop", 0) or 0)
    resumed = state.get("resumed") or {}
    try:
        import ip_probe
        ours = set(ip_probe.test_chats())   # openhop's own IP-test chat never gets 'continue'
    except Exception:
        ours = set()
    sids = [sid for sid in (state.get("pending") or {})
            if sid not in ours and float(resumed.get(sid, 0) or 0) < last_hop]
    archived = _archived_chats(sids)
    return [sid for sid in sids if sid not in archived]


def _archived_chats(sids: list[str]) -> set[str]:
    """The chats among sids that are archived: put away by the user, or test runs (`opencode run --archived`,
    such as openhop's own tests). They never get 'continue': once 25 test chats got one each, and every
    'continue' spends a free message of the new IP. If the database can't be read, none are left out."""
    if not sids:
        return set()
    db = opencode_db_path()
    if not db.is_file():
        return set()
    try:
        import sqlite3
        con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
        try:
            marks = ",".join("?" * len(sids))
            rows = con.execute(f"SELECT id FROM session_v2 WHERE time_archived IS NOT NULL AND id IN ({marks})",
                               list(sids)).fetchall()
        finally:
            con.close()
    except Exception:
        return set()
    return {str(r[0]) for r in rows}


def _prune(state: dict) -> None:
    cutoff = time.time() - PENDING_MAX_AGE
    for key in ("pending", "resumed"):
        state[key] = {k: v for k, v in (state.get(key) or {}).items() if float(v or 0) >= cutoff}


def _dispatch_auto_resume(state: dict) -> None:
    """After a new IP: send 'continue' to every chat (tab) that hit the limit, not just the latest one."""
    if _egress_mode() == "vpn":
        return
    try:
        from auto_resume import resume_active_session, is_auto_resume_enabled, clear_opencode_free_limit

        # Always clear client-side free-limit locks immediately upon rotation
        clear_opencode_free_limit()

        sids = _resumable(state)
        if is_auto_resume_enabled():
            time.sleep(3.0)  # the proxy closes OpenCode's old tunnels within ~2 s of the IP change
            verdict = _test_ip_first(state, sids)
            if verdict == "spent":
                _usage("spent")
                # The new IP is already used up: the user's chats get nothing (no pile of 'continue'). They stay
                # waiting. If this address gets its messages back very soon, wait for it (a new random address
                # is usually used up too, and this one would be lost); otherwise the main loop looks for the
                # next address (see _ip_known_spent).
                if _wait_for_back(state):
                    return
                append_log(GUARD_LOG_FILE, "trying the next address")
                state["probe_spent"] = float(state.get("last_ok_hop", 0) or 0)
                _save_state(state)
                return
            _count_good_ip(state, verified=verdict == "works")
            ok, res_msg = resume_active_session(sids or None)
            if ok:
                _usage("cont", n=len(sids) or 1)
            dispatch_status = "request started" if ok else "request not started"
            append_log(GUARD_LOG_FILE, f"[auto-resume] {dispatch_status}: {res_msg}")
        else:
            _count_good_ip(state, verified=False)
        now = time.time()
        for sid in sids:
            state.setdefault("resumed", {})[sid] = now
            (state.get("pending") or {}).pop(sid, None)
        _prune(state)
        _save_state(state)
    except Exception as exc:
        append_log(GUARD_LOG_FILE, f"[auto-resume] note: {exc}")


# ---------------------------------------------------------------- test the new IP in openhop's own chat first
# Since 2026-11-16 almost every new shared IP arrives already used up, so 'continue' after each new IP piled
# dozens of 'continue' messages into the user's chats. Now openhop first sends one small message to its own hidden
# chat (ip_probe.py) and only sends 'continue' when that chat got an answer. Setting "test_ip_first" (on by default).

def _test_ip_first_enabled() -> bool:
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return bool(data.get("test_ip_first", True)) if isinstance(data, dict) else True
    except Exception:
        return True


def _probe_model(sids: list[str]) -> str:
    """The model of the first waiting chat (or the latest chat), as provider/model#variant."""
    try:
        import chat_handoff as ch
        for sid in sids:
            info = ch.chat_info(ch.root_chat(sid)) or {}
            if info.get("model"):
                return str(info["model"])
        from auto_resume import get_latest_session_info
        latest = get_latest_session_info()
        if latest:
            return str((ch.chat_info(latest[0]) or {}).get("model") or "")
    except Exception:
        pass
    return ""


def _test_ip_first(state: dict, sids: list[str]) -> str:
    """'works', 'spent' or 'unknown' ('unknown' = send 'continue' as before 3.8)."""
    if not _test_ip_first_enabled():
        return "unknown"
    try:
        import ip_probe
    except Exception as exc:
        append_log(GUARD_LOG_FILE, f"IP test unavailable ({exc}) — sending 'continue' directly")
        return "unknown"
    _heartbeat()
    verdict, note = ip_probe.check_ip(_probe_model(sids), ip=str(state.get("current") or ""))
    _heartbeat()
    n = len(sids) or 1
    if verdict == "works":
        append_log(GUARD_LOG_FILE, f"tested the new IP in openhop's own hidden chat: it answered ✓ — "
                                   f"sending 'continue' to {n} chat(s)")
    elif verdict == "spent":
        append_log(GUARD_LOG_FILE, f"tested the new IP in openhop's own hidden chat: {note} — your chats "
                                   f"get no 'continue' on this IP yet")
    else:
        append_log(GUARD_LOG_FILE, f"couldn't test the new IP ({note}) — sending 'continue' directly")
    return verdict


WAIT_FOR_BACK_SECONDS = 150   # an address whose messages come back within this long is waited for, not skipped
BACK_MARGIN_SECONDS = 5


def _wait_for_back(state: dict, now: float | None = None) -> bool:
    """The test found this address used up, but it gets its messages back within WAIT_FOR_BACK_SECONDS: stay on
    it and test it again then (the main loop calls _check_wait_for_back). True = waiting."""
    now = time.time() if now is None else now
    try:
        import ip_probe
        back = ip_probe.last_back_at()
    except Exception:
        back = 0.0
    if not back or not 0 < back - now <= WAIT_FOR_BACK_SECONDS:
        return False
    state["wait_back_until"] = back + BACK_MARGIN_SECONDS
    state["wait_back_hop"] = float(state.get("last_ok_hop", 0) or 0)
    state.pop("probe_spent", None)
    _save_state(state)
    append_log(GUARD_LOG_FILE, f"this address gets its free messages back in {int(back - now) + 1}s — waiting for "
                               f"it instead of skipping it, then testing it again")
    return True


def _check_wait_for_back(state: dict, now: float, sessions: list[str]) -> bool:
    """While waiting for the current address to come back: hold rotation (True). When the time is up, test it
    again: 'continue' if it answers, otherwise the usual search for the next address."""
    until = float(state.get("wait_back_until", 0) or 0)
    if not until:
        return False
    if float(state.get("wait_back_hop", 0) or 0) != float(state.get("last_ok_hop", 0) or 0):
        # The IP changed meanwhile (the user pressed "Get a new IP", ...): nothing to wait for any more.
        state.pop("wait_back_until", None)
        state.pop("wait_back_hop", None)
        _save_state(state)
        return False
    if sessions:
        pending = state.setdefault("pending", {})
        for sid in sessions:
            pending.setdefault(sid, now)
        _save_state(state)
    if now < until:
        return True
    state.pop("wait_back_until", None)
    state.pop("wait_back_hop", None)
    _save_state(state)
    append_log(GUARD_LOG_FILE, "the address should have its free messages back now — testing it again")
    _dispatch_auto_resume(state)
    return True


def _ip_known_spent(state: dict) -> bool:
    """The test chat found the current IP already used up (and no newer IP was set since)."""
    spent = float(state.get("probe_spent", 0) or 0)
    return bool(spent) and spent == float(state.get("last_ok_hop", 0) or 0)


# ---------------------------------------------------------------- per-chat limit: move the work to a new chat
# OFF in 3.8 (chat_handoff.AVAILABLE): the 2026-11-19 analysis of logs and reports showed real chats hit the
# per-IP cap, not a per-chat one (docs/research-chat-limit/2026-11-19-findings-per-ip.md). Original idea:
# Since 2026-11-16 OpenCode counts the free limit per chat: a chat that hit it stays blocked on every new IP,
# while a new chat on the same IP works. So the first answer to a limit is a new chat that carries on
# (chat_handoff.py). A new IP is only needed when a chat openhop JUST opened hits the limit too.
CHAT_MOVE_FRESH = 150        # a chat openhop opened less than this ago that hits the limit -> it's the IP
CHAT_MOVES_PER_HOUR = 10     # safety cap


def _chat_move_enabled() -> bool:
    try:
        import chat_handoff
        if not chat_handoff.AVAILABLE:
            return False
    except Exception:
        return False
    try:
        raw = json.loads((DEST_DIR / "app_settings.json").read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "new_chat_on_limit" in raw:
            return bool(raw["new_chat_on_limit"])
    except Exception:
        pass
    return True


def _move_limited_chats(state: dict, sessions: list[str], now: int) -> tuple[list[str], int]:
    """Move each limited chat to a new chat. Returns (chats that still need a new IP, how many were handled)."""
    if not sessions or not _chat_move_enabled():
        return sessions, 0
    try:
        import chat_handoff as ch
        from auto_resume import is_auto_resume_enabled
    except Exception as exc:
        append_log(GUARD_LOG_FILE, f"new-chat step unavailable: {exc}")
        return sessions, 0
    if not is_auto_resume_enabled():
        return sessions, 0
    moves = [t for t in (state.get("chat_moves_at") or []) if now - float(t) < 3600]
    left: list[str] = []
    handled = 0
    for sid in dict.fromkeys(s for s in sessions if s):
        root = ch.root_chat(sid)
        if ch.already_moved(root):
            handled += 1          # its work already continues in a new chat
            (state.get("pending") or {}).pop(sid, None)
            continue
        came = ch.moved_from(root)
        if (came and now - float(came.get("at", 0) or 0) < CHAT_MOVE_FRESH) or ch.answers(root) == 0:
            # A chat openhop opened hit the limit right away, or any chat hit it before its first answer: its
            # own quota is untouched, so the address is used up -> new IP, not another new chat.
            left.append(sid)
            continue
        if len(moves) >= CHAT_MOVES_PER_HOUR:
            if now - int(state.get("chat_move_cap_logged", 0) or 0) > 600:
                state["chat_move_cap_logged"] = now
                append_log(GUARD_LOG_FILE, f"already opened {len(moves)} new chats in the last hour — "
                                           "not opening more for now (trying a new IP instead)")
            left.append(sid)
            continue
        new, note = ch.move_to_new_chat(root)
        if new:
            moves.append(now)
            handled += 1
            (state.get("pending") or {}).pop(sid, None)
            append_log(GUARD_LOG_FILE, f"chat {root[:12]}… hit the free limit (OpenCode now limits each chat) — "
                                       f"opened a new chat {new[:12]}… that carries on with its work ✓")
        else:
            append_log(GUARD_LOG_FILE, f"couldn't open a new chat for {root[:12]}…: {note} — trying a new IP")
            left.append(sid)
    state["chat_moves_at"] = moves
    _save_state(state)
    return left, handled


def _blacklist_add(state: dict, addr: str) -> None:
    """Remember a used-up address until OpenCode's daily reset (a bit after 01:00 UTC)."""
    prefix = normalize_v6_64(addr)
    if not prefix:
        return
    now = time.time()
    until = {k: v for k, v in (state.get("burnt_until") or {}).items() if float(v or 0) > now}
    until.pop(prefix, None)   # re-insert at the end: the list stays newest-last
    until[prefix] = next_free_reset(now)
    state["burnt_until"] = dict(list(until.items())[-BLACKLIST_MAX:])
    state["blacklist"] = list(state["burnt_until"].keys())


def _is_burnt(state: dict, addr: str) -> bool:
    """Used up today: by this PC, or (Turbo) by another openhop user on the shared list."""
    prefix = normalize_v6_64(addr)
    if not prefix:
        return False
    if float((state.get("burnt_until") or {}).get(prefix, 0) or 0) > time.time():
        return True
    return _turbo_listed(addr)


# ---- openhop Turbo (src/turbo.py): the shared list of exits other openhop users already used up today.
_turbo_noted: dict = {}


def _turbo_listed(addr: str) -> bool:
    """On the shared list? Counts and logs each skipped address once an hour."""
    try:
        import turbo
        if not turbo.is_listed(addr):
            return False
        key = turbo.canon(addr)
    except Exception:
        return False
    now = time.time()
    if key and now - _turbo_noted.get(key, 0.0) > 3600:
        _turbo_noted[key] = now
        try:
            turbo.note_skip()
        except Exception:
            pass
        append_log(GUARD_LOG_FILE, f"Turbo: {addr} was already used up today by another openhop user — skipping it")
    return True


def _turbo_report(mode: str, addr: str) -> None:
    """Share this used-up exit (sent through the exit itself) and refresh the shared list. Never raises."""
    try:
        import turbo
        if not turbo.enabled() or not addr:
            return
        ok, why = turbo.report_hit(mode, addr)
        if ok:
            append_log(GUARD_LOG_FILE, f"Turbo: shared that {addr} is used up ({why})")
        else:
            append_log(GUARD_LOG_FILE, f"Turbo: couldn't share {addr} ({why})")
            turbo.sync(mode)
    except Exception as exc:
        append_log(GUARD_LOG_FILE, f"Turbo: note: {exc}")


def _turbo_sync(mode: str) -> None:
    try:
        import turbo
        if turbo.enabled():
            turbo.sync(mode)
    except Exception:
        pass


def _tor_used_up_exits(state: dict, now: float | None = None) -> list[str]:
    """Today's used-up IPv4 addresses that haven't reset yet, newest last (Tor exits are IPv4).

    With Turbo on, the relays of openhop's own Tor that are on the shared list come first (so this PC's own
    ones, the surest, are the newest and survive the TOR_EXCLUDE_MAX cut).
    """
    now = time.time() if now is None else now
    own = [a for a, until in (state.get("burnt_until") or {}).items()
           if a and ":" not in a and float(until or 0) > now]
    shared: list[str] = []
    try:
        import turbo
        if turbo.enabled():
            from tor_manager import tor_relay_addresses
            shared = [a for a in tor_relay_addresses() if a not in own and turbo.is_listed(a, now)]
    except Exception:
        shared = []
    return shared + own


def _tor_skip_used_up(state: dict) -> int:
    """Tell openhop's own Tor not to pick exits we already know are used up today.

    Returns how many it was told to skip (0 = not applied: a Tor Browser / system Tor, or nothing to skip).
    """
    try:
        from tor_manager import set_tor_exclude_exits
        done, count = set_tor_exclude_exits(_tor_used_up_exits(state))
        return count if done else 0
    except Exception:
        return 0


# When OpenCode's free messages come back. The 429 FreeUsageLimitError rows in opencode.db carry
# retryAfterMs: the reset is time_created + retryAfterMs (each address between 00:00 and 01:00 UTC).
_back_cache: dict = {"at": 0.0, "value": None}


def free_limit_back(now: float | None = None) -> dict:
    """{"at": epoch, "to": 0} from the newest free-limit error, or the daily window when there's none:
    {"at": next 00:00 UTC, "to": next 01:00 UTC}. Cached for a minute (the app polls this)."""
    now = time.time() if now is None else now
    cached = _back_cache.get("value")
    if cached and now - float(_back_cache.get("at", 0) or 0) < 60 and float(cached["to"] or cached["at"]) > now:
        return dict(cached)
    value = None
    db = opencode_db_path()
    if db.is_file():
        try:
            import sqlite3
            con = sqlite3.connect(f"file:{db.as_posix()}?mode=ro", uri=True, timeout=3.0)
            try:
                rows = con.execute(
                    "SELECT time_created, data FROM session_message WHERE data LIKE '%provider.free-limit%' "
                    "ORDER BY time_created DESC LIMIT 5").fetchall()
            finally:
                con.close()
            for created, data in rows:
                try:
                    d = json.loads(data)
                    err = d.get("error") or {}
                    ms = float(err.get("retryAfterMs", d.get("retryAfterMs")) or 0)
                    at = float(created or 0) / 1000 + ms / 1000
                except Exception:
                    continue
                if ms > 0 and now < at < now + 26 * 3600:
                    value = {"at": int(at), "to": 0}
                    break
        except Exception:
            pass
    if value is None:
        day = int(now // 86400) * 86400
        if now >= day + 3600:
            day += 86400
        value = {"at": max(day, int(now)), "to": day + 3600}   # already inside the window: from now on
    _back_cache.update(at=now, value=value)
    return dict(value)


def used_up_line(now: float | None = None) -> str:
    """Plain words for the guard log when every address is used up: when they come back, in local time."""
    back = free_limit_back(now)
    clock = time.strftime("%H:%M", time.localtime(back["at"]))
    when = (f"between {clock} and {time.strftime('%H:%M', time.localtime(back['to']))}" if back["to"]
            else f"around {clock}")
    return (f"every address tried today is used up. OpenCode refills them {when} (your time). "
            f"openhop keeps trying.")


def _note_wall(state: dict, now: int) -> bool:
    """Track walls that return right after a new IP and pause rotation when it's clearly a loop.

    Returns True while auto-rotation is paused (the caller should not rotate or resume).
    """
    backoff_until = int(state.get("backoff_until", 0) or 0)
    if backoff_until and now >= backoff_until:
        state["backoff_until"] = 0
        state["failed_hops"] = 0
        append_log(GUARD_LOG_FILE, "rotation pause is over — trying again on the next limit")
    if now < int(state.get("backoff_until", 0) or 0):
        if now - int(state.get("backoff_logged", 0) or 0) >= 60:
            state["backoff_logged"] = now
            _save_state(state)
            left = max(0, (int(state["backoff_until"]) - now) // 60)
            append_log(
                GUARD_LOG_FILE,
                f"limit seen again, but auto-rotation is paused for ~{left} more min "
                f"(new IPs kept hitting the limit immediately)",
            )
        return True

    hops_max = _hops_max()
    state["hops_max"] = hops_max   # the app shows "address N of up to <this>"
    last_hop = int(state.get("last_ok_hop", 0) or 0)
    if last_hop and now - last_hop <= REWALL_SECONDS:
        if int(state.get("hop_counted", 0) or 0) != last_hop:
            state["hop_counted"] = last_hop
            state["failed_hops"] = int(state.get("failed_hops", 0) or 0) + 1
            _usage("rewall")
            _uncount_unverified_ip(state)
            append_log(
                GUARD_LOG_FILE,
                f"limit came back {now - last_hop}s after the last IP change "
                f"(failed attempt {state['failed_hops']} of {hops_max})",
            )
    elif last_hop:
        # The previous IP lasted long enough: this isn't a rotation loop.
        state["failed_hops"] = 0

    if int(state.get("failed_hops", 0) or 0) >= hops_max:
        state["backoff_until"] = now + BACKOFF_SECONDS
        _usage("pause")
        state["backoff_logged"] = now
        state["pause_started"] = now   # _auto_tor() only acts on a pause that starts on this wall
        if hops_max < FAILED_HOPS_MAX:
            append_log(GUARD_LOG_FILE, f"the last {hops_max} new WARP addresses were already used up and Tor is "
                                       f"ready — switching to Tor now instead of trying more WARP addresses")
        else:
            append_log(
                GUARD_LOG_FILE,
                f"pausing auto-rotation for {BACKOFF_SECONDS // 60} min: the last "
                f"{hops_max} new addresses hit the limit again immediately. "
                f"It will try one more address by itself after that; you can also press "
                f"'Get a new IP' or try Tor now.",
            )
            append_log(GUARD_LOG_FILE, used_up_line(now))
    _save_state(state)
    return now < int(state.get("backoff_until", 0) or 0)


def _read_new_log_bytes(state: dict, *, scan: bool = True) -> tuple[bool, list[str]]:
    """Follow the OpenCode log; optionally skip interpreting new entries."""
    if not OPENCODE_LOG_FILE.is_file():
        return False, []
    try:
        size = OPENCODE_LOG_FILE.stat().st_size
    except OSError:
        return False, []

    offset = int(state.get("offset", 0) or 0)
    if not scan:
        if size != offset:
            state["offset"] = size
            _save_state(state)
        return False, []
    if size < offset:
        offset = 0

    delta = size - offset
    wall = False
    sessions: list[str] = []
    if 0 < delta < MAX_READ_BYTES:
        try:
            with OPENCODE_LOG_FILE.open("rb") as fh:
                fh.seek(offset)
                chunk = fh.read(delta).decode("utf-8", errors="replace")
            chunk_lower = chunk.lower()
            wall_patterns = (
                "quotaexceeded",
                "quota exceeded",
                "quota_exceeded",
                "free usage limit",
                "free usage exceeded",
                "rate limit exceeded",
                "rate_limit_exceeded",
                "429 too many requests",
                "free-tier usage limit",
                "too many requests",
                "exceeded your current quota",
                "usage limit reached",
                "credits exhausted",
                "status=429",
                "status: 429",
                "status 429",
                "code=429",
                "resource_exhausted",
                "ratelimit",
            )
            if any(p in chunk_lower for p in wall_patterns):
                try:
                    from auto_resume import session_id_in
                except Exception:
                    session_id_in = None
                try:
                    import ip_probe
                    ours = set(ip_probe.test_chats())   # openhop's own IP-test chat: handled by _test_ip_first
                except Exception:
                    ours = set()
                if session_id_in is None:
                    wall = True
                else:
                    for line in chunk.splitlines():
                        low = line.lower()
                        if any(p in low for p in wall_patterns):
                            sid = session_id_in(line)
                            if sid and sid in ours:
                                continue
                            wall = True
                            if sid and sid not in sessions:
                                sessions.append(sid)
        except OSError:
            pass

    state["offset"] = size
    _save_state(state)
    return wall, sessions


def _rotate(state: dict, warp_bin: str | None, fresh: bool = False, resume: bool = True) -> None:
    """Get a new address in the current connection type and send 'continue' to the chats waiting.

    fresh=True: we only just switched to this connection type, so the current address never hit the
    limit and isn't added to the used-up list.
    resume=False (Turbo changing a listed WARP address ahead of time): no chat hit the limit, so no 'continue'.
    """
    now = int(time.time())
    _heartbeat()

    # Detect active egress mode
    mode = "warp"
    try:
        from proxy_chain import get_egress_config
        mode = get_egress_config().get("mode", "warp")
    except Exception:
        pass

    # 1. Tor Network mode: new circuit, and check the new exit isn't one we already saw used up today
    if mode == "tor":
        try:
            from tor_manager import get_active_tor_target, rotate_tor_circuit
            target = get_active_tor_target()
            old_ip = trace_via_socks(target[0], target[1]).get("ip", "") if target else ""
            if fresh:
                append_log(GUARD_LOG_FILE, f"now on Tor (exit {old_ip or '?'}) — new circuit")
            else:
                append_log(GUARD_LOG_FILE, f"wall detected in Tor mode (exit {old_ip or '?'}) — new circuit")
            if old_ip and not fresh:
                _blacklist_add(state, old_ip)
                _turbo_report("tor", old_ip)   # through this same exit, before the new circuit
            elif fresh:
                _turbo_sync("tor")
            # Only openhop's own Tor: skip the exits already used up today, so it doesn't land on them again.
            skipped = _tor_skip_used_up(state)
            new_ip, msg = "", ""
            for attempt in range(1, TOR_TRIES + 1):
                _heartbeat()
                ok, msg = rotate_tor_circuit()
                target = get_active_tor_target()
                new_ip = trace_via_socks(target[0], target[1]).get("ip", "") if target else ""
                if not new_ip and skipped:
                    # With those exits left out Tor may find no working one: let it use all of them again.
                    append_log(GUARD_LOG_FILE, f"Tor found no working exit while skipping {skipped} used-up "
                                               f"ones — letting it use all exits again")
                    from tor_manager import clear_tor_exclude_exits
                    clear_tor_exclude_exits()
                    skipped = 0
                    target = get_active_tor_target()
                    new_ip = trace_via_socks(target[0], target[1]).get("ip", "") if target else ""
                if not new_ip or (not _is_burnt(state, new_ip) and new_ip != old_ip):
                    break
                append_log(GUARD_LOG_FILE, f"try {attempt}: Tor exit {new_ip} was already used up today — another one")
                time.sleep(10)  # Tor ignores new-circuit requests closer than 10 s apart
            bump_egress_epoch()
            state["last_hop"] = now
            state["hops"] = int(state.get("hops", 0) or 0) + 1
            _count_new_ip(state)
            if new_ip:
                state["current"] = new_ip
            _save_state(state)
            skip_note = f" — skipped {skipped} used-up exits" if skipped else ""
            append_log(GUARD_LOG_FILE, f"Tor rotated #{state['hops']}: {old_ip or '?'} -> {new_ip or '?'} ({msg}){skip_note}")
            state["last_ok_hop"] = time.time()
            _dispatch_auto_resume(state)
            return
        except Exception as exc:
            append_log(GUARD_LOG_FILE, f"Tor rotation error: {exc}")
            return

    # Your VPN uses the server selected by the user.
    if mode == "vpn":
        return

    # 3. Custom Proxy mode: cycle proxy node in pool
    if mode == "custom":
        append_log(GUARD_LOG_FILE, "wall detected in Custom Proxy mode — cycling proxy node")
        try:
            from proxy_chain import rotate_custom_proxy
            ok, msg = rotate_custom_proxy()
            bump_egress_epoch()
            state["last_hop"] = now
            state["hops"] = int(state.get("hops", 0) or 0) + 1
            _count_new_ip(state)
            _save_state(state)
            append_log(GUARD_LOG_FILE, f"Custom proxy rotated #{state['hops']}: {msg}")
            state["last_ok_hop"] = time.time()
            _dispatch_auto_resume(state)
            return
        except Exception as exc:
            append_log(GUARD_LOG_FILE, f"Custom proxy rotation error: {exc}")
            state["last_hop"] = now
            state["hops"] = int(state.get("hops", 0) or 0) + 1
            _save_state(state)
            return

    # 3. WARP / Clean Endpoint mode: rotate IPv6 /64 subnet
    if not warp_bin:
        append_log(GUARD_LOG_FILE, "wall detected but warp-cli is not installed for WARP rotation")
        return

    old_v6 = get_egress_v6(timeout=8.0) or (get_warp_local_v6() or "")
    old_prefix = normalize_v6_64(old_v6)
    append_log(GUARD_LOG_FILE, f"wall detected — rotating WARP /64 (was: {old_v6 or '?'})")
    _blacklist_add(state, old_v6)
    _save_state(state)
    if mode == "warp" and not fresh:
        _turbo_report("warp", old_v6)   # through WARP itself, before the hop changes the address

    new_v6 = ""
    with keeper_pause_ctx("zen-guard", seconds=300):
        for attempt in range(1, MAX_HOP_TRIES + 1):
            _heartbeat()
            cand = hop_once(warp_bin)
            if not cand:
                if warp_path_leaking(max_age=60):
                    # WARP says Connected but sends traffic out with the user's own IP. More hops won't
                    # change that, and "continue" would just hit the limit again on the real IP.
                    append_log(
                        GUARD_LOG_FILE,
                        f"try {attempt}: WARP is connected but traffic leaves with your own IP — "
                        f"not rotating further; OpenCode stays blocked until WARP is repaired",
                    )
                    break
                append_log(GUARD_LOG_FILE, f"try {attempt}: tunnel repair failed")
                continue
            if old_prefix and normalize_v6_64(cand) == old_prefix:
                append_log(
                    GUARD_LOG_FILE,
                    f"try {attempt}: WARP returned the same address again ({cand}) — not a new IP",
                )
                continue
            if _is_burnt(state, cand):
                append_log(
                    GUARD_LOG_FILE,
                    f"try {attempt}: cloudflare returned a blacklisted /64 ({cand}) — again",
                )
                continue
            new_v6 = cand
            break

        if not new_v6 and not warp_path_leaking(max_age=60):
            append_log(GUARD_LOG_FILE, "everything blacklisted — recycling oldest block")
            cand = hop_once(warp_bin)
            if cand:
                new_v6 = cand

    state["last_hop"] = now
    same_address = bool(old_prefix) and bool(new_v6) and normalize_v6_64(new_v6) == old_prefix
    if new_v6 and not same_address:
        state["current"] = new_v6
        state["hops"] = int(state.get("hops", 0) or 0) + 1
        _count_new_ip(state)
        _save_state(state)
        append_log(GUARD_LOG_FILE, f"rotated #{state['hops']}: {old_v6} -> {new_v6} ✓")
        try:
            from ouc_common import LANE_DIR
            (LANE_DIR / "edge_reroll").write_text(str(time.time()), encoding="utf-8")
        except Exception:
            pass
        state["last_ok_hop"] = time.time()
        if resume:
            _dispatch_auto_resume(state)
        else:
            _save_state(state)
    elif same_address:
        # Don't send "continue" to the same limited IP: that is exactly the loop users were seeing.
        state["last_ok_hop"] = time.time()
        state["hop_counted"] = state["last_ok_hop"]
        state["failed_hops"] = int(state.get("failed_hops", 0) or 0) + 1
        _save_state(state)
        append_log(
            GUARD_LOG_FILE,
            f"rotation ended on the same address ({new_v6}) — chat left paused instead of looping",
        )
    else:
        _save_state(state)
        append_log(
            GUARD_LOG_FILE,
            "rotation failed — keeper will keep the tunnel alive, retrying next wall",
        )


VPN_WAIT_MAX = 45 * 60   # stop waiting for a new VPN server after this (the next limit asks again)
VPN_STRAGGLERS = 30      # limit errors this soon after a new VPN IP come from before the change
VPN_AUTO_STEP_CLI = 45   # after asking a VPN app for another place, wait this long for the new IP
VPN_AUTO_STEP_WG = 10    # a WireGuard file is up in a couple of seconds
VPN_AUTO_TRIES = 8       # most automatic changes in a row before asking the user


def _vpn_id() -> str:
    try:
        from proxy_chain import get_egress_config
        return str(get_egress_config().get("vpn_id") or "")
    except Exception:
        return ""


def _vpn_name(vpn_id: str) -> str:
    try:
        from vpn_detect import get_vpn
        return str((get_vpn(vpn_id) or {}).get("name") or vpn_id or "your VPN")
    except Exception:
        return vpn_id or "your VPN"


def _vpn_ask_new_server(state: dict, fresh: bool = False) -> None:
    """The free limit, on a VPN app: remember the used-up exit IP and wait for the user to change server.

    The app shows "Change server in <VPN>" (state "vpn_wait") and the tray says it once, with a button that
    opens the VPN. _vpn_wait_check() sees the new IP and sends 'continue' by itself.
    """
    from vpn_detect import vpn_exit_ip
    now = int(time.time())
    vpn_id = _vpn_id()
    old_ip = vpn_exit_ip(vpn_id) or str(state.get("current", "") or "")
    if fresh:
        # Just switched to this VPN: its IP never hit the limit, so nothing to wait for.
        if old_ip:
            state["current"] = old_ip
        state["last_ok_hop"] = time.time()
        _save_state(state)
        append_log(GUARD_LOG_FILE, f"now on {_vpn_name(vpn_id)} (exit {old_ip or '?'})")
        _dispatch_auto_resume(state)
        return
    if old_ip:
        _blacklist_add(state, old_ip)
        _turbo_report("vpn", old_ip)   # through the VPN itself, before the server changes
    wait = {"since": now, "old_ip": old_ip, "vpn": vpn_id, "burnt_seen": "", "auto": False, "tried": []}
    state["vpn_wait"] = wait
    state["last_hop"] = now
    try:
        from vpn_detect import can_change_server
        auto = can_change_server(vpn_id)
    except Exception:
        auto = False
    if auto:
        if vpn_id == "wireguard":
            try:
                import wg_manager
                cur = wg_manager.current_conf()
                wg_manager.record_exit(cur, old_ip)
                wait["tried"] = [cur] if cur else []
            except Exception:
                pass
        wait["auto"] = True
        append_log(GUARD_LOG_FILE, f"free limit on {_vpn_name(vpn_id)} (exit {old_ip or '?'}) — changing server")
        _vpn_auto_step(state, wait)
        _save_state(state)
        return
    _save_state(state)
    append_log(GUARD_LOG_FILE, f"free limit on {_vpn_name(vpn_id)} (exit {old_ip or '?'}) — {_vpn_name(vpn_id)} "
                               f"can't change server by itself: waiting for you to pick another server in the app")


def _vpn_auto_step(state: dict, wait: dict) -> None:
    """Change server by ourselves: the next WireGuard file, or another place through the VPN's command line.

    Servers already known to be used up today are skipped without trying them. When nothing is left the
    wait becomes a normal "pick another server" (auto off) so the app asks the user.
    """
    vpn_id = str(wait.get("vpn") or "")
    tried = wait.setdefault("tried", [])
    wait["step_at"] = int(time.time())
    if len(tried) >= VPN_AUTO_TRIES + (1 if vpn_id == "wireguard" else 0):
        wait["auto"] = False
        append_log(GUARD_LOG_FILE, f"{_vpn_name(vpn_id)}: {VPN_AUTO_TRIES} servers in a row were used up — "
                                   f"pick another one yourself, or add more")
        return
    if vpn_id == "wireguard":
        import wg_manager
        nxt = wg_manager.next_conf(tried, is_burnt=lambda ip: _is_burnt(state, ip))
        if not nxt:
            wait["auto"] = False
            wait["exhausted"] = True
            n = len(wg_manager.list_confs())
            append_log(GUARD_LOG_FILE, f"all {n} WireGuard file(s) are used up today — add more files "
                                       f"(another server or country) and openhop goes on by itself")
            return
        tried.append(nxt)
        wg_manager.set_current(nxt)
        ok, msg = wg_manager.ensure_running()
        wait["target"] = wg_manager._label(nxt)
        append_log(GUARD_LOG_FILE, f"WireGuard: switching to {wait['target']}" + ("" if ok else f" ({msg})"))
        return
    from vpn_detect import change_server
    res = change_server(vpn_id, tried)
    if res.get("place"):
        tried.append(res["place"])
    wait["target"] = res.get("place", "")
    if res.get("ok"):
        append_log(GUARD_LOG_FILE, f"{_vpn_name(vpn_id)}: asked for a server in {res['place']}")
    elif res.get("place"):
        append_log(GUARD_LOG_FILE, f"{_vpn_name(vpn_id)} couldn't go to {res['place']}: {res.get('message', '')}")
        wait["step_at"] = 0   # try the next place on the next check
    else:
        wait["auto"] = False
        append_log(GUARD_LOG_FILE, f"{_vpn_name(vpn_id)} can't change server by itself ({res.get('message', '')})"
                                   f" — waiting for you to pick another server in the app")


def _vpn_wait_check(state: dict) -> bool:
    """While waiting for a new VPN server: has the exit IP changed? Then 'continue'. True while still waiting."""
    wait = state.get("vpn_wait")
    if not wait:
        return False
    now = int(time.time())
    vpn_id = str(wait.get("vpn") or "")
    if _egress_mode() != "vpn" or vpn_id != _vpn_id():
        state.pop("vpn_wait", None)
        _save_state(state)
        append_log(GUARD_LOG_FILE, "connection type changed — no longer waiting for a new VPN server")
        return False
    if now - int(wait.get("since", now) or now) > VPN_WAIT_MAX:
        state.pop("vpn_wait", None)
        _save_state(state)
        append_log(GUARD_LOG_FILE, f"stopped waiting for a new {_vpn_name(vpn_id)} server after "
                                   f"{VPN_WAIT_MAX // 60} min — the next limit asks again")
        return False
    from vpn_detect import vpn_exit_ip
    new_ip = vpn_exit_ip(vpn_id, timeout=6.0)
    old_ip = str(wait.get("old_ip") or "")
    if vpn_id == "wireguard" and new_ip:
        try:
            import wg_manager
            wg_manager.record_exit(wg_manager.current_conf(), new_ip)
        except Exception:
            pass
    if not new_ip or new_ip == old_ip or _is_burnt(state, new_ip):
        if wait.get("auto"):
            # WireGuard switches before we get here, so any answer is the new server's. A VPN app takes a
            # while: the old IP just means it hasn't moved yet. A used-up new IP needs no more waiting.
            if vpn_id == "wireguard":
                min_wait = 2 if new_ip else VPN_AUTO_STEP_WG
            else:
                min_wait = 2 if (new_ip and new_ip != old_ip) else VPN_AUTO_STEP_CLI
            if now - int(wait.get("step_at", 0) or 0) >= min_wait:
                if new_ip and new_ip != old_ip:
                    append_log(GUARD_LOG_FILE, f"{_vpn_name(vpn_id)} is now on {new_ip}, but that one already "
                                               f"hit the limit today — trying the next one")
                _vpn_auto_step(state, wait)
                _save_state(state)
            return True
    if not new_ip or new_ip == old_ip:
        return True   # still the same server, or the VPN is reconnecting
    if _is_burnt(state, new_ip):
        if wait.get("burnt_seen") != new_ip:
            wait["burnt_seen"] = new_ip
            _save_state(state)
            append_log(GUARD_LOG_FILE, f"{_vpn_name(vpn_id)} is now on {new_ip}, but that one already hit the "
                                       f"limit today — waiting for another server")
        return True
    state.pop("vpn_wait", None)
    bump_egress_epoch()   # close OpenCode's open connections so they reconnect through the new server
    state["current"] = new_ip
    state["hops"] = int(state.get("hops", 0) or 0) + 1
    _count_new_ip(state)
    state["last_ok_hop"] = time.time()
    _save_state(state)
    append_log(GUARD_LOG_FILE, f"{_vpn_name(vpn_id)} changed server #{state['hops']}: {old_ip or '?'} -> {new_ip} ✓")
    _dispatch_auto_resume(state)
    return False


def _vpn_on_wall(state: dict, warp_bin: str | None, now: int, sessions: list[str]) -> None:
    """The limit in VPN mode. No pause/backoff here: only the user can change the server."""
    pending = state.setdefault("pending", {})
    for sid in sessions:
        pending.setdefault(sid, now)
    _save_state(state)
    if state.get("vpn_wait"):
        return   # already asked; the new IP sends 'continue' to these chats too
    since_ok = now - int(state.get("last_ok_hop", 0) or 0)
    if since_ok < VPN_STRAGGLERS and _resumable(state):
        append_log(GUARD_LOG_FILE, f"{len(_resumable(state))} more chat(s) hit the limit just before the new "
                                   f"server ({since_ok}s ago) — sending them 'continue'")
        _dispatch_auto_resume(state)
        return
    _rotate(state, warp_bin)


_maint_logged = False


def _maintenance_block() -> bool:
    """openhop is in maintenance (set by the openhop dev team): don't rotate. Fails open on any error."""
    global _maint_logged
    try:
        from notices import blocked
        on = blocked()
    except Exception:
        on = False
    if on != _maint_logged:
        _maint_logged = on
        append_log(GUARD_LOG_FILE, "openhop is in maintenance: not rotating until it ends" if on
                   else "maintenance is over: rotating again")
    return on


def _end_pause(state: dict, warp_bin: str | None, now: int) -> bool:
    """Called every tick: if the rotation pause is over, try one fresh IP for the chats still waiting.

    Returns True when a rotation was attempted.
    """
    if not state.get("backoff_until") or now < int(state["backoff_until"]):
        return False
    state["backoff_until"] = 0
    state["failed_hops"] = 0
    state["backoff_logged"] = 0
    state["hop_counted"] = 0
    _save_state(state)
    append_log(GUARD_LOG_FILE, "rotation pause is over")
    if not state.get("pending"):
        return False
    append_log(
        GUARD_LOG_FILE,
        f"trying a fresh IP for {len(state['pending'])} chat(s) that are still waiting",
    )
    _rotate(state, warp_bin)
    return True


def _auto_tor_enabled() -> bool:
    """The app's "Switch to Tor by itself" setting (app_settings.json "auto_tor", on by default)."""
    try:
        data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        return bool(data.get("auto_tor", True)) if isinstance(data, dict) else True
    except Exception:
        return True


def _egress_mode() -> str:
    try:
        from proxy_chain import get_egress_config
        return str(get_egress_config().get("mode", "warp") or "warp")
    except Exception:
        return "warp"


def _tor_available() -> tuple[bool, str]:
    """Tor already running, or installed so it can start in seconds. (No download here: that can take minutes.)"""
    try:
        from tor_manager import find_tor_exe, get_active_tor_target
        if get_active_tor_target() or find_tor_exe():
            return True, ""
        return False, "Tor isn't installed yet"
    except Exception as exc:
        return False, f"couldn't check Tor ({exc})"


TOR_PREFETCH_AT = 5          # WARP addresses in a row that hit the limit before Tor is downloaded (in the background)
TOR_EARLY_AT = 6             # with Tor ready, switch after this many used-up WARP addresses instead of FAILED_HOPS_MAX
TOR_WAIT_MAX = 30 * 60       # how long a pause keeps waiting for that download to finish
_tor_dl: dict = {"thread": None, "ok": None, "msg": ""}


def _hops_max() -> int:
    """Used-up addresses in a row before the guard gives up on this kind of address.

    On WARP, with "Switch to Tor by itself" on and Tor already downloaded, that's TOR_EARLY_AT: a report showed
    Tor ready after 5 used-up WARP addresses while the guard kept trying WARP up to 15 (another ~3 min stuck),
    and when 6 in a row are used up the rest usually are too. Otherwise FAILED_HOPS_MAX, as before.
    """
    try:
        if _egress_mode() in ("warp", "clean") and _auto_tor_enabled() and _tor_available()[0]:
            return TOR_EARLY_AT
    except Exception:
        pass
    return FAILED_HOPS_MAX


def _tor_downloading() -> bool:
    t = _tor_dl.get("thread")
    return bool(t and t.is_alive())


def _prefetch_tor(why: str) -> None:
    """Download Tor (~15 MB) in a background thread, so a later switch takes seconds instead of blocking the guard.

    Only when the auto-Tor setting is on, in WARP mode, Tor isn't there yet, and no download is running.
    A failed download is retried at most every 10 minutes.
    """
    if _tor_downloading() or not _auto_tor_enabled() or _egress_mode() not in ("warp", "clean"):
        return
    if _tor_available()[0]:
        return
    if _tor_dl.get("ok") is False and time.time() - float(_tor_dl.get("at", 0) or 0) < 600:
        return
    import threading

    def run() -> None:
        try:
            from tor_manager import download_and_extract_tor
            ok, msg = download_and_extract_tor()
        except Exception as exc:
            ok, msg = False, str(exc)
        _tor_dl.update(ok=ok, msg=msg, at=time.time())
        append_log(GUARD_LOG_FILE, f"Tor download {'finished' if ok else 'failed'}: {msg}")

    append_log(GUARD_LOG_FILE, f"{why} — downloading Tor (~15 MB) in the background, so openhop can switch to it "
                               f"by itself if WARP's addresses stay used up")
    _tor_dl.update(ok=None, msg="", at=time.time())
    t = threading.Thread(target=run, name="tor-download", daemon=True)
    _tor_dl["thread"] = t
    t.start()


def _auto_tor_when_ready(state: dict, warp_bin: str | None, now: int) -> bool:
    """A pause began while Tor was still downloading: switch as soon as it's ready (while the pause lasts)."""
    wanted = int(state.get("auto_tor_wanted", 0) or 0)
    if not wanted:
        return False
    if now - wanted > TOR_WAIT_MAX or now >= int(state.get("backoff_until", 0) or 0):
        state.pop("auto_tor_wanted", None)   # pause over: the normal "pause is over" path takes it from here
        _save_state(state)
        return False
    if _tor_downloading() or not _tor_available()[0]:
        return False
    state.pop("auto_tor_wanted", None)
    _save_state(state)
    return _auto_tor(state, warp_bin, now, [], waited=True)


def _auto_tor(state: dict, warp_bin: str | None, now: int, sessions: list[str], waited: bool = False) -> bool:
    """WARP's shared addresses are used up (the guard just paused): switch to Tor instead of waiting.

    Only when the pause starts on this wall, in WARP mode, with the setting on. The switch is marked as
    automatic (egress_mode.json "auto_tor_until" = the next daily reset) so _auto_tor_back() can undo it;
    a Tor switch the user made is never undone. If Tor can't start, the pause and the app's
    "Try Tor" notice stay exactly as before. Returns True when it switched (and rotated on Tor).
    """
    if (not waited and int(state.get("pause_started", 0) or 0) != now) or now >= int(state.get("backoff_until", 0) or 0):
        return False
    if _egress_mode() not in ("warp", "clean"):
        return False
    if not _auto_tor_enabled():
        append_log(GUARD_LOG_FILE, "not switching to Tor by itself: that setting is off in the app")
        return False
    ok, why = _tor_available()
    if not ok:
        pending = state.setdefault("pending", {})
        for sid in sessions:
            pending.setdefault(sid, now)
        _prefetch_tor("WARP's addresses are used up")
        if _tor_downloading():
            state["auto_tor_wanted"] = now
            _save_state(state)
            append_log(GUARD_LOG_FILE, "WARP's addresses are used up and Tor is still downloading — openhop switches "
                                       "to Tor by itself as soon as it's ready")
        else:
            _save_state(state)
            append_log(GUARD_LOG_FILE, f"WARP's addresses are used up, but openhop can't switch to Tor by itself: "
                                       f"{_tor_dl.get('msg') or why}. Waiting instead; 'Try Tor' in the app sets it up.")
        return False

    until = next_free_reset(now)
    back_at = time.strftime("%H:%M", time.localtime(until))
    append_log(GUARD_LOG_FILE, f"WARP's shared addresses are used up right now ({int(state.get('failed_hops', 0) or 0) or FAILED_HOPS_MAX} in a row hit the "
                               f"limit) — switching to Tor by itself instead of pausing. It goes back to WARP "
                               f"after the daily reset ({back_at}).")
    # Lift the pause while Tor starts, so the app doesn't announce a pause that isn't happening.
    backoff_until = int(state.get("backoff_until", 0) or 0)
    state.update(backoff_until=0, failed_hops=0, hop_counted=0, backoff_logged=0)
    pending = state.setdefault("pending", {})
    for sid in sessions:
        pending.setdefault(sid, now)
    _save_state(state)
    _heartbeat()
    try:
        from proxy_chain import switch_to_tor
        switched, msg = switch_to_tor(wait=True, auto_until=until)
    except Exception as exc:
        switched, msg = False, str(exc)
    _heartbeat()
    if not switched:
        state.update(backoff_until=max(backoff_until, int(time.time()) + 60), failed_hops=FAILED_HOPS_MAX,
                     backoff_logged=int(time.time()))
        _save_state(state)
        append_log(GUARD_LOG_FILE, f"couldn't start Tor ({msg}) — staying on WARP and waiting instead")
        return False
    state["auto_tor_at"] = int(time.time())
    _save_state(state)
    append_log(GUARD_LOG_FILE, f"switched to Tor by itself ({msg or 'Tor is running'}) — getting a Tor address "
                               f"and sending 'continue'")
    _rotate(state, warp_bin, fresh=True)
    return True


def _auto_tor_back(state: dict, now: float) -> bool:
    """After the daily reset, go back to WARP if (and only if) openhop switched to Tor by itself."""
    try:
        from proxy_chain import get_egress_config, switch_back_from_auto_tor
        cfg = get_egress_config()
        until = float(cfg.get("auto_tor_until", 0) or 0)
        if cfg.get("mode") != "tor" or not until or now < until:
            return False
        if not switch_back_from_auto_tor(now):
            return False
    except Exception as exc:
        append_log(GUARD_LOG_FILE, f"couldn't switch back to WARP: {exc}")
        return False
    state.update(backoff_until=0, failed_hops=0, hop_counted=0, backoff_logged=0)
    state.pop("auto_tor_at", None)
    _save_state(state)
    append_log(GUARD_LOG_FILE, "the free limit has reset for the day, so openhop switched back from Tor to WARP "
                               "(it had switched to Tor by itself)")
    return True


TURBO_CHECK_SECONDS = 10 * 60   # how often the WARP address is checked against the shared list
TURBO_IDLE_SECONDS = 120        # only switch when OpenCode has written nothing to its log for this long
_turbo_next_check = time.time() + 60


def _turbo_check_current(state: dict, warp_bin: str | None, now: int) -> bool:
    """WARP + Turbo: if the address in use is already on the shared list, change it before the user sends a
    message, never in the middle of an answer (OpenCode idle). True when it changed address."""
    global _turbo_next_check
    if now < _turbo_next_check:
        return False
    _turbo_next_check = now + TURBO_CHECK_SECONDS
    if not warp_bin or _egress_mode() != "warp" or state.get("pending")             or int(state.get("backoff_until", 0) or 0) > now:
        return False
    try:
        import turbo
        if not turbo.enabled():
            return False
        turbo.sync("warp")
        cur = get_egress_v6(timeout=6.0)
        if not cur or not turbo.is_listed(cur):
            return False
        idle = now - OPENCODE_LOG_FILE.stat().st_mtime if OPENCODE_LOG_FILE.is_file() else 1e9
    except Exception:
        return False
    if idle < TURBO_IDLE_SECONDS:
        _turbo_next_check = now + 60   # busy: look again in a minute
        return False
    if now - int(state.get("last_hop", 0) or 0) < COOLDOWN_SECONDS:
        return False
    append_log(GUARD_LOG_FILE, f"Turbo: the WARP address in use ({cur}) was already used up today by another "
                               f"openhop user — changing it before you send a message")
    _turbo_listed(cur)
    _rotate(state, warp_bin, fresh=True, resume=False)
    return True


def main() -> int:
    ensure_dirs()
    mutex = acquire_mutex(MUTEX_GUARD)
    if mutex is None:
        return 0

    warp_bin = find_warp_cli()
    state = _load_state()
    _backfill_good_hops(state)
    append_log(
        GUARD_LOG_FILE,
        f"guard up (poll {POLL_SECONDS}s, cooldown {COOLDOWN_SECONDS}s, warp={'found' if warp_bin else 'none'})",
    )
    _heartbeat()
    _rescue_stuck_at_start(state, warp_bin)

    while True:
        time.sleep(POLL_SECONDS)
        try:
            _heartbeat()
            now = int(time.time())
            if _egress_mode() == "vpn":
                _read_new_log_bytes(state, scan=False)
                continue
            # "Try now" / "Try Tor" in the app: end the pause right away.
            if KICK_FILE.is_file():
                try:
                    KICK_FILE.unlink()
                except OSError:
                    pass
                if int(state.get("backoff_until", 0) or 0) > now:
                    append_log(GUARD_LOG_FILE, "the user asked to try again now")
                    state["backoff_until"] = now
            # "N chats are stopped at the free limit" fix in the app: new IP + 'continue' for them.
            if RESCUE_FILE.is_file():
                try:
                    RESCUE_FILE.unlink()
                except OSError:
                    pass
                _rescue_stuck_at_start(state, warp_bin, asked=True)
                continue
            # Switched to Tor by itself earlier and the daily reset has passed: back to WARP.
            _auto_tor_back(state, now)
            # A pause started while Tor was downloading: switch now that it's ready.
            if _auto_tor_when_ready(state, warp_bin, now):
                continue
            # Pause is over and chats are still waiting: try one fresh address by ourselves,
            # instead of waiting for the user to type and hit the limit again just to trigger us.
            _end_pause(state, warp_bin, now)
            wall, sessions = _read_new_log_bytes(state)
            if not _maintenance_block() and _check_wait_for_back(state, now, sessions):
                continue
            # The test chat found this IP used up: same as the limit coming back right after the IP change.
            probe_wall = _ip_known_spent(state)
            wall = wall or probe_wall
            if not wall:
                _turbo_check_current(state, warp_bin, now)
                continue
            if _maintenance_block() or _egress_mode() == "vpn":
                continue
            if sessions:
                sessions, handled = _move_limited_chats(state, sessions, now)
                if handled and not sessions:
                    continue   # every limited chat now continues in a new chat: no new IP needed
            if _note_wall(state, now):
                state.pop("probe_spent", None)   # counted once; the pause / Tor switch takes it from here
                _save_state(state)
                # WARP's addresses are used up: try Tor by itself instead of waiting (if allowed and possible).
                _auto_tor(state, warp_bin, now, sessions)
                continue
            pending = state.setdefault("pending", {})
            for sid in sessions:
                pending.setdefault(sid, now)
            _usage("limit", n=len(sessions))
            _save_state(state)
            if int(state.get("failed_hops", 0) or 0) >= TOR_PREFETCH_AT:
                _prefetch_tor(f"{state['failed_hops']} new WARP addresses in a row were already used up")
            last_hop = int(state.get("last_hop", 0) or 0)
            age = now - last_hop
            # The new IP was already used up: look for the next one now, not after the normal cooldown.
            hunting = (
                int(state.get("failed_hops", 0) or 0) > 0
                and now - int(state.get("last_ok_hop", 0) or 0) <= REWALL_SECONDS
            )
            if age >= COOLDOWN_SECONDS or (hunting and age >= HUNT_GAP_SECONDS):
                if hunting and age < COOLDOWN_SECONDS:
                    append_log(GUARD_LOG_FILE, "that address was already used up — trying the next one right away")
                state.pop("probe_spent", None)   # one test result = one limit: a failed rotation waits as before
                _rotate(state, warp_bin)
            elif probe_wall:
                append_log(GUARD_LOG_FILE, f"that address was already used up — trying the next one in "
                                           f"{max(1, HUNT_GAP_SECONDS - age)}s")
            elif _resumable(state):
                # Other tabs that were cut off by the same wall (their errors arrive while or just after we
                # switched IP). The new IP is fresh, so just send them 'continue' too, once each.
                append_log(
                    GUARD_LOG_FILE,
                    f"{len(_resumable(state))} more chat(s) hit the limit around the last IP change "
                    f"({age}s ago) — sending them 'continue'",
                )
                _dispatch_auto_resume(state)
            else:
                append_log(
                    GUARD_LOG_FILE,
                    f"wall seen but in cooldown ({age}s < {COOLDOWN_SECONDS}s) — waiting",
                )
        except Exception as exc:
            append_log(GUARD_LOG_FILE, f"guard error: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())

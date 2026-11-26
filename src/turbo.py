"""openhop Turbo: share, through openhop.cfd, the exit addresses that already hit OpenCode's free limit today.

When this PC hits the limit on a WARP / Tor / VPN exit, openhop tells openhop.cfd ("this exit is used up"). When
another user lands on that exit, openhop skips it straight away instead of spending a message to find out.

Privacy, by design (see web/src/turbo.mjs for the server side):
- Every Turbo request leaves THROUGH the exit (WARP's local proxy, Tor, the VPN's adapter / WireGuard tunnel),
  never through the user's normal connection. If openhop can't send it that way, it doesn't send it.
- The report holds no address. The site lists the address the request comes from, and only if it matches the
  hash openhop expected (so a leak or a forged report stores nothing).
- Only day-salted hashes are kept, and they're all gone at 01:05 UTC the next day (when the free limit resets).
- Custom proxies are never reported (they can be personal).

The switch is "turbo" in app_settings.json (on unless the user turned it off). Without openhop.cfd it pauses
itself: what was already downloaded still counts until it expires, nothing else changes.
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import secrets
import shutil
import time
from datetime import datetime, timezone

from ouc_common import DEST_DIR, GUARD_DIR, VERSION, WARP_PROXY_PORT, current_warp_scope, run_hidden

TURBO_URL = os.environ.get("OPENHOP_TURBO_URL", "https://openhop.cfd").rstrip("/")
# 3.8 ships without Turbo (it comes in 4.0, and the site's Turbo endpoints aren't live yet). False = Turbo is off
# for everyone, whatever app_settings.json says, and its card is hidden in the UI. OPENHOP_TURBO=1 turns it on for tests.
AVAILABLE = os.environ.get("OPENHOP_TURBO") == "1"
TURBO_FILE = GUARD_DIR / "turbo.json"
SETTINGS_FILE = DEST_DIR / "app_settings.json"
MODES = {"warp": "w", "tor": "t", "vpn": "v"}
SYNC_EVERY = 30 * 60          # refresh the list at most this often (and right when the limit is hit)
PAUSE_AFTER = 30 * 60         # no answer from openhop.cfd for this long -> "paused"
RESET_AFTER_MIDNIGHT = 3900   # 01:05 UTC, like ouc_common.next_free_reset()


# ------------------------------------------------------------------------------------------------- keys

def canon(addr: str) -> str:
    """The block the free limit counts by, written exactly like the site does: IPv4 itself, IPv6 'a:b:c:d::/64'."""
    clean = (addr or "").strip().strip("[]").split("%", 1)[0]
    if not clean:
        return ""
    try:
        ip = ipaddress.ip_address(clean)
    except ValueError:
        return ""
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.version == 4:
        return str(ip)
    groups = ip.exploded.split(":")[:4]
    return ":".join(g.lstrip("0") or "0" for g in groups) + "::/64"


def utc_day(ts: float | None = None) -> str:
    ts = time.time() if ts is None else ts
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d")


def key(day: str, addr: str) -> str:
    """The listed key for an address on a UTC day ('' if it isn't an address)."""
    prefix = canon(addr)
    if not prefix:
        return ""
    return hashlib.sha256(f"openhop-turbo:{day}:{prefix}".encode()).hexdigest()[:16]


def expiry(day: str) -> float:
    """When entries from this UTC day stop counting (01:05 UTC the next day)."""
    d = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    return d.timestamp() + 86400 + RESET_AFTER_MIDNIGHT


# ------------------------------------------------------------------------------------------------- state

def enabled() -> bool:
    """The user's switch (Turbo is on unless they turned it off). Off for everyone while AVAILABLE is False."""
    if not AVAILABLE:
        return False
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict) and "turbo" in raw:
            return bool(raw["turbo"])
    except Exception:
        pass
    return True


def _load() -> dict:
    try:
        data = json.loads(TURBO_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _save(st: dict) -> None:
    try:
        TURBO_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = TURBO_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, separators=(",", ":")), encoding="utf-8")
        os.replace(tmp, TURBO_FILE)
    except Exception:
        pass


def _state() -> dict:
    st = _load()
    if not isinstance(st.get("inst"), str) or len(st["inst"]) != 32:
        st["inst"] = secrets.token_hex(16)   # random, not tied to the person; only used to cap reports
    if not isinstance(st.get("entries"), dict):
        st["entries"] = {}
    return st


def _prune(st: dict, now: float) -> None:
    ents = st.get("entries") or {}
    st["entries"] = {h: e for h, e in ents.items()
                     if isinstance(e, list) and e and _safe_expiry(e[0]) > now}


def _safe_expiry(day) -> float:
    try:
        return expiry(str(day))
    except Exception:
        return 0.0


def _merge(st: dict, body: dict, now: float) -> None:
    if body.get("full"):
        st["entries"] = {}
    for row in body.get("entries") or []:
        try:
            h, day, m, at = row[0], str(row[1]), str(row[2]), int(row[3])
        except Exception:
            continue
        if isinstance(h, str) and len(h) == 16:
            st["entries"][h] = [day, m, at]
    if isinstance(body.get("next"), (int, float)):
        st["next"] = int(body["next"])
    st["total"] = int(body.get("total") or len(st["entries"]))
    st["synced"] = now
    st["last_ok"] = now
    st["fails"] = 0
    st.pop("last_error", None)
    _prune(st, now)


def _failed(st: dict, why: str, now: float) -> None:
    st["fails"] = int(st.get("fails") or 0) + 1
    st["last_error"] = why
    st["last_try"] = now


def _backing_off(st: dict, now: float) -> bool:
    """After failures, wait before asking openhop.cfd again (2 min per failure, at most 30 min), so a site
    that's down never slows down a change of address."""
    fails = int(st.get("fails") or 0)
    return fails > 0 and now - float(st.get("last_try") or 0) < min(PAUSE_AFTER, 120 * fails)


def _bump_today(st: dict, name: str, now: float) -> None:
    day = utc_day(now)
    cnt = st.get("today") if isinstance(st.get("today"), dict) else {}
    if cnt.get("day") != day:
        cnt = {"day": day}
    cnt[name] = int(cnt.get(name) or 0) + 1
    st["today"] = cnt
    tot = st.get("totals") if isinstance(st.get("totals"), dict) else {}
    tot[name] = int(tot.get(name) or 0) + 1      # since Turbo was first used on this PC (shown in the app)
    st["totals"] = tot


# ------------------------------------------------------------------------------------------------- transport

def route_for(mode: str, exit_ip: str = "") -> list[str] | None:
    """curl options that send a request out through the current exit, or None (then nothing is sent).

    OPENHOP_TURBO_ROUTE=direct is for local tests against `wrangler dev` only.
    """
    if os.environ.get("OPENHOP_TURBO_ROUTE") == "direct":
        return []
    fam = []
    if exit_ip:
        fam = ["-6"] if ":" in exit_ip else ["-4"]
    if mode == "warp":
        if current_warp_scope() == "isolated":
            # socks5:// (not socks5h): curl looks the name up itself, so -6/-4 picks the same family the exit has.
            return ["-x", f"socks5://127.0.0.1:{WARP_PROXY_PORT}", *fam] if fam else \
                   ["-x", f"socks5h://127.0.0.1:{WARP_PROXY_PORT}"]
        return ["--noproxy", "*", *fam]       # Full TUN: the whole PC goes through WARP
    if mode == "tor":
        try:
            from tor_manager import get_active_tor_target
            target = get_active_tor_target()
        except Exception:
            target = None
        return ["-x", f"socks5h://{target[0]}:{target[1]}"] if target else None
    if mode == "vpn":
        try:
            from proxy_chain import get_egress_config
            vpn_id = str(get_egress_config().get("vpn_id") or "")
            import vpn_detect
            if vpn_id == vpn_detect.WIREGUARD["id"]:
                import wg_manager
                return ["-x", f"socks5h://127.0.0.1:{wg_manager.SOCKS_PORT}"] if wg_manager.socks_alive(0.6) else None
            bind = vpn_detect.vpn_bind_address(vpn_id, max_age=0)
            return ["--interface", bind[0]] if bind else None
        except Exception:
            return None
    return None


def _request(route: list[str], method: str, path: str, body: dict | None = None,
             timeout: float = 6.0) -> tuple[int, dict]:
    """(HTTP status, JSON body). Status 0 = no answer."""
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        return 0, {}
    cmd = [curl, *route, "-s", "-m", str(int(timeout)), "-A", f"openhop/{VERSION}", "-w", "\n%{http_code}",
           "-X", method]
    if body is not None:
        # Small, and holds no address or anything personal, so it can go on the command line.
        cmd += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(body, separators=(",", ":"))]
    cmd.append(f"{TURBO_URL}{path}")
    try:
        res = run_hidden(cmd, timeout=timeout + 3.0)
    except Exception:
        return 0, {}
    out = (res.stdout or "").rstrip()
    text, _, code = out.rpartition("\n")
    try:
        status = int(code.strip())
    except ValueError:
        return 0, {}
    try:
        parsed = json.loads(text) if text else {}
    except ValueError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


# ------------------------------------------------------------------------------------------------- public API

def is_listed(addr: str, now: float | None = None) -> bool:
    """True when another openhop user already hit the free limit on this address today (and Turbo is on)."""
    if not addr or not enabled():
        return False
    now = time.time() if now is None else now
    st = _load()
    ents = st.get("entries") or {}
    if not ents:
        return False
    days = {str(e[0]) for e in ents.values() if isinstance(e, list) and e and _safe_expiry(e[0]) > now}
    return any(key(d, addr) in ents for d in days)


def note_skip(n: int = 1) -> None:
    """Count tries Turbo saved today (shown in the app)."""
    st = _state()
    for _ in range(max(0, n)):
        _bump_today(st, "skipped", time.time())
    _save(st)


def sync(mode: str, force: bool = False, route: list[str] | None = None) -> bool:
    """Download what's new on the shared list (through the exit). Returns True when the list is fresh."""
    if mode not in MODES or not enabled():
        return False
    now = time.time()
    st = _state()
    if not force and now - float(st.get("synced") or 0) < SYNC_EVERY:
        return True
    if _backing_off(st, now):
        return False
    if route is None:
        route = route_for(mode)
    if route is None:
        return False
    since = int(st.get("next") or 0) if st.get("entries") else 0
    status, body = _request(route, "GET", f"/api/turbo/list?since={since}")
    if status == 200 and body.get("ok"):
        _merge(st, body, now)
    else:
        _failed(st, f"list {status or 'no answer'}", now)
    _save(st)
    return status == 200


def report_hit(mode: str, exit_ip: str, at: float | None = None,
               route: list[str] | None = None) -> tuple[bool, str]:
    """Tell openhop.cfd this exit just hit the free limit. Call it BEFORE changing address (it goes through it).

    Returns (stored or already listed, short reason). Also refreshes the local copy of the list.
    """
    if mode not in MODES:
        return False, "mode"
    if not enabled():
        return False, "off"
    prefix = canon(exit_ip)
    if not prefix:
        return False, "no exit address"
    now = time.time()
    at = now if at is None else min(now, max(now - 600, at))
    st = _state()
    if _backing_off(st, now):
        return False, "openhop.cfd didn't answer a moment ago"
    if route is None:
        route = route_for(mode, exit_ip)
    if route is None:
        return False, "no way to send it through the exit"
    body = {"mode": mode, "inst": st["inst"], "expect": key(utc_day(at), exit_ip), "at": int(at * 1000),
            "since": int(st.get("next") or 0) if st.get("entries") else 0}
    status, ans = _request(route, "POST", "/api/turbo/hit", body)
    if status == 200 and ans.get("ok"):
        _merge(st, ans, now)
        _bump_today(st, "reported", now)
        _save(st)
        return True, "listed" if ans.get("stored") else "already listed"
    code = str(ans.get("code") or (status or "no answer"))
    if status in (0, 502, 503, 504):
        _failed(st, f"report {code}", now)
    _save(st)
    return False, code


def status() -> dict:
    """For the app: {enabled, state: on|paused|starting|off, listed, skipped_today, reported_today,
    skipped_total, reported_total, synced, last_ok}."""
    on = enabled()
    st = _load()
    now = time.time()
    today = st.get("today") if isinstance(st.get("today"), dict) and st["today"].get("day") == utc_day(now) else {}
    totals = st.get("totals") if isinstance(st.get("totals"), dict) else {}
    ents = st.get("entries") or {}
    listed = sum(1 for e in ents.values() if isinstance(e, list) and e and _safe_expiry(e[0]) > now)
    last_ok = float(st.get("last_ok") or 0)
    if not on:
        state = "off"
    elif not last_ok:
        state = "paused" if int(st.get("fails") or 0) >= 1 else "starting"
    elif now - last_ok > PAUSE_AFTER and int(st.get("fails") or 0) >= 1:
        state = "paused"
    else:
        state = "on"
    return {
        "enabled": on,
        "state": state,
        "listed": max(listed, int(st.get("total") or 0)) if state == "on" else listed,
        "skipped_today": int(today.get("skipped") or 0),
        "reported_today": int(today.get("reported") or 0),
        "skipped_total": int(totals.get("skipped") or 0),
        "reported_total": int(totals.get("reported") or 0),
        "synced": float(st.get("synced") or 0),
        "last_ok": last_ok,
    }

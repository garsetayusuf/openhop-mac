"""Builds the diagnostic report the "Report a problem" button sends (stdlib only).

collect() gathers what's needed to debug the quota loop on someone else's PC: app + Windows version,
WARP status/mode/protocol, egress settings, the guard state, tails of the guard and keeper logs,
opencode.log ERROR lines, the last provider.free-limit rows from opencode.db, and a live check of
which IP each route really leaves from (WARP SOCKS :40000, the local proxy :41987, and direct).

Everything goes through redact() before it leaves the PC: home path and user name become "~"/<user>,
anything that looks like a token or password is dropped, IPv4 addresses lose their last octet and
IPv6 addresses keep only their /64. The real IP is never sent: only real_ip_leak (true/false), i.e.
whether the proxy route leaves from the same address as the direct one.
"""

from __future__ import annotations

import getpass
import ipaddress
import json
import os
import platform
import re
import shutil
import socket
import sqlite3
import ssl
import subprocess
import sys
import threading
import time
import urllib.request
from pathlib import Path

try:  # paths/version from the app when available; fall back to the same defaults otherwise
    from ouc_common import DEST_DIR, VERSION, opencode_db_path  # type: ignore
except Exception:  # pragma: no cover - only when run outside the app
    DEST_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "openhop"
    VERSION = "unknown"

    def opencode_db_path() -> Path:  # type: ignore[misc]
        return Path.home() / ".local" / "share" / "opencode" / "opencode.db"

HOME = Path.home()
GUARD_STATE = DEST_DIR / "guard" / "state.json"
GUARD_LOG = DEST_DIR / "guard" / "guard.log"
KEEPER_LOG = DEST_DIR / "keeper" / "keeper.log"
EGRESS_FILE = DEST_DIR / "lane" / "egress_mode.json"
STRICT_FLAG = DEST_DIR / "lane" / "strict_proxy.flag"
APP_SETTINGS = DEST_DIR / "app_settings.json"
OPENCODE_LOG = HOME / ".local" / "share" / "opencode" / "log" / "opencode.log"

TRACE_HOST = "cloudflare.com"
TRACE_PATH = "/cdn-cgi/trace"
TRACE_URL = f"https://{TRACE_HOST}{TRACE_PATH}"
PROXY_PORT = int(os.environ.get("OUC_SERVICE_PROXY_PORT", "41987"))
SOCKS_PORT = 40000
TRACE_TIMEOUT = 8.0
MAX_BUNDLE = 200_000  # characters of report text
CREATE_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0
_WARP_CANDIDATES = (
    Path(r"C:\Program Files\Cloudflare\Cloudflare WARP\warp-cli.exe"),
    Path(r"C:\Program Files (x86)\Cloudflare\Cloudflare WARP\warp-cli.exe"),
)


# ---------------------------------------------------------------- redaction

def _identity_words() -> list[str]:
    words = set()
    for v in (os.environ.get("USERNAME"), os.environ.get("USER"), HOME.name, os.environ.get("COMPUTERNAME")):
        if v and len(v) >= 3:
            words.add(v)
    try:
        u = getpass.getuser()
        if u and len(u) >= 3:
            words.add(u)
    except Exception:
        pass
    return sorted(words, key=len, reverse=True)


_HOME_VARIANTS: list[str] = []
for _h in {str(HOME), str(HOME).replace("\\", "/"), str(HOME).replace("\\", "\\\\")}:
    if len(_h) > 3:
        _HOME_VARIANTS.append(_h)
_HOME_VARIANTS.sort(key=len, reverse=True)
_IDENTITY = _identity_words()
_PC_NAME = (os.environ.get("COMPUTERNAME") or "").lower()

_SECRET_KV = re.compile(
    r"(?i)(\"?(?:password|passwd|pwd|passphrase|secret|token|access[_-]?token|refresh[_-]?token|id[_-]?token|"
    r"authorization|auth|api[_-]?key|apikey|x-api-key|cookie|set-cookie|session[_-]?key|private[_-]?key|"
    r"client[_-]?secret|webhook)\"?\s*[:=]\s*)(\"[^\"]*\"|'[^']*'|[^\s,;}&]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]{8,}")
_SK_KEY = re.compile(r"\b(?:sk|pk|rk|ghp|gho|github_pat|xox[abprs])[-_][A-Za-z0-9_-]{12,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_HEX = re.compile(r"\b[0-9a-fA-F]{32,}\b")
_LONG_B64 = re.compile(r"(?<![A-Za-z0-9+_=-])(?=[A-Za-z0-9+_-]*[0-9])(?=[A-Za-z0-9+_-]*[A-Z])"
                       r"(?=[A-Za-z0-9+_-]*[a-z])[A-Za-z0-9+_-]{40,}={0,2}(?![A-Za-z0-9+_=-])")
_URL_CREDS = re.compile(r"(?i)\b([a-z][a-z0-9+.-]*://)[^/\s:@]+:[^/\s@]+@")
_EMAIL = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
_USERS_DIR = re.compile(r"(?i)\b([A-Z]:[\\/]+Users[\\/]+)(?!Public\b|Default\b)[^\\/\s\"']+")
_IPV4 = re.compile(r"(?<![\d.])(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})(?![\d.])")
_IPV6_CAND = re.compile(r"(?<![0-9A-Za-z:.])[0-9A-Fa-f:]*:[0-9A-Fa-f:]*:[0-9A-Fa-f:.]*(?![0-9A-Za-z:])")

# Shared Cloudflare anycast / WARP entry ranges: not personal, and useful for debugging, so kept as-is.
_KEEP_NETS = [ipaddress.ip_network(n) for n in (
    "2606:4700::/32", "2a06:98c0::/29", "2803:f800::/32",
    "162.158.0.0/15", "104.16.0.0/13", "104.24.0.0/14", "172.64.0.0/13", "188.114.96.0/20",
    "1.1.1.0/24", "1.0.0.0/24",
)]


def _mask_v4(m: re.Match) -> str:
    parts = [int(x) for x in m.groups()]
    if any(p > 255 for p in parts):
        return m.group(0)
    addr = ipaddress.IPv4Address(".".join(map(str, parts)))
    if addr.is_loopback or addr.is_unspecified or addr.is_private or addr.is_link_local:
        return m.group(0)
    if any(addr in n for n in _KEEP_NETS):
        return m.group(0)
    return f"{parts[0]}.{parts[1]}.{parts[2]}.x"


def _mask_v6(m: re.Match) -> str:
    raw = m.group(0)
    s = raw.strip(":") if raw.count("::") == 0 else raw
    try:
        addr = ipaddress.IPv6Address(s)
    except ValueError:
        return raw
    if addr.is_loopback or addr.is_unspecified or addr.is_link_local:
        return raw
    if addr.ipv4_mapped:
        return "::ffff:" + _IPV4.sub(_mask_v4, str(addr.ipv4_mapped))
    if any(addr in n for n in _KEEP_NETS):
        return raw
    groups = addr.exploded.split(":")
    prefix = ":".join(g.lstrip("0") or "0" for g in groups[:4])
    host_zero = all(int(g, 16) == 0 for g in groups[4:])
    return f"{prefix}::" if host_zero else f"{prefix}::x"


def redact(text: str) -> str:
    """Strip anything personal or secret from a piece of report text."""
    if not text:
        return ""
    s = str(text)
    for h in _HOME_VARIANTS:
        s = re.sub(re.escape(h), "~", s, flags=re.IGNORECASE)
    s = _USERS_DIR.sub(r"\1<user>", s)
    s = _URL_CREDS.sub(r"\1<redacted>@", s)
    s = _BEARER.sub(lambda m: m.group(1) + " <redacted>", s)
    s = _SECRET_KV.sub(lambda m: m.group(1) + "<redacted>", s)
    s = _JWT.sub("<redacted>", s)
    s = _SK_KEY.sub("<redacted>", s)
    s = _LONG_HEX.sub("<hex>", s)
    s = _LONG_B64.sub("<redacted>", s)
    s = _EMAIL.sub("<email>", s)
    s = _IPV6_CAND.sub(_mask_v6, s)
    s = _IPV4.sub(_mask_v4, s)
    for w in _IDENTITY:
        repl = "<pc>" if w.lower() == _PC_NAME else "<user>"
        s = re.sub(re.escape(w), repl, s, flags=re.IGNORECASE)
    return s


def redact_obj(obj):
    """redact() applied to every string inside a dict/list (keys too), keeping the structure."""
    if isinstance(obj, dict):
        return {redact(str(k)): redact_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [redact_obj(v) for v in obj]
    if isinstance(obj, str):
        return redact(obj)
    return obj


# ---------------------------------------------------------------- small readers

def _tail(path: Path, lines: int, max_bytes: int = 1_500_000) -> list[str]:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read()
        text = data.decode("utf-8", errors="replace")
        out = text.splitlines()
        if size > max_bytes and out:
            out = out[1:]  # first line is probably cut in half
        return out[-lines:]
    except Exception:
        return []


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return None


def _age(ts) -> str:
    try:
        ts = float(ts)
    except (TypeError, ValueError):
        return ""
    if ts <= 0:
        return ""
    if ts > 1e12:  # ms
        ts /= 1000.0
    d = time.time() - ts
    if d < 0:
        return f"in {int(-d)}s"
    if d < 120:
        return f"{int(d)}s ago"
    if d < 7200:
        return f"{int(d // 60)}min ago"
    if d < 172800:
        return f"{d / 3600:.1f}h ago"
    return f"{d / 86400:.1f}d ago"


def _run(args: list[str], timeout: float = 8.0) -> str:
    try:
        res = subprocess.run(args, capture_output=True, text=True, errors="replace", timeout=timeout,
                             creationflags=CREATE_NO_WINDOW, stdin=subprocess.DEVNULL)
        return ((res.stdout or "") + ("\n" + res.stderr if res.stderr else "")).strip()
    except Exception as exc:
        return f"(could not run: {type(exc).__name__})"


def _find_warp_cli() -> str | None:
    on_path = shutil.which("warp-cli")
    if on_path:
        return on_path
    for c in _WARP_CANDIDATES:
        if c.is_file():
            return str(c)
    return None


# ---------------------------------------------------------------- sections

def windows_version() -> str:
    try:
        if sys.platform == "win32":
            v = sys.getwindowsversion()  # type: ignore[attr-defined]
            edition = ""
            try:
                edition = platform.win32_edition() or ""
            except Exception:
                pass
            return f"Windows {platform.release()} {edition} build {v.build}".replace("  ", " ")
        return platform.platform()
    except Exception:
        return platform.platform()


def warp_info() -> dict:
    exe = _find_warp_cli()
    if not exe:
        return {"found": False, "status": "warp-cli not found", "settings": [], "mode": "", "protocol": ""}
    status = _run([exe, "--accept-tos", "status"])
    settings_raw = _run([exe, "--accept-tos", "settings"])
    keep = [ln.strip() for ln in settings_raw.splitlines() if re.search(r"(?i)\bmode\b|protocol", ln)]
    mode = protocol = ""
    for ln in keep:
        m = re.search(r"(?i)\bmode:\s*(.+)$", ln)
        if m and not mode:
            mode = m.group(1).strip()
        m = re.search(r"(?i)protocol:\s*(.+)$", ln)
        if m and not protocol:
            protocol = m.group(1).strip()
    return {"found": True, "status": status[:1500], "settings": keep[:20], "mode": mode, "protocol": protocol}


def egress_info() -> dict:
    cfg = _read_json(EGRESS_FILE) or {}
    return {
        "mode": cfg.get("mode", "warp (default)") if cfg else "warp (default, no config file)",
        "warp_scope": cfg.get("warp_scope", "isolated (default)") if cfg else "isolated (default)",
        "custom_type": cfg.get("custom_type", "") if cfg.get("mode") == "custom" else "",
        "custom_proxy_count": len([p for p in str(cfg.get("custom_proxy", "")).split(",") if p.strip()])
        if cfg.get("mode") == "custom" else 0,
        "strict_proxy_flag": STRICT_FLAG.is_file(),
        "vpn_app": cfg.get("vpn_id", "") if cfg.get("mode") == "vpn" else "",
    }


def guard_summary() -> dict:
    st = _read_json(GUARD_STATE)
    if st is None:
        return {"found": False}
    bl = [str(x) for x in (st.get("blacklist") or [])]
    families: dict[str, int] = {}
    for p in bl:
        if ":" in p:
            fam = ":".join(p.split(":")[:2]) + ":*"
        else:
            fam = ".".join(p.split(".")[:2]) + ".*"
        families[fam] = families.get(fam, 0) + 1
    backoff = float(st.get("backoff_until") or 0)
    return {
        "found": True,
        "hops": st.get("hops", 0),
        "failed_hops": st.get("failed_hops", 0),
        "backoff_until": backoff,
        "backoff_active": backoff > time.time(),
        "backoff_left_s": max(0, int(backoff - time.time())) if backoff else 0,
        "blacklist_count": len(bl),
        "blacklist_families": dict(sorted(families.items(), key=lambda kv: -kv[1])),
        "current": st.get("current", ""),
        "last_hop": st.get("last_hop", 0),
        "last_hop_age": _age(st.get("last_hop")),
        "last_ok_hop_age": _age(st.get("last_ok_hop")),
        "pending_count": len(st.get("pending") or {}),
        "resumed_count": len(st.get("resumed") or {}),
    }


def heartbeat_ages() -> dict:
    out = {}
    for name in ("guard", "keeper"):
        hb = DEST_DIR / name / "heartbeat.txt"
        try:
            out[name] = _age(hb.stat().st_mtime) or "?"
        except Exception:
            out[name] = "missing"
    return out


def opencode_errors(limit: int = 80) -> list[str]:
    lines = _tail(OPENCODE_LOG, 6000, max_bytes=4_000_000)
    errs = [ln for ln in lines if "ERROR" in ln]
    return [ln[:1200] for ln in errs[-limit:]]


def free_limit_rows(limit: int = 20) -> list[dict]:
    OPENCODE_DB = opencode_db_path()
    if not OPENCODE_DB.is_file():
        return []
    rows: list[dict] = []
    try:
        uri = f"file:{OPENCODE_DB.as_posix()}?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=3.0)
        try:
            cur = con.execute(
                "SELECT session_id, type, time_created, data FROM session_message "
                "WHERE data LIKE '%provider.free-limit%' AND json_valid(data) "
                "AND json_extract(data, '$.error.type') = 'provider.free-limit' "
                "ORDER BY time_created DESC LIMIT ?", (int(limit),))
            for sid, typ, created, data in cur.fetchall():
                try:
                    d = json.loads(data)
                except Exception:
                    continue
                err = d.get("error") or {}
                body = ((err.get("response") or {}).get("body") or "")
                model = (d.get("model") or {}).get("id", "") if isinstance(d.get("model"), dict) else ""
                rows.append({
                    "at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime((created or 0) / 1000)),
                    "age": _age(created),
                    "session": str(sid)[-8:],
                    "row": typ,
                    "status": err.get("status"),
                    "retryAfterMs": err.get("retryAfterMs", d.get("retryAfterMs")),
                    "model": model,
                    "body": str(body)[:400],
                })
        finally:
            con.close()
    except Exception as exc:
        rows.append({"error": f"could not read opencode.db ({type(exc).__name__})"})
    return rows


# ---------------------------------------------------------------- live egress check

def _parse_trace(text: str) -> dict:
    out = {}
    for ln in text.splitlines():
        if "=" in ln:
            k, v = ln.split("=", 1)
            if k in ("ip", "warp", "colo", "http", "tls"):
                out[k] = v.strip()
    return out


def _trace_urllib(proxy: str | None) -> dict:
    t0 = time.time()
    try:
        handler = urllib.request.ProxyHandler({"http": proxy, "https": proxy} if proxy else {})
        opener = urllib.request.build_opener(handler)
        req = urllib.request.Request(TRACE_URL, headers={"User-Agent": f"openhop/{VERSION} report", "Cache-Control": "no-store"})
        with opener.open(req, timeout=TRACE_TIMEOUT) as r:
            text = r.read(4096).decode("utf-8", errors="replace")
        res = _parse_trace(text)
        res.update(ok=bool(res.get("ip")), ms=int((time.time() - t0) * 1000))
        return res
    except Exception as exc:
        return {"ok": False, "error": _short_err(exc), "ms": int((time.time() - t0) * 1000)}


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("SOCKS closed")
        buf += chunk
    return buf


def _trace_socks5(port: int = SOCKS_PORT) -> dict:
    """GET the trace through a SOCKS5 proxy on 127.0.0.1:<port> (no auth, remote DNS)."""
    t0 = time.time()
    deadline = t0 + TRACE_TIMEOUT
    try:
        raw = socket.create_connection(("127.0.0.1", port), timeout=TRACE_TIMEOUT)
        try:
            raw.settimeout(max(0.5, deadline - time.time()))
            raw.sendall(b"\x05\x01\x00")
            if _recv_exact(raw, 2) != b"\x05\x00":
                raise ConnectionError("SOCKS auth refused")
            host = TRACE_HOST.encode()
            raw.sendall(b"\x05\x01\x00\x03" + bytes([len(host)]) + host + (443).to_bytes(2, "big"))
            head = _recv_exact(raw, 4)
            if head[1] != 0:
                raise ConnectionError(f"SOCKS connect failed (code {head[1]})")
            atyp = head[3]
            skip = {1: 4, 4: 16}.get(atyp)
            if skip is None:
                skip = _recv_exact(raw, 1)[0]
            _recv_exact(raw, skip + 2)
            ctx = ssl.create_default_context()
            tls = ctx.wrap_socket(raw, server_hostname=TRACE_HOST)
            tls.settimeout(max(0.5, deadline - time.time()))
            tls.sendall((f"GET {TRACE_PATH} HTTP/1.1\r\nHost: {TRACE_HOST}\r\nUser-Agent: openhop/{VERSION} report\r\n"
                         "Accept: */*\r\nConnection: close\r\n\r\n").encode())
            data = b""
            while len(data) < 16384:
                chunk = tls.recv(4096)
                if not chunk:
                    break
                data += chunk
            tls.close()
        finally:
            try:
                raw.close()
            except Exception:
                pass
        text = data.decode("utf-8", errors="replace")
        body = text.split("\r\n\r\n", 1)[-1]
        res = _parse_trace(body)
        res.update(ok=bool(res.get("ip")), ms=int((time.time() - t0) * 1000))
        return res
    except Exception as exc:
        return {"ok": False, "error": _short_err(exc), "ms": int((time.time() - t0) * 1000)}


def _short_err(exc: BaseException) -> str:
    msg = str(getattr(exc, "reason", "") or exc) or type(exc).__name__
    return redact(f"{type(exc).__name__}: {msg}")[:200]


def _proxy_connect_ok(host: str, port: int, timeout: float = 8.0) -> bool:
    """Does the local proxy open a tunnel to host:port? (CONNECT only, nothing is sent through it.)"""
    try:
        with socket.create_connection(("127.0.0.1", PROXY_PORT), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
            head = s.recv(256).decode("latin-1", errors="replace")
            return head.startswith("HTTP/") and head.split(" ", 2)[1:2] == ["200"]
    except Exception:
        return False


def egress_check() -> dict:
    """Which IP does each route leave from? Runs the three probes in parallel (<= ~8 s)."""
    results: dict[str, dict] = {}
    jobs = {
        "warp_socks_40000": lambda: _trace_socks5(SOCKS_PORT),
        "local_proxy_41987": lambda: _trace_urllib(f"http://127.0.0.1:{PROXY_PORT}"),
        "direct": lambda: _trace_urllib(None),
    }

    def run(name, fn):
        try:
            results[name] = fn()
        except Exception as exc:  # pragma: no cover
            results[name] = {"ok": False, "error": _short_err(exc)}

    threads = [threading.Thread(target=run, args=(n, f), daemon=True) for n, f in jobs.items()]
    for t in threads:
        t.start()
    for t in threads:
        t.join(TRACE_TIMEOUT + 3)
    for n in jobs:
        results.setdefault(n, {"ok": False, "error": "timed out"})

    direct = results["direct"]
    proxy = results["local_proxy_41987"]
    socks = results["warp_socks_40000"]
    direct_ip = direct.get("ip") if direct.get("ok") else None

    def same(r: dict):
        if not (r.get("ok") and direct_ip):
            return None
        return r.get("ip") == direct_ip

    # A leak means OpenCode's route (the local proxy) leaves from the same address as the PC itself while
    # that address isn't WARP. With full-PC WARP both are WARP, which is fine.
    leak = None
    opencode_held = None
    if proxy.get("ok") and direct_ip:
        leak = proxy.get("ip") == direct_ip and direct.get("warp") != "on"
        if leak:
            # A VPN app (Your VPN) changes the IP of the whole PC, so "direct" leaves through it too.
            try:
                from proxy_chain import get_egress_config
                cfg = get_egress_config()
                if cfg.get("mode") == "vpn":
                    from vpn_detect import vpn_exit_ip
                    if proxy.get("ip") == vpn_exit_ip(str(cfg.get("vpn_id") or ""), timeout=6.0):
                        leak = False
            except Exception:
                pass
        if leak:
            # The trace host isn't OpenCode: when the protected route is down, other sites go direct by
            # design. It's only a leak if the proxy also lets OpenCode itself out (it should refuse it).
            opencode_held = not _proxy_connect_ok("opencode.ai", 443)
            if opencode_held:
                leak = False
    out = {
        "real_ip_leak": leak,
        "opencode_held_back": opencode_held,
        "proxy_same_as_direct": same(proxy),
        "socks_same_as_direct": same(socks),
        "proxy_same_as_socks": (proxy.get("ip") == socks.get("ip")) if proxy.get("ok") and socks.get("ok") else None,
        "routes": {},
    }
    for name, r in results.items():
        clean = {k: v for k, v in r.items() if k != "ip"}
        if r.get("ip"):
            clean["ip"] = redact(r["ip"])
        out["routes"][name] = clean
    return out


# ---------------------------------------------------------------- bundle

def tor_info() -> dict:
    """Why Tor does or doesn't skip used-up exits: is it openhop's own Tor, and does openhop control it?

    Only facts: which kind of Tor is answering, whether port 9051 is that same Tor, and how many exits it was
    told to skip. No paths, no IPs.
    """
    try:
        import tor_manager as tm  # type: ignore
    except Exception as exc:
        return {"error": f"tor_manager not loaded: {_short_err(exc)}"}
    out: dict = {}
    try:
        exe = tm.find_tor_exe()
        if not exe:
            out["tor_exe"] = "not found"
        elif str(exe).lower().startswith(str(tm.TOR_DIR).lower()):
            out["tor_exe"] = "openhop portable"
        elif "tor browser" in str(exe).lower():
            out["tor_exe"] = "Tor Browser"
        else:
            out["tor_exe"] = "other (system Tor)"
        out["pid_file"] = tm.TOR_PID_FILE.is_file()
        out["portable_running"] = tm.is_portable_tor_running()
        out["socks_9050_alive"] = tm.test_socks5_alive(tm.TOR_DEFAULT_SOCKS_PORT)
        out["socks_9150_alive (Tor Browser)"] = tm.test_socks5_alive(tm.TOR_BROWSER_SOCKS_PORT)
        tgt = tm.get_active_tor_target()
        out["used_for_opencode"] = f"port {tgt[1]}" if tgt else "none"
        res = tm.tor_control(["GETINFO process/pid", "GETCONF ExcludeExitNodes"])
        if res is None:
            out["control_9051"] = "no answer (closed, or needs a password)"
        else:
            out["control_9051"] = "answers"
            ctl_pid = ""
            for line in res[0][1] if res and res[0][0] == 250 else []:
                if line.startswith("process/pid="):
                    ctl_pid = line.split("=", 1)[1].strip()
            try:
                my_pid = tm.TOR_PID_FILE.read_text(encoding="utf-8").strip()
            except Exception:
                my_pid = ""
            out["control_is_openhop_tor"] = bool(ctl_pid) and ctl_pid == my_pid
            excl = ""
            if len(res) > 1 and res[1][0] == 250:
                for line in res[1][1]:
                    if line.startswith("ExcludeExitNodes"):
                        excl = line.split("=", 1)[1].strip().strip('"') if "=" in line else ""
            out["skipping_exits_now"] = len([x for x in excl.split(",") if x.strip()])
        out["owns_tor_control"] = tm.owns_tor_control()
    except Exception as exc:
        out["error"] = _short_err(exc)
    st = _read_json(GUARD_STATE) or {}
    now = time.time()
    out["used_up_exits_known_today"] = len([a for a, until in (st.get("burnt_until") or {}).items()
                                            if a and ":" not in a and float(until or 0) > now])
    return out


def _opencode_install() -> str:
    """Which OpenCode CLI openhop sends 'continue' with, its version, and every opencode*.db (which one is used).

    A report said "continue doesn't get sent" while the guard log said it was; this is what tells a
    second OpenCode install or a different database apart."""
    lines = []
    try:
        from ouc_common import find_opencode_cli  # type: ignore
        oc = find_opencode_cli()
    except Exception as exc:
        oc, lines = None, [f"cli lookup failed: {type(exc).__name__}"]
    lines.append(f"cli: {oc or '(not found)'}")
    if oc:
        try:
            r = subprocess.run([oc, "--version"], capture_output=True, text=True, errors="replace", timeout=15,
                               creationflags=0x08000000 if os.name == "nt" else 0)
            lines.append(f"cli version: {(r.stdout or r.stderr).strip()[:80]}")
        except Exception as exc:
            lines.append(f"cli version: failed ({type(exc).__name__})")
    used = opencode_db_path()
    lines.append(f"database openhop reads: {used.name}")
    data_dir = used.parent
    try:
        for db in sorted(data_dir.glob("opencode*.db*")):
            st = db.stat()
            lines.append(f"  {db.name}: {st.st_size // 1024} KB, changed {_age(st.st_mtime * 1000)}")
    except Exception as exc:
        lines.append(f"  (couldn't list: {type(exc).__name__})")
    storage = data_dir / "storage"
    if storage.is_dir():
        lines.append("  storage\\ folder present (older OpenCode kept chats there)")
    return "\n".join(lines)


def _continue_info() -> str:
    """resume_result.json and the output of the newest 'opencode run … continue' (guard\\continue\\*.log)."""
    out = []
    res = DEST_DIR / "guard" / "resume_result.json"
    try:
        data = json.loads(res.read_text(encoding="utf-8"))
        for sid, v in sorted(data.items(), key=lambda kv: -float((kv[1] or {}).get("at", 0) or 0))[:6]:
            ok = {True: "arrived", False: "DID NOT arrive", None: "couldn't check"}.get(v.get("ok"), "?")
            out.append(f"{sid[-8:]}: {ok} · {_age(float(v.get('at', 0) or 0) * 1000)}"
                       + (f" · {v.get('reason')}" if v.get("reason") else ""))
    except Exception:
        out.append("(no results yet)")
    logs = sorted((DEST_DIR / "guard" / "continue").glob("*.log"), key=lambda p: p.stat().st_mtime, reverse=True) \
        if (DEST_DIR / "guard" / "continue").is_dir() else []
    if logs:
        out.append(f"\nnewest output ({logs[0].name[-12:]}, {_age(logs[0].stat().st_mtime * 1000)}), last 25 lines:")
        out.extend(_tail(logs[0], 25))
    return "\n".join(out)


def _section(title: str, body: str) -> str:
    return f"\n===== {title} =====\n{body.rstrip()}\n"


def _fmt_kv(d: dict, indent: str = "  ") -> str:
    lines = []
    for k, v in d.items():
        if isinstance(v, dict):
            lines.append(f"{indent}{k}:")
            lines.append(_fmt_kv(v, indent + "  "))
        else:
            lines.append(f"{indent}{k}: {v}")
    return "\n".join(lines)


def collect(extra: dict | None = None, live: bool = True) -> dict:
    """Build the report. Returns {"text": redacted report, "summary": small dict for the Discord embed}."""
    started = time.time()
    warp = warp_info()
    egress = egress_info()
    guard = guard_summary()
    # Only when Tor is in play: it asks Tor's control port, which takes up to a few seconds when nothing answers.
    tor = tor_info() if (egress["mode"] == "tor" or (DEST_DIR / "tor" / "tor.pid").is_file()) else None
    hb = heartbeat_ages()
    limits = free_limit_rows(20)
    check = egress_check() if live else {"skipped": True}
    settings = _read_json(APP_SETTINGS) or {}

    last_limit = next((r for r in limits if "status" in r), None)
    summary = {
        "version": VERSION,
        "os": windows_version(),
        "frozen": bool(getattr(sys, "frozen", False)),
        "warp_mode": warp.get("mode", ""),
        "warp_protocol": warp.get("protocol", ""),
        "warp_status": (warp.get("status", "").splitlines() or [""])[0][:120],
        "egress_mode": egress["mode"],
        "warp_scope": egress["warp_scope"],
        "strict": egress["strict_proxy_flag"],
        "real_ip_leak": check.get("real_ip_leak"),
        "hops": guard.get("hops"),
        "failed_hops": guard.get("failed_hops"),
        "backoff_active": guard.get("backoff_active"),
        "blacklist_count": guard.get("blacklist_count"),
        "tor_owned": tor.get("owns_tor_control") if tor else None,
        "last_hop_age": guard.get("last_hop_age"),
        "free_limit_rows": len([r for r in limits if "status" in r]),
        "last_free_limit": ({"at": last_limit.get("at"), "age": last_limit.get("age"),
                             "status": last_limit.get("status"), "retryAfterMs": last_limit.get("retryAfterMs")}
                            if last_limit else None),
        "routes": {k: {"ok": v.get("ok"), "warp": v.get("warp", ""), "colo": v.get("colo", "")}
                   for k, v in (check.get("routes") or {}).items()},
    }

    parts = [
        f"openhop diagnostic report\nCreated: {time.strftime('%Y-%m-%d %H:%M:%S %z')}\n"
        f"App: openhop {VERSION} ({'exe' if summary['frozen'] else 'python'}) · Python {platform.python_version()}\n"
        f"OS: {summary['os']}\n"
        "Personal data removed on the user's PC: home path, user name, tokens, last part of every IP.\n"
    ]
    parts.append(_section("Live route check (cloudflare.com/cdn-cgi/trace)", _fmt_kv(check)))
    parts.append(_section("Cloudflare WARP", f"warp-cli status:\n{warp.get('status', '')}\n\nsettings (mode/protocol lines):\n"
                          + "\n".join(warp.get("settings", []))))
    parts.append(_section("Connection settings", _fmt_kv(egress)))
    parts.append(_section("Auto-rotate state (guard/state.json)", _fmt_kv(guard)))
    if tor is not None:
        parts.append(_section("Tor (does openhop control it? used-up exits skipped?)", _fmt_kv(tor)))
    parts.append(_section("Background workers (heartbeat age)", _fmt_kv(hb)))
    if settings:
        parts.append(_section("App settings", _fmt_kv({k: v for k, v in settings.items() if not isinstance(v, (dict, list))})))
    if extra:
        parts.append(_section("App health panel", json.dumps(extra, indent=2, ensure_ascii=False, default=str)[:20000]))
    lim_txt = "\n".join(json.dumps(r, ensure_ascii=False) for r in limits) or "(none found)"
    parts.append(_section("Last provider.free-limit rows (opencode.db, newest first)", lim_txt))
    parts.append(_section("OpenCode install (CLI used for 'continue', databases)", _opencode_install()))
    parts.append(_section("Last 'continue' from openhop", _continue_info()))
    parts.append(_section("opencode.log ERROR lines (last 80)", "\n".join(opencode_errors(80)) or "(none)"))
    parts.append(_section("guard.log (last 200 lines)", "\n".join(_tail(GUARD_LOG, 200)) or "(empty)"))
    parts.append(_section("keeper.log (last 120 lines)", "\n".join(_tail(KEEPER_LOG, 120)) or "(empty)"))
    # Why an automatic update didn't install ("auto update popped out errors") was invisible before.
    parts.append(_section("update.log (last 30 lines)",
                          "\n".join(_tail(DEST_DIR / "logs" / "update.log", 30)) or "(empty)"))
    parts.append(f"\n(collected in {time.time() - started:.1f}s)\n")

    text = redact("".join(parts))
    if len(text) > MAX_BUNDLE:
        text = text[:MAX_BUNDLE - 80] + "\n\n[... report cut here: it was longer than 200 KB ...]\n"
    summary = redact_obj(summary)
    return {"text": text, "summary": summary}


if __name__ == "__main__":  # manual check: python src\report_bundle.py
    b = collect()
    sys.stdout.reconfigure(errors="replace")  # type: ignore[attr-defined]
    print(b["text"])
    print(json.dumps(b["summary"], indent=2))

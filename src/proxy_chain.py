"""Multi-egress proxy router: supports Cloudflare WARP, Tor Network, and Custom SOCKS5/HTTP."""

from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Literal

from ouc_common import DEFAULT_WARP_SCOPE, LANE_DIR, L, ensure_dirs
from tor_manager import (
    get_active_tor_target,
    open_socks5_tunnel,
    start_portable_tor,
)

import sys

EgressType = Literal["warp", "tor", "custom", "vpn"]
# Kept across every set_egress_config() call: they're preferences, not part of one connection type.
STICKY_KEYS = ("vpn_id",)
EGRESS_CONFIG_FILE = LANE_DIR / "egress_mode.json"


def get_default_warp_scope() -> str:
    return DEFAULT_WARP_SCOPE


def get_egress_config() -> dict:
    """Read the active egress routing configuration."""
    default_scope = get_default_warp_scope()
    if EGRESS_CONFIG_FILE.is_file():
        try:
            cfg = json.loads(EGRESS_CONFIG_FILE.read_text(encoding="utf-8"))
            if "warp_scope" not in cfg:
                cfg["warp_scope"] = default_scope
            return cfg
        except Exception:
            pass
    return {
        "mode": "warp",
        "warp_scope": default_scope,
        "custom_proxy": "127.0.0.1:10808",
        "custom_type": "socks5",
    }


AUTO_TOR_KEYS = ("auto_tor_until", "auto_tor_since")


def set_egress_config(
    mode: EgressType,
    custom_proxy: str = "127.0.0.1:10808",
    custom_type: str = "socks5",
    warp_scope: str | None = None,
    extra: dict | None = None,
) -> dict:
    """Save the active egress configuration.

    Any other key (e.g. the auto-Tor mark) is dropped unless passed in `extra`, so a connection type the
    user picks by hand is never treated as one openhop picked by itself.
    """
    ensure_dirs()
    if warp_scope is None or not warp_scope.strip():
        chosen_scope = get_default_warp_scope()
    else:
        chosen_scope = "tun" if warp_scope.lower().strip() == "tun" else "isolated"
    cfg = {
        "mode": mode,
        "warp_scope": chosen_scope,
        "custom_proxy": custom_proxy.strip(),
        "custom_type": custom_type.lower().strip(),
    }
    old = get_egress_config()
    for key in STICKY_KEYS:
        if key in old:
            cfg[key] = old[key]
    if extra:
        cfg.update(extra)
    EGRESS_CONFIG_FILE.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    try:
        (LANE_DIR / "custom_proxy_index.txt").write_text("0", encoding="utf-8")
    except Exception:
        pass
    return cfg


def switch_to_tor(wait: bool = False, auto_until: float = 0.0) -> tuple[bool, str]:
    """Send OpenCode through Tor. One code path for the app's "Try Tor" fix and Zen Guard's automatic switch.

    wait=True: start Tor first and change nothing if it can't start (Zen Guard then stays on WARP).
    wait=False: switch now and start Tor in the background (the app's button must not block).
    auto_until > 0 marks the switch as automatic: Zen Guard goes back to WARP after that time.
    The custom proxy list and the WARP scope are kept.
    """
    import threading
    import time

    from ouc_common import bump_egress_epoch

    msg = ""
    if wait:
        ok, msg = start_portable_tor()
        if not ok:
            return False, msg
    cfg = get_egress_config()
    extra = {"auto_tor_until": float(auto_until), "auto_tor_since": time.time()} if auto_until else None
    set_egress_config("tor", str(cfg.get("custom_proxy", "127.0.0.1:10808")), str(cfg.get("custom_type", "socks5")),
                      cfg.get("warp_scope"), extra=extra)
    if not wait and not get_active_tor_target():
        threading.Thread(target=start_portable_tor, daemon=True).start()
    bump_egress_epoch()   # close OpenCode's open connections so they reconnect through Tor
    return True, msg


def switch_back_from_auto_tor(now: float | None = None) -> bool:
    """Back to WARP after an automatic Tor switch, once its time is up. Never touches a Tor mode the user picked.

    Open connections are left to finish on Tor (no epoch bump): nothing is being fixed, so a reply that is
    still streaming shouldn't be cut; new connections go through WARP.
    """
    import time

    now = time.time() if now is None else now
    cfg = get_egress_config()
    until = float(cfg.get("auto_tor_until", 0) or 0)
    if cfg.get("mode") != "tor" or not until or now < until:
        return False
    set_egress_config("warp", str(cfg.get("custom_proxy", "127.0.0.1:10808")), str(cfg.get("custom_type", "socks5")),
                      cfg.get("warp_scope"))
    return True


PROXY_INDEX_FILE = LANE_DIR / "custom_proxy_index.txt"


def parse_proxy_list(raw: str) -> list[tuple[str, int]]:
    """Parse comma, semicolon, space, or newline delimited proxies."""
    import re
    items = re.split(r"[,;\s\n\r]+", raw.strip())
    results = []
    for item in items:
        clean = item.strip()
        if not clean:
            continue
        for prefix in ("socks5://", "socks5h://", "http://", "https://"):
            if clean.lower().startswith(prefix):
                clean = clean[len(prefix):]
        clean = clean.strip("/ ")
        phost, sep, pport_s = clean.rpartition(":")
        if sep and pport_s.isdigit():
            results.append((phost.strip("[] ") or "127.0.0.1", int(pport_s)))
        elif clean.isdigit():
            results.append(("127.0.0.1", int(clean)))
    return results or [("127.0.0.1", 10808)]


def get_current_custom_proxy() -> tuple[str, int, int, int]:
    """Return (host, port, current_index, total_count)."""
    cfg = get_egress_config()
    raw = cfg.get("custom_proxy", "127.0.0.1:10808").strip()
    proxies = parse_proxy_list(raw)
    idx = 0
    if PROXY_INDEX_FILE.is_file():
        try:
            idx = int(PROXY_INDEX_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            idx = 0
    idx = idx % len(proxies)
    host, port = proxies[idx]
    return host, port, idx, len(proxies)


def rotate_custom_proxy() -> tuple[bool, str]:
    """Rotate to the next proxy node in the custom proxy pool."""
    ensure_dirs()
    cfg = get_egress_config()
    raw = cfg.get("custom_proxy", "127.0.0.1:10808").strip()
    proxies = parse_proxy_list(raw)
    if len(proxies) <= 1:
        return True, L(f"Only one proxy ({proxies[0][0]}:{proxies[0][1]}). Add more (comma-separated) to switch between them automatically.",
                       f"Solo hay un proxy ({proxies[0][0]}:{proxies[0][1]}). Añade más (separados por comas) para cambiar entre ellos automáticamente.")
    idx = 0
    if PROXY_INDEX_FILE.is_file():
        try:
            idx = int(PROXY_INDEX_FILE.read_text(encoding="utf-8").strip())
        except Exception:
            idx = 0
    new_idx = (idx + 1) % len(proxies)
    PROXY_INDEX_FILE.write_text(str(new_idx), encoding="utf-8")
    host, port = proxies[new_idx]
    return True, L(f"Switched to proxy #{new_idx + 1}/{len(proxies)}: {host}:{port}",
                   f"Cambiado al proxy n.º {new_idx + 1}/{len(proxies)}: {host}:{port}")


def open_http_proxy_tunnel(dest_host: str, dest_port: int, proxy_host: str, proxy_port: int, timeout: float = 12.0) -> socket.socket:
    """Connect to an upstream HTTP proxy and negotiate CONNECT tunnel."""
    s = socket.create_connection((proxy_host, proxy_port), timeout=timeout)
    req = (
        f"CONNECT {dest_host}:{dest_port} HTTP/1.1\r\n"
        f"Host: {dest_host}:{dest_port}\r\n"
        f"Proxy-Connection: Keep-Alive\r\n\r\n"
    )
    s.sendall(req.encode("ascii"))
    resp = b""
    while b"\r\n\r\n" not in resp:
        chunk = s.recv(4096)
        if not chunk:
            s.close()
            raise ConnectionError("Upstream HTTP proxy closed connection.")
        resp += chunk
    status_line = resp.split(b"\r\n", 1)[0].decode("latin-1")
    if " 200 " not in status_line:
        s.close()
        raise ConnectionError(f"Upstream HTTP proxy rejected CONNECT: {status_line}")
    s.settimeout(None)
    return s


def open_multi_egress_upstream(host: str, port: int, warp_fallback_fn) -> socket.socket:
    """Route an outbound connection according to the active egress mode with auto-healing and failover."""
    cfg = get_egress_config()
    mode = cfg.get("mode", "warp")

    if mode == "tor":
        target = get_active_tor_target()
        if not target:
            ok, msg = start_portable_tor()
            if not ok:
                if warp_fallback_fn and not (os.environ.get("OUC_STRICT_PROXY") == "1" or (LANE_DIR / "strict_proxy.flag").is_file()):
                    return warp_fallback_fn(host, port)
                raise ConnectionError(f"Could not connect via Tor: {msg}")
            target = get_active_tor_target() or ("127.0.0.1", 9050)
        try:
            return open_socks5_tunnel(host, port, socks_host=target[0], socks_port=target[1], timeout=10.0)
        except Exception:
            if warp_fallback_fn and not (os.environ.get("OUC_STRICT_PROXY") == "1" or (LANE_DIR / "strict_proxy.flag").is_file()):
                return warp_fallback_fn(host, port)
            raise

    if mode == "custom":
        ptype = cfg.get("custom_type", "socks5").lower()
        raw = cfg.get("custom_proxy", "127.0.0.1:10808").strip()
        proxies = parse_proxy_list(raw)
        tries = max(1, len(proxies))
        last_err = None

        for _ in range(tries):
            phost, pport, idx, tot = get_current_custom_proxy()
            if ptype == "http":
                try:
                    return open_http_proxy_tunnel(host, port, phost, pport, timeout=8.0)
                except Exception as e:
                    last_err = e
            else:
                try:
                    return open_socks5_tunnel(host, port, socks_host=phost, socks_port=pport, timeout=4.0)
                except Exception as e:
                    last_err = e
                    # Seamlessly fallback to HTTP CONNECT if SOCKS5 fails or was misconfigured
                    try:
                        return open_http_proxy_tunnel(host, port, phost, pport, timeout=5.0)
                    except Exception:
                        pass
            # If current node failed and we have more nodes, advance pool to next node
            if tot > 1:
                rotate_custom_proxy()

        if warp_fallback_fn and not (os.environ.get("OUC_STRICT_PROXY") == "1" or (LANE_DIR / "strict_proxy.flag").is_file()):
            try:
                return warp_fallback_fn(host, port)
            except Exception:
                pass
        raise ConnectionError(f"All proxies in pool failed (last error: {last_err})")

    if mode == "vpn":
        # A VPN app the user has (Norton, Proton VPN, ...): bind to its adapter. No fallback here: if the VPN
        # is off, the caller decides (OpenCode must never go out with the user's own IP).
        from vpn_detect import open_via_vpn
        return open_via_vpn(host, port, str(cfg.get("vpn_id") or ""))

    # Default: Cloudflare WARP IPv6 socket binding
    return warp_fallback_fn(host, port)

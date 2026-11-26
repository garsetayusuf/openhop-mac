"""Local IPv6-pinning HTTP/CONNECT proxy bound to Cloudflare WARP on Windows."""

from __future__ import annotations

import os
import random
import select
import socket
import socketserver
import sys
import threading
import time
import urllib.parse

from ouc_common import (
    EGRESS_EPOCH_FILE,
    KEEPER_LOG_FILE,
    LANE_DIR,
    append_log,
    ensure_dirs,
    fetch_opencode_aaaa_pool,
    get_warp_local_v6,
    is_keeper_paused,
    normalize_v6_64,
)

PINNED_HOSTS = {
    "opencode.ai",
    "www.opencode.ai",
    "api.opencode.ai",
    "opencode.com",
    "www.opencode.com",
    "api.opencode.com",
    "inference.opencode.ai",
}


# Bumped (via EGRESS_EPOCH_FILE) whenever the exit IP changes. OpenCode tunnels opened before that are
# closed, so the next request goes out on the new IP instead of reusing a connection on the old one.
_EPOCH = {"v": 0.0}


def _file_epoch() -> float:
    try:
        return EGRESS_EPOCH_FILE.stat().st_mtime
    except OSError:
        return 0.0


def _is_opencode(host: str) -> bool:
    host = (host or "").strip("[]").lower()
    return host in PINNED_HOSTS or host.endswith(".opencode.ai") or host.endswith(".opencode.com")


class _WarpState:
    """Thread-safe cache for active WARP IPv6 and auto-rotated pinned edge."""

    def __init__(self, pool: list[str], initial_edge: str, ttl: float = 1.5) -> None:
        self._ttl = ttl
        self._lock = threading.Lock()
        self._ts = 0.0
        self._v6: str | None = None
        self._last_prefix: str = ""
        self._pool = pool
        self._edge = initial_edge
        self._reroll_mtime = 0.0

    def _apply_edge_controls(self) -> None:
        override_file = LANE_DIR / "edge_override.txt"
        reroll_file = LANE_DIR / "edge_reroll"
        if not os.environ.get("OUC_PIN_EDGE") and override_file.is_file():
            try:
                val = override_file.read_text(encoding="utf-8").strip()
                if val and val.lower() != "auto" and ":" in val:
                    self._edge = val
                    (LANE_DIR / "last_edge.txt").write_text(val + "\n", encoding="utf-8")
                    return
            except Exception:
                pass
        if reroll_file.is_file():
            try:
                mtime = reroll_file.stat().st_mtime
                if mtime > self._reroll_mtime:
                    self._reroll_mtime = mtime
                    fresh_pool = fetch_opencode_aaaa_pool() or self._pool
                    self._pool = fresh_pool
                    choices = [c for c in fresh_pool if c != self._edge] or fresh_pool
                    self._edge = random.choice(choices)
                    (LANE_DIR / "last_edge.txt").write_text(self._edge + "\n", encoding="utf-8")
            except Exception:
                pass

    def poll_controls(self) -> None:
        """Refresh edge override / reroll state immediately without waiting for traffic."""
        with self._lock:
            self._apply_edge_controls()

    def get(self, wait_if_rotating: bool = True) -> tuple[str | None, str]:
        deadline = time.monotonic() + (18.0 if wait_if_rotating else 0.0)
        while True:
            now = time.monotonic()
            with self._lock:
                if now - self._ts < self._ttl and self._v6:
                    return self._v6, self._edge
            v6 = get_warp_local_v6()
            with self._lock:
                self._ts = time.monotonic()
                self._v6 = v6
                if v6:
                    prefix = normalize_v6_64(v6)
                    if prefix and self._last_prefix and prefix != self._last_prefix:
                        if not os.environ.get("OUC_PIN_EDGE"):
                            fresh_pool = fetch_opencode_aaaa_pool() or self._pool
                            self._pool = fresh_pool
                            self._edge = random.choice(fresh_pool)
                            try:
                                (LANE_DIR / "last_edge.txt").write_text(
                                    self._edge + "\n", encoding="utf-8"
                                )
                            except Exception:
                                pass
                    if prefix:
                        self._last_prefix = prefix
                self._apply_edge_controls()
                edge = self._edge
            if v6:
                return v6, edge
            if not wait_if_rotating or not is_keeper_paused() or time.monotonic() >= deadline:
                return None, edge
            time.sleep(0.75)


class _ThreadedTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    # On Windows SO_REUSEADDR lets a second process bind the same port and silently
    # steal connections, so use an exclusive bind there instead.
    allow_reuse_address = sys.platform != "win32"
    daemon_threads = True

    def server_bind(self) -> None:
        if sys.platform == "win32" and hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            try:
                self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            except OSError:
                pass
        super().server_bind()


_last_err_log: dict[str, float] = {}


class NoRealIpError(ConnectionRefusedError):
    """The only way out for OpenCode would be the user's own IP. Never fall back to a direct connection."""


NO_REAL_IP_REASON = (
    "openhop: WARP isn't protecting the connection right now, so OpenCode was not sent out with your "
    "own IP. openhop is reconnecting WARP - try again in a minute"
)


def _is_strict() -> bool:
    return os.environ.get("OUC_STRICT_PROXY") == "1" or (LANE_DIR / "strict_proxy.flag").is_file()


def _friendly_reason(host: str, exc: BaseException, strict: bool) -> str:
    """Short, human-readable 503 reason shown by OpenCode (raw details go to the keeper log)."""
    now = time.monotonic()
    if now - _last_err_log.get(host, 0.0) > 30.0:
        _last_err_log[host] = now
        try:
            append_log(KEEPER_LOG_FILE, f"proxy: could not reach {host}: {type(exc).__name__}: {exc}")
        except Exception:
            pass
    if strict:
        return "openhop: WARP is down and Strict Fail-Closed mode is ON - reconnect WARP or turn Strict mode off"
    return f"openhop: cannot reach {host} - check your internet / WARP connection"


def _relay(a: socket.socket, b: socket.socket, watch_epoch: bool = False) -> None:
    """Bidirectionally forward bytes between two sockets with TCP keepalive and resilient streaming.

    watch_epoch: close the tunnel as soon as the exit IP changes (OpenCode tunnels only).
    """
    sockets = [a, b]
    start_epoch = _EPOCH["v"]
    closed = set()
    for s in (a, b):
        try:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
            # Windows TCP keepalive parameters: idle 15s, interval 5s
            if hasattr(socket, "SIO_KEEPALIVE_VALS"):
                s.ioctl(socket.SIO_KEEPALIVE_VALS, (1, 15000, 5000))
            # Linux TCP keepalive parameters: idle 15s, interval 5s, 5 probes
            if hasattr(socket, "TCP_KEEPIDLE"):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, 15)
            if hasattr(socket, "TCP_KEEPINTVL"):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, 5)
            if hasattr(socket, "TCP_KEEPCNT"):
                s.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, 5)
        except Exception:
            pass

    last_active = time.monotonic()
    try:
        while sockets:
            if watch_epoch and _EPOCH["v"] != start_epoch:
                break  # new exit IP: drop this tunnel so OpenCode reconnects through the new one
            readable, _, exceptional = select.select(sockets, [], sockets, 1.0 if watch_epoch else 5.0)
            if exceptional:
                break
            if not readable:
                # Generous 15-minute idle ceiling for long-thinking models (DeepSeek V4.1 reasoning)
                if time.monotonic() - last_active > 900.0:
                    break
                continue

            last_active = time.monotonic()
            for src in readable:
                dst = b if src is a else a
                try:
                    data = src.recv(65536)
                except (OSError, ConnectionResetError):
                    data = b""

                if data:
                    try:
                        dst.sendall(data)
                    except (OSError, ConnectionResetError):
                        return
                else:
                    # EOF from src -> send TCP FIN to dst (half-close)
                    closed.add(src)
                    if src in sockets:
                        sockets.remove(src)
                    try:
                        dst.shutdown(socket.SHUT_WR)
                    except OSError:
                        pass
                    if len(closed) >= 2:
                        return
    except Exception:
        pass


def _open_pinned_v6(dest_ip: str, port: int, local_v6: str, timeout: float = 12.0) -> socket.socket:
    """Open an IPv6 TCP socket explicitly bound to the WARP tunnel IPv6 address."""
    sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.bind((local_v6, 0))
        sock.connect((dest_ip, port))
        sock.settimeout(None)
        return sock
    except Exception:
        sock.close()
        raise


def _open_warp_direct(
    clean_host: str,
    port: int,
    warp_state: _WarpState,
) -> socket.socket:
    from ouc_common import current_warp_scope
    scope = current_warp_scope()

    is_strict = os.environ.get("OUC_STRICT_PROXY") == "1" or (LANE_DIR / "strict_proxy.flag").is_file()
    is_opencode_host = (
        clean_host in PINNED_HOSTS
        or clean_host.endswith(".opencode.ai")
        or clean_host.endswith(".opencode.com")
    )

    # 1. Isolated (App-Only) mode: tunnel via local WARP SOCKS5 proxy (port 40000).
    # The timeout covers WARP's own connect to the server, which over MASQUE can take a few seconds,
    # so 2 s used to fail on slow links and silently fall back to a direct connection below.
    if scope == "isolated":
        from tor_manager import open_socks5_tunnel
        if is_opencode_host:
            from ouc_common import warp_path_leaking
            if warp_path_leaking():
                # WARP says "Connected" but its proxy is sending traffic out with the real IP.
                raise NoRealIpError("WARP proxy is leaking the real IP (warp=off); refusing OpenCode traffic")
        last_exc: Exception | None = None
        for attempt in range(3 if is_opencode_host else 1):
            try:
                return open_socks5_tunnel(clean_host, port, socks_host="127.0.0.1", socks_port=40000,
                                          timeout=10.0 if is_opencode_host else 4.0)
            except Exception as exc:
                last_exc = exc
                if attempt < 2:
                    time.sleep(1.0)
        if is_opencode_host:
            # Never send OpenCode out with the user's own IP: that IP is usually the one that already hit
            # the free limit, so a new WARP IP would change nothing and the limit error would repeat.
            raise NoRealIpError(
                f"WARP proxy (port 40000) isn't answering ({type(last_exc).__name__}); "
                "not sending OpenCode out with your own IP"
            )

    # 2. TUN mode or fallback: bind explicitly to the WARP adapter's IPv6
    if is_opencode_host:
        local_v6, pinned_edge = warp_state.get(wait_if_rotating=True)
        if local_v6:
            try:
                return _open_pinned_v6(pinned_edge, port, local_v6)
            except Exception:
                pass

        # Try local WARP SOCKS5 if TUN socket bind failed
        try:
            from tor_manager import open_socks5_tunnel
            return open_socks5_tunnel(clean_host, port, socks_host="127.0.0.1", socks_port=40000, timeout=2.0)
        except Exception:
            pass

    local_v6, _ = warp_state.get(wait_if_rotating=False)
    if local_v6:
        try:
            infos = socket.getaddrinfo(clean_host, port, socket.AF_INET6, socket.SOCK_STREAM)
            if infos:
                return _open_pinned_v6(infos[0][4][0], port, local_v6)
        except Exception:
            pass

    # 3. Resilient failover: If WARP is down, try active Tor before failing
    try:
        from tor_manager import get_active_tor_target, open_socks5_tunnel
        tor_target = get_active_tor_target()
        if tor_target:
            return open_socks5_tunnel(clean_host, port, socks_host=tor_target[0], socks_port=tor_target[1], timeout=6.0)
    except Exception:
        pass

    # 4. OpenCode is never sent out with the user's own IP, strict mode or not. In Full TUN a plain socket
    # still goes through WARP while its adapter is up (local_v6 set); with the adapter down it would be the
    # real IP, which is the one that hit the limit (report 20261118-1605: WARP down, real IP out).
    if is_opencode_host and not warp_state.get(wait_if_rotating=False)[0]:
        raise NoRealIpError("WARP's adapter is down and Tor isn't running; not sending OpenCode out with your own IP")

    # 5. Strict fail-closed privacy mode (other hosts): only fail if explicitly enabled by user
    if is_strict:
        raise ConnectionRefusedError("Cloudflare WARP tunnel unavailable (fail-closed strict mode)")

    # 6. Direct path. For OpenCode this only runs while WARP's adapter is up (Full TUN: still through WARP).
    if is_opencode_host:
        try:
            _, pinned_edge = warp_state.get(wait_if_rotating=False)
            if pinned_edge and ":" in pinned_edge:
                sock = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                sock.settimeout(3.0)
                sock.connect((pinned_edge, port))
                sock.settimeout(None)
                return sock
        except Exception:
            pass

    # Direct standard dual-stack connection (IPv4/IPv6)
    sock = socket.create_connection((clean_host, port), timeout=8.0)
    sock.settimeout(None)
    return sock


def _open_upstream(
    host: str,
    port: int,
    warp_state: _WarpState,
) -> socket.socket:
    """Connect upstream: supports WARP IPv6, Tor, and Custom SOCKS5/HTTP with resilient fallback."""
    clean_host = host.strip("[]").lower()
    if clean_host in ("127.0.0.1", "localhost", "::1"):
        sock = socket.create_connection((clean_host, port), timeout=10.0)
        sock.settimeout(None)
        return sock

    try:
        from proxy_chain import get_egress_config
        vpn_cfg = get_egress_config()
    except Exception:
        vpn_cfg = {}
    if vpn_cfg.get("mode") == "vpn":
        # A VPN app the user has (Norton, Proton VPN, ...). Outside the try below on purpose: its
        # "except Exception: pass" would turn the refusal into a WARP connection.
        try:
            from proxy_chain import open_multi_egress_upstream
            return open_multi_egress_upstream(host, port, None)
        except Exception as exc:
            if (clean_host in PINNED_HOSTS or clean_host.endswith(".opencode.ai")
                    or clean_host.endswith(".opencode.com")):
                # The VPN is off or changing server: don't send OpenCode out with the user's own IP
                # (that IP is the one that hit the limit). It works again as soon as the VPN is back.
                raise NoRealIpError(f"VPN '{vpn_cfg.get('vpn_id')}' isn't connected ({exc}); "
                                    "not sending OpenCode out with your own IP")
            sock = socket.create_connection((clean_host, port), timeout=8.0)
            sock.settimeout(None)
            return sock

    try:
        from proxy_chain import get_egress_config, open_multi_egress_upstream
        cfg = get_egress_config()
        if cfg.get("mode") in ("tor", "custom"):
            try:
                return open_multi_egress_upstream(
                    host,
                    port,
                    lambda h, p: _open_warp_direct(h, p, warp_state),
                )
            except NoRealIpError:
                raise
            except Exception as exc:
                is_strict = os.environ.get("OUC_STRICT_PROXY") == "1" or (LANE_DIR / "strict_proxy.flag").is_file()
                if is_strict:
                    raise ConnectionRefusedError(f"Multi-egress ({cfg.get('mode')}) upstream error: {exc}")
                # Fall back to resilient direct path if upstream proxy dropped
                return _open_warp_direct(clean_host, port, warp_state)
    except Exception:
        pass

    return _open_warp_direct(clean_host, port, warp_state)


def _make_handler(warp_state: _WarpState):
    class ProxyHandler(socketserver.StreamRequestHandler):
        def handle(self) -> None:
            try:
                raw_line = self.rfile.readline(65536)
            except OSError:
                return
            if not raw_line:
                return
            parts = raw_line.decode("latin-1").strip().split()
            if len(parts) < 3:
                return
            method, target, proto = parts[0].upper(), parts[1], parts[2]

            headers: list[bytes] = []
            content_len = 0
            while True:
                line = self.rfile.readline(65536)
                if not line or line in (b"\r\n", b"\n"):
                    break
                low = line.decode("latin-1").lower()
                if low.startswith("proxy-connection:"):
                    continue
                if low.startswith("content-length:"):
                    try:
                        content_len = int(low.split(":", 1)[1].strip())
                    except ValueError:
                        pass
                headers.append(line)

            if method == "CONNECT":
                host, _, port_s = target.rpartition(":")
                if not host:
                    host, port = target, 443
                else:
                    try:
                        port = int(port_s)
                    except ValueError:
                        self._send_err(400, "Bad Request")
                        return
                try:
                    upstream = _open_upstream(host, port, warp_state)
                except Exception as exc:
                    if isinstance(exc, NoRealIpError):
                        _friendly_reason(host, exc, strict=True)  # logs it
                        self._send_err(503, NO_REAL_IP_REASON)
                        return
                    if _is_opencode(host):
                        # Never retry OpenCode on a plain direct socket: that's the user's own IP.
                        _friendly_reason(host, exc, strict=True)  # logs it
                        self._send_err(503, NO_REAL_IP_REASON)
                        return
                    if _is_strict():
                        self._send_err(503, _friendly_reason(host, exc, strict=True))
                        return
                    # Seamless direct fallback (not OpenCode) so other requests don't get 502/503 errors
                    try:
                        upstream = socket.create_connection((host, port), timeout=8.0)
                        upstream.settimeout(None)
                    except Exception as exc2:
                        self._send_err(503, _friendly_reason(host, exc2, strict=False))
                        return

                try:
                    self.wfile.write(f"{proto} 200 Connection Established\r\n\r\n".encode("ascii"))
                    self.wfile.flush()
                    _relay(self.connection, upstream, watch_epoch=_is_opencode(host))
                finally:
                    upstream.close()
                return

            parsed = urllib.parse.urlsplit(target)
            host = parsed.hostname or ""
            if not host:
                self._send_err(400, "Bad Request")
                return
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
            path = parsed.path or "/"
            if parsed.query:
                path = f"{path}?{parsed.query}"

            try:
                upstream = _open_upstream(host, port, warp_state)
            except Exception as exc:
                if isinstance(exc, NoRealIpError):
                    _friendly_reason(host, exc, strict=True)  # logs it
                    self._send_err(503, NO_REAL_IP_REASON)
                    return
                if _is_opencode(host):
                    # Never retry OpenCode on a plain direct socket: that's the user's own IP.
                    _friendly_reason(host, exc, strict=True)  # logs it
                    self._send_err(503, NO_REAL_IP_REASON)
                    return
                if _is_strict():
                    self._send_err(503, _friendly_reason(host, exc, strict=True))
                    return
                # Seamless direct fallback (not OpenCode) so other requests don't get 502/503 errors
                try:
                    upstream = socket.create_connection((host, port), timeout=8.0)
                    upstream.settimeout(None)
                except Exception as exc2:
                    self._send_err(503, _friendly_reason(host, exc2, strict=False))
                    return

            try:
                req_head = f"{method} {path} {proto}\r\n".encode("latin-1")
                upstream.sendall(req_head)
                for h in headers:
                    upstream.sendall(h)
                upstream.sendall(b"\r\n")
                if content_len > 0:
                    body = self.rfile.read(content_len)
                    if body:
                        upstream.sendall(body)
                _relay(self.connection, upstream, watch_epoch=_is_opencode(host))
            finally:
                upstream.close()

        def _send_err(self, code: int, reason: str) -> None:
            try:
                reason = " ".join(str(reason).split())[:220]
                msg = f"HTTP/1.1 {code} {reason}\r\nConnection: close\r\nContent-Length: 0\r\n\r\n"
                self.wfile.write(msg.encode("ascii", errors="replace"))
                self.wfile.flush()
            except OSError:
                pass

    return ProxyHandler


def start_proxy_server(port: int = 0) -> tuple[_ThreadedTCPServer, int, str]:
    """Start the local CONNECT proxy in a background thread and return (server, port, edge)."""
    pool = fetch_opencode_aaaa_pool()
    chosen_edge = os.environ.get("OUC_PIN_EDGE", "").strip() or random.choice(pool)
    try:
        ensure_dirs()
        (LANE_DIR / "last_edge.txt").write_text(chosen_edge + "\n", encoding="utf-8")
    except Exception:
        pass

    warp_state = _WarpState(pool=pool, initial_edge=chosen_edge, ttl=1.5)
    handler = _make_handler(warp_state)
    srv = _ThreadedTCPServer(("127.0.0.1", port), handler)
    actual_port = srv.server_address[1]
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()

    _EPOCH["v"] = _file_epoch()  # an old bump must not close the first tunnels

    def _watch_controls() -> None:
        while True:
            time.sleep(1.0)
            try:
                warp_state.poll_controls()
            except Exception:
                pass
            epoch = _file_epoch()
            if epoch and epoch != _EPOCH["v"]:
                _EPOCH["v"] = epoch

    threading.Thread(target=_watch_controls, daemon=True).start()
    return srv, actual_port, chosen_edge


if __name__ == "__main__":
    bind_port = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    server, listening_port, edge = start_proxy_server(bind_port)
    print(f"zen-proxy listening on 127.0.0.1:{listening_port} (edge={edge})", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
        server.server_close()

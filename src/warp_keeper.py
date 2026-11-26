"""Windows WARP keeper: enforces TUN mode and auto-repairs WARP connections."""

from __future__ import annotations

import os
import socket
import sys
import threading
import time

from ouc_common import (
    apply_warp_mode,
    current_warp_scope,
    ensure_warp_protocol,
    KEEPER_LOG_FILE,
    MUTEX_KEEPER,
    SERVICE_PROXY_PORT,
    acquire_mutex,
    append_log,
    ensure_dirs,
    find_warp_cli,
    get_warp_local_v6,
    is_admin,
    is_keeper_paused,
    record_warp_path,
    run_warp,
    trace_warp_path,
    warp_status,
)
from ouc_firewall import install_killswitch, is_killswitch_installed
from zen_proxy import start_proxy_server

TICK_SECONDS = 45
PROXY_WATCH_SECONDS = 3.0
# How often we check that WARP really carries the traffic (one small HTTPS request through WARP).
PATH_CHECK_SECONDS = 20
PATH_REPAIR_GAP = 120   # don't reconnect WARP more than once per 2 minutes for this
HEARTBEAT_FILE = KEEPER_LOG_FILE.parent / "heartbeat.txt"


def _heartbeat() -> None:
    """Cheap liveness signal the app checks: a running but hung keeper stops writing this."""
    try:
        HEARTBEAT_FILE.write_text(str(int(time.time())), encoding="utf-8")
    except Exception:
        pass


def _port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.8):
            return True
    except OSError:
        return False


def _proxy_watchdog() -> None:
    """Keep 127.0.0.1:SERVICE_PROXY_PORT served; restart the proxy whenever it disappears."""
    servers = []  # keep references so the servers are not garbage collected
    bind_failing = False
    while True:
        try:
            if not _port_listening(SERVICE_PROXY_PORT):
                try:
                    srv, bound_port, edge = start_proxy_server(SERVICE_PROXY_PORT)
                    servers.append(srv)
                    append_log(KEEPER_LOG_FILE, f"desktop/service proxy listening on 127.0.0.1:{bound_port} (edge={edge})")
                    bind_failing = False
                except Exception as exc:
                    if not bind_failing:
                        append_log(KEEPER_LOG_FILE, f"service proxy bind failed on port {SERVICE_PROXY_PORT}: {exc} (retrying)")
                        bind_failing = True
        except Exception:
            pass
        time.sleep(PROXY_WATCH_SECONDS)


def _desktop_brand_loop() -> None:
    """Keep OpenCode Desktop's start screen in line with the openhop.cfd setting (only while it's closed)."""
    time.sleep(20)
    last = ""
    while True:
        try:
            import desktop_brand
            res = desktop_brand.sync()
            if res not in ("ok", "skip") and res != last:
                append_log(KEEPER_LOG_FILE, f"desktop wordmark: {res}")
            last = res
        except Exception as exc:
            append_log(KEEPER_LOG_FILE, f"desktop wordmark error: {exc}")
        time.sleep(120)


def _extras_helper() -> None:
    """Local helper for openhop's extras inside OpenCode Desktop (voice typing, site icons). 127.0.0.1 only."""
    try:
        import opencode_extras
        opencode_extras.serve_forever()
    except Exception as exc:
        append_log(KEEPER_LOG_FILE, f"extras helper error: {exc}")


def _egress_mode() -> str:
    try:
        from proxy_chain import get_egress_config
        return get_egress_config().get("mode", "warp")
    except Exception:
        return "warp"


def _repair_leaking_warp(warp_bin: str) -> bool:
    """WARP says Connected but sends traffic out with the real IP: reconnect, then reset the tunnel."""
    steps = (
        ("reconnecting WARP", [["disconnect"], ["connect"]]),
        ("resetting the WARP tunnel", [["disconnect"], ["tunnel", "endpoint", "reset"],
                                       ["tunnel", "rotate-keys"], ["connect"]]),
    )
    for label, cmds in steps:
        append_log(KEEPER_LOG_FILE, f"WARP leak repair: {label}")
        for cmd in cmds:
            run_warp(cmd, warp_bin=warp_bin)
            if cmd == ["disconnect"]:
                time.sleep(1.0)
        if current_warp_scope() == "isolated":
            apply_warp_mode("isolated", warp_bin=warp_bin)
        for _ in range(10):
            time.sleep(1.5)
            connected, _, _ = warp_status(warp_bin)
            if not connected:
                continue
            info = trace_warp_path(timeout=6.0)
            if info:
                record_warp_path(info)
            if info.get("warp") in ("on", "plus"):
                append_log(KEEPER_LOG_FILE, "WARP leak repair: fixed, traffic goes through WARP again")
                return True
            if info.get("warp") == "off":
                break   # connected but still leaking: next step
    append_log(
        KEEPER_LOG_FILE,
        "WARP leak repair: still not fixed. OpenCode is blocked instead of using your own IP. "
        "Try 'Repair WARP' or restart Cloudflare WARP.",
    )
    return False


def _warp_path_watch(warp_bin: str) -> None:
    """Catch WARP's silent leak: 'Connected', but its proxy sends traffic outside the tunnel."""
    time.sleep(10)
    leaks = 0
    last_repair = 0.0
    reported = False
    while True:
        try:
            if _egress_mode() in ("warp", "clean") and not is_keeper_paused():
                connected, _, _ = warp_status(warp_bin)
                info = trace_warp_path(timeout=6.0) if connected else {}
                if info:
                    record_warp_path(info)
                if info.get("warp") == "off":
                    leaks += 1
                    if not reported:
                        reported = True
                        append_log(
                            KEEPER_LOG_FILE,
                            "WARP says Connected, but traffic is leaving with your own IP (warp=off). "
                            "Blocking OpenCode traffic and repairing WARP.",
                        )
                    if leaks >= 2 and time.time() - last_repair >= PATH_REPAIR_GAP and not is_keeper_paused():
                        last_repair = time.time()
                        if _repair_leaking_warp(warp_bin):
                            leaks = 0
                            reported = False
                elif info.get("warp") in ("on", "plus"):
                    if reported:
                        append_log(KEEPER_LOG_FILE, "traffic goes through WARP again")
                    leaks = 0
                    reported = False
        except Exception as exc:
            append_log(KEEPER_LOG_FILE, f"WARP path check error: {exc}")
        time.sleep(PATH_CHECK_SECONDS)


def ensure_connected(warp_bin: str | None = None) -> bool:
    """Verify WARP connection based on active scope (isolated proxy or full TUN)."""
    if not warp_bin:
        warp_bin = find_warp_cli()
    if not warp_bin:
        return False

    scope = current_warp_scope()

    connected, mode, raw = warp_status(warp_bin)

    # 1. If already connected, DO NOT TOUCH IT!
    # Re-setting protocol, ports or endpoints tears down the adapter and drops active sockets (causing ECONNRESET).
    if connected:
        return True

    raw_lower = (raw or "").lower()

    # 2. If WARP is currently establishing handshake (Connecting / Reconnecting),
    # give it a generous grace window (up to 8s) rather than declaring it dead.
    if "connecting" in raw_lower:
        for _ in range(8):
            time.sleep(1.0)
            connected, mode, raw = warp_status(warp_bin)
            if connected:
                return True
            if "connecting" not in (raw or "").lower():
                break

    # 3. Disconnected state: apply configuration once and connect
    if is_keeper_paused():
        return False   # the guard (or a manual hop) is changing the IP right now: don't touch WARP
    if scope == "isolated":
        if not mode or "proxy" not in mode.lower():
            apply_warp_mode("isolated", warp_bin=warp_bin)
        else:
            # proxy mode refuses to connect on WireGuard ("Proxy mode only supports MASQUE")
            ensure_warp_protocol("masque", warp_bin=warp_bin)

        run_warp(["connect"], warp_bin=warp_bin)
        for _ in range(8):
            time.sleep(1.0)
            connected, _, raw = warp_status(warp_bin)
            if connected:
                return True
            if "connecting" not in (raw or "").lower() and not connected:
                pass

        # If still not connected after grace window, rotate keys and reset endpoints (NO nuclear registration deletion!)
        if is_keeper_paused():
            return False
        append_log(KEEPER_LOG_FILE, "isolated tunnel disconnected — rotating keys & reconnecting")
        run_warp(["tunnel", "endpoint", "reset"], warp_bin=warp_bin)
        run_warp(["tunnel", "rotate-keys"], warp_bin=warp_bin)
        apply_warp_mode("isolated", warp_bin=warp_bin)
        run_warp(["connect"], warp_bin=warp_bin)

        for _ in range(8):
            time.sleep(1.0)
            connected, _, _ = warp_status(warp_bin)
            if connected:
                return True

        # Only register new if registration is explicitly missing or corrupt
        raw_check = (raw or "").lower()
        if ("registration missing" in raw_check or "not registered" in raw_check) and not is_keeper_paused():
            append_log(KEEPER_LOG_FILE, "registration missing — registering fresh WARP client")
            run_warp(["registration", "new"], warp_bin=warp_bin)
            apply_warp_mode("isolated", warp_bin=warp_bin)
            run_warp(["connect"], warp_bin=warp_bin)
            time.sleep(3)
            connected, _, _ = warp_status(warp_bin)

        return connected

    # Full System TUN mode (only when scope is explicitly set to full TUN)
    if not mode or "warp" not in mode.lower():
        run_warp(["mode", "warp"], warp_bin=warp_bin)

    run_warp(["connect"], warp_bin=warp_bin)
    for _ in range(8):
        time.sleep(1.0)
        connected, _, raw = warp_status(warp_bin)
        if connected:
            return True
        if "connecting" not in (raw or "").lower() and not connected:
            pass

    if is_keeper_paused():
        return False
    append_log(KEEPER_LOG_FILE, "TUN tunnel disconnected — rotating keys & reconnecting")
    run_warp(["tunnel", "endpoint", "reset"], warp_bin=warp_bin)
    run_warp(["tunnel", "rotate-keys"], warp_bin=warp_bin)
    run_warp(["mode", "warp"], warp_bin=warp_bin)
    run_warp(["connect"], warp_bin=warp_bin)

    for _ in range(8):
        time.sleep(1.0)
        connected, _, _ = warp_status(warp_bin)
        if connected:
            return True

    raw_check = (raw or "").lower()
    if ("registration missing" in raw_check or "not registered" in raw_check) and not is_keeper_paused():
        append_log(KEEPER_LOG_FILE, "registration missing — registering fresh TUN client")
        run_warp(["registration", "new"], warp_bin=warp_bin)
        run_warp(["mode", "warp"], warp_bin=warp_bin)
        run_warp(["connect"], warp_bin=warp_bin)
        time.sleep(4)
        connected, _, _ = warp_status(warp_bin)

    return connected


def _wireguard_loop() -> None:
    """WireGuard files (Your VPN): keep wireproxy up while they're in use, stop it otherwise (5 s tick)."""
    last_msg = ""
    while True:
        try:
            import wg_manager
            from proxy_chain import get_egress_config
            cfg = get_egress_config()
            if cfg.get("mode") == "vpn" and str(cfg.get("vpn_id") or "") == wg_manager.VPN_ID:
                ok, msg = wg_manager.ensure_running()
                if not ok and msg != "busy" and msg != last_msg:
                    append_log(KEEPER_LOG_FILE, f"WireGuard: {msg}")
                last_msg = "" if ok else msg
            elif wg_manager.running_conf():
                wg_manager.stop()
                last_msg = ""
                append_log(KEEPER_LOG_FILE, "WireGuard files not in use any more: wireproxy stopped")
        except Exception as exc:
            append_log(KEEPER_LOG_FILE, f"WireGuard error: {exc}")
        time.sleep(5)


def main() -> int:
    ensure_dirs()
    mutex = acquire_mutex(MUTEX_KEEPER)
    if mutex is None:
        return 0

    warp_bin = find_warp_cli()
    append_log(KEEPER_LOG_FILE, f"keeper up (tick={TICK_SECONDS}s, warp={'found' if warp_bin else 'none'})")
    _heartbeat()

    # 1. ALWAYS keep the desktop/service proxy on port 41987 alive so OpenCode can route in all modes.
    # A watchdog (re)binds it within seconds whenever nothing is listening (e.g. the port was held by
    # another process that exited), which is what previously caused long ECONNREFUSED outages.
    threading.Thread(target=_proxy_watchdog, daemon=True).start()
    threading.Thread(target=_desktop_brand_loop, daemon=True).start()
    threading.Thread(target=_extras_helper, daemon=True).start()
    threading.Thread(target=_wireguard_loop, daemon=True).start()
    if warp_bin:
        threading.Thread(target=_warp_path_watch, args=(warp_bin,), daemon=True).start()
    for _ in range(40):
        if _port_listening(SERVICE_PROXY_PORT):
            break
        time.sleep(0.25)

    # On startup, allow network interface initialization if booting and warp is installed
    if warp_bin:
        for _ in range(6):
            rc, _ = run_warp(["status"], warp_bin=warp_bin, timeout=8.0)
            if rc == 0:
                break
            time.sleep(8)

    while True:
        try:
            _heartbeat()
            mode = "warp"
            try:
                from proxy_chain import get_egress_config
                mode = get_egress_config().get("mode", "warp")
            except Exception:
                pass


            if mode in ("warp", "clean"):
                if warp_bin and not is_keeper_paused():
                    ensure_connected(warp_bin)
                    if is_admin() and (
                        os.environ.get("OUC_FIREWALL_KILLSWITCH") == "1"
                        or is_killswitch_installed()
                    ):
                        if not is_killswitch_installed():
                            install_killswitch()
            elif mode == "tor":
                # Ensure Tor daemon is healthy if managed
                try:
                    from tor_manager import get_active_tor_target, is_portable_tor_running, start_portable_tor
                    if not get_active_tor_target() and is_portable_tor_running():
                        start_portable_tor()
                except Exception:
                    pass
        except Exception as exc:
            append_log(KEEPER_LOG_FILE, f"keeper error: {exc}")
        time.sleep(TICK_SECONDS)


if __name__ == "__main__":
    raise SystemExit(main())

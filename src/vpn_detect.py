"""VPN apps the user already has (Norton, Proton VPN, NordVPN, ...): find them, show their real icon, and
send OpenCode through them.

How it works: a VPN app adds its own network adapter while it's connected. openhop binds OpenCode's
connections to that adapter's address, so they leave through the VPN even if the VPN only covers some apps.

Changing server: most VPN apps have no command for it, so when the free limit is hit openhop asks the user
to pick another server in the app, waits until the exit IP changes and then sends 'continue' by itself.

Nothing here changes the PC: it only reads the registry, the network adapters and the program files.
"""

from __future__ import annotations

import base64
import json
import re
import shutil
import socket
import sys
import time
from pathlib import Path

from ouc_common import DEST_DIR, IS_WINDOWS, LANE_DIR, ensure_dirs, run_hidden

ICON_DIR = DEST_DIR / "vpn_icons"
MANUAL_FILE = LANE_DIR / "vpn_manual.json"

# Known VPN apps. Regexes are case-insensitive.
#   match:   the app's name in "Installed apps" (registry uninstall entries)
#   adapter: its network adapter (name or description), which only has an address while connected
#   service: its Windows service
#   suite:   the name above is an antivirus suite that may not include the VPN; only count it when the
#            adapter or the service is there too
#   plan:    "free" (has a free plan), "trial" or "paid"
CATALOG: list[dict] = [
    {"id": "proton", "name": "Proton VPN", "plan": "free",
     "match": r"^proton ?vpn", "adapter": r"proton ?vpn", "service": r"^protonvpn"},
    {"id": "norton", "name": "Norton VPN", "plan": "paid", "suite": True,
     "match": r"^norton (360|vpn|secure vpn|ultra vpn)", "adapter": r"norton vpn", "service": r"^nortonvpn$",
     "exe": [r"Norton\Suite\NortonUI.exe"]},
    {"id": "windscribe", "name": "Windscribe", "plan": "free",
     "match": r"^windscribe", "adapter": r"windscribe", "service": r"^windscribe",
     "cli": {"exe": [r"Windscribe\windscribe-cli.exe"], "kind": "windscribe"}},
    {"id": "tunnelbear", "name": "TunnelBear", "plan": "free",
     "match": r"^tunnelbear", "adapter": r"tunnelbear", "service": r"tunnelbear"},
    {"id": "hideme", "name": "hide.me VPN", "plan": "free",
     "match": r"^hide\.me", "adapter": r"hide\.me|hideme", "service": r"hide\.?me"},
    {"id": "planet", "name": "Planet VPN", "plan": "free",
     "match": r"^(free ?vpn )?planet ?vpn", "adapter": r"planet ?vpn", "service": r"planet ?vpn"},
    {"id": "hotspotshield", "name": "Hotspot Shield", "plan": "free",
     "match": r"^hotspot shield", "adapter": r"hotspot ?shield|hydra", "service": r"hotspot ?shield|hss"},
    {"id": "nordvpn", "name": "NordVPN", "plan": "paid",
     "match": r"^nordvpn", "adapter": r"nordlynx|nordvpn", "service": r"^nordvpn",
     "cli": {"exe": [r"NordVPN\nordvpn.exe"], "kind": "nordvpn"}},
    {"id": "surfshark", "name": "Surfshark", "plan": "paid",
     "match": r"^surfshark", "adapter": r"surfshark", "service": r"^surfshark"},
    {"id": "expressvpn", "name": "ExpressVPN", "plan": "paid",
     "match": r"^expressvpn", "adapter": r"expressvpn", "service": r"expressvpn"},
    {"id": "cyberghost", "name": "CyberGhost", "plan": "paid",
     "match": r"^cyberghost", "adapter": r"cyberghost", "service": r"cyberghost"},
    {"id": "pia", "name": "Private Internet Access", "plan": "paid",
     "match": r"^private internet access", "adapter": r"private internet access|wgpia|\bpia\b", "service": r"^pia",
     "cli": {"exe": [r"Private Internet Access\piactl.exe"], "kind": "pia"}},
    {"id": "mullvad", "name": "Mullvad VPN", "plan": "paid",
     "match": r"^mullvad", "adapter": r"mullvad", "service": r"^mullvad",
     "cli": {"exe": [r"Mullvad VPN\resources\mullvad.exe"], "kind": "mullvad"}},
    {"id": "avast", "name": "Avast SecureLine VPN", "plan": "paid", "suite": True,
     "match": r"^avast (secureline|vpn)", "adapter": r"avast (secureline|vpn)", "service": r"^(avast ?vpn|secureline)"},
    {"id": "avg", "name": "AVG Secure VPN", "plan": "paid", "suite": True,
     "match": r"^avg (secure )?vpn", "adapter": r"avg (secure )?vpn", "service": r"^avg ?vpn"},
    {"id": "kaspersky", "name": "Kaspersky VPN", "plan": "paid", "suite": True,
     "match": r"^kaspersky (vpn|secure connection)", "adapter": r"kaspersky (vpn|secure connection)",
     "service": r"^kaspersky ?vpn|^ksde"},
    {"id": "bitdefender", "name": "Bitdefender VPN", "plan": "paid", "suite": True,
     "match": r"^bitdefender vpn", "adapter": r"bitdefender vpn", "service": r"^bdvpn|bitdefender vpn"},
]
CATALOG_BY_ID = {v["id"]: v for v in CATALOG}

# WireGuard files (.conf) run by openhop itself through wireproxy (see wg_manager): Proton VPN Free and any
# VPN that gives WireGuard files. Not an installed app, so it's always offered.
WIREGUARD = {"id": "wireguard", "name": "WireGuard", "plan": "free", "kind": "wireguard", "adapter": ""}

# Adapters that are never "the VPN": WARP is its own mode, the rest are virtual switches and the like.
_NOT_VPN = re.compile(r"cloudflare|warp|hyper-v|vethernet|virtualbox|vmware|loopback|bluetooth|wsl|docker|npcap|"
                      r"teredo|isatap|6to4|kernel debug|wan miniport|microsoft wi-fi direct", re.I)
_TUNNEL_HINT = re.compile(r"wintun|wireguard|tap-windows|\btap\b|openvpn|\btun\b|vpn|tunnel", re.I)


# ---------------------------------------------------------------------------------------------------------------
# Network adapters (GetAdaptersAddresses: fast, no PowerShell, gives name + description + addresses)

def _adapters_windows() -> list[dict]:
    import ctypes
    from ctypes import wintypes

    class SOCKET_ADDRESS(ctypes.Structure):
        _fields_ = [("lpSockaddr", ctypes.c_void_p), ("iSockaddrLength", ctypes.c_int)]

    class UNICAST(ctypes.Structure):
        pass
    UNICAST._fields_ = [("Length", ctypes.c_ulong), ("Flags", wintypes.DWORD),
                        ("Next", ctypes.POINTER(UNICAST)), ("Address", SOCKET_ADDRESS)]

    class ADAPTER(ctypes.Structure):
        pass
    ADAPTER._fields_ = [("Length", ctypes.c_ulong), ("IfIndex", wintypes.DWORD),
                        ("Next", ctypes.POINTER(ADAPTER)), ("AdapterName", ctypes.c_char_p),
                        ("FirstUnicastAddress", ctypes.POINTER(UNICAST)),
                        ("FirstAnycastAddress", ctypes.c_void_p), ("FirstMulticastAddress", ctypes.c_void_p),
                        ("FirstDnsServerAddress", ctypes.c_void_p), ("DnsSuffix", ctypes.c_wchar_p),
                        ("Description", ctypes.c_wchar_p), ("FriendlyName", ctypes.c_wchar_p),
                        ("PhysicalAddress", ctypes.c_ubyte * 8), ("PhysicalAddressLength", ctypes.c_ulong),
                        ("Flags", ctypes.c_ulong), ("Mtu", ctypes.c_ulong), ("IfType", ctypes.c_ulong),
                        ("OperStatus", ctypes.c_int)]

    iphlp = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(32 * 1024)
    flags = 0x0002 | 0x0004 | 0x0008  # skip anycast, multicast, DNS servers
    for _ in range(3):
        buf = ctypes.create_string_buffer(size.value)
        rc = iphlp.GetAdaptersAddresses(0, flags, None, buf, ctypes.byref(size))
        if rc == 0:
            break
        if rc != 111:  # ERROR_BUFFER_OVERFLOW: try again with the size Windows asked for
            return []
    else:
        return []

    out: list[dict] = []
    ptr = ctypes.cast(buf, ctypes.POINTER(ADAPTER))
    while ptr:
        a = ptr.contents
        v4, v6 = [], []
        u = a.FirstUnicastAddress
        while u:
            sa = u.contents.Address.lpSockaddr
            if sa:
                fam = ctypes.c_ushort.from_address(sa).value
                if fam == socket.AF_INET:
                    raw = ctypes.string_at(sa + 4, 4)
                    v4.append(socket.inet_ntop(socket.AF_INET, raw))
                elif fam == socket.AF_INET6:
                    raw = ctypes.string_at(sa + 8, 16)
                    ip = socket.inet_ntop(socket.AF_INET6, raw)
                    if not ip.lower().startswith("fe80"):
                        v6.append(ip)
            u = u.contents.Next
        out.append({"name": a.FriendlyName or "", "desc": a.Description or "", "up": a.OperStatus == 1,
                    "v4": [ip for ip in v4 if not ip.startswith("169.254.")], "v6": v6})
        ptr = a.Next
    return out


def _adapters_psutil() -> list[dict]:
    try:
        import psutil
    except Exception:
        return []
    stats = psutil.net_if_stats()
    out = []
    for name, addrs in psutil.net_if_addrs().items():
        v4 = [a.address for a in addrs if a.family == socket.AF_INET and not a.address.startswith("169.254.")]
        v6 = [a.address.split("%", 1)[0] for a in addrs
              if a.family == socket.AF_INET6 and not a.address.lower().startswith("fe80")]
        out.append({"name": name, "desc": "", "up": bool(stats.get(name) and stats[name].isup), "v4": v4, "v6": v6})
    return out


def list_adapters() -> list[dict]:
    """Every network adapter: name, description, connected, IPv4/IPv6 addresses."""
    if IS_WINDOWS:
        try:
            return _adapters_windows()
        except Exception:
            pass
    return _adapters_psutil()


def tunnel_adapters() -> list[dict]:
    """Adapters that look like a VPN (for "Add by hand"), WARP left out."""
    return [a for a in list_adapters()
            if not _NOT_VPN.search(f"{a['name']} {a['desc']}") and _TUNNEL_HINT.search(f"{a['name']} {a['desc']}")]


def _adapter_for(vpn: dict, adapters: list[dict] | None = None) -> dict | None:
    """The VPN's adapter that is connected right now (has an address), else any of its adapters, else None."""
    adapters = list_adapters() if adapters is None else adapters
    exact = (vpn.get("adapter_name") or "").strip().lower()
    rx = re.compile(vpn["adapter"], re.I) if vpn.get("adapter") else None
    mine = [a for a in adapters
            if (exact and a["name"].lower() == exact) or (rx and rx.search(f"{a['name']} {a['desc']}"))]
    live = [a for a in mine if a["up"] and (a["v4"] or a["v6"])]
    return (live or mine or [None])[0]


_bind_cache: dict[str, tuple[float, tuple[str, int] | None]] = {}


def vpn_bind_address(vpn_id: str, max_age: float = 2.0) -> tuple[str, int] | None:
    """Local address to bind to so a connection leaves through this VPN: (ip, family), or None if it's off.

    Cached for a couple of seconds: the proxy asks once per connection and OpenCode opens many at once.
    """
    hit = _bind_cache.get(vpn_id)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    res: tuple[str, int] | None = None
    vpn = get_vpn(vpn_id)
    if vpn and vpn.get("kind") == "wireguard":
        import wg_manager
        res = ("127.0.0.1", socket.AF_INET) if wg_manager.socks_alive(0.6) else None
        _bind_cache[vpn_id] = (time.time(), res)
        return res
    ad = _adapter_for(vpn) if vpn else None
    if ad and ad["up"]:
        if ad["v4"]:
            res = ad["v4"][0], socket.AF_INET
        elif ad["v6"]:
            res = ad["v6"][0], socket.AF_INET6
    _bind_cache[vpn_id] = (time.time(), res)
    return res


def open_via_vpn(host: str, port: int, vpn_id: str, timeout: float = 10.0) -> socket.socket:
    """TCP connection to host:port that leaves through the VPN's adapter. Raises if the VPN isn't connected."""
    if vpn_id == WIREGUARD["id"]:
        import wg_manager
        if not vpn_bind_address(vpn_id):
            raise ConnectionError("the WireGuard tunnel isn't running")
        return wg_manager.open_tunnel(host, port, timeout=max(timeout, 12.0))
    bind = vpn_bind_address(vpn_id)
    if not bind:
        raise ConnectionError(f"VPN '{vpn_id}' isn't connected")
    local_ip, fam = bind
    infos = socket.getaddrinfo(host, port, fam, socket.SOCK_STREAM)
    if not infos:
        raise ConnectionError(f"{host} has no {'IPv4' if fam == socket.AF_INET else 'IPv6'} address")
    last: Exception | None = None
    for info in infos[:3]:
        s = socket.socket(fam, socket.SOCK_STREAM)
        s.settimeout(timeout)
        try:
            s.bind((local_ip, 0))
            s.connect(info[4])
            s.settimeout(None)
            return s
        except Exception as exc:
            last = exc
            s.close()
    raise ConnectionError(f"couldn't reach {host} through the VPN: {last}")


def vpn_exit_ip(vpn_id: str, timeout: float = 8.0) -> str:
    """The public IP this VPN gives right now ('' if it's off or doesn't answer)."""
    if vpn_id == WIREGUARD["id"]:
        import wg_manager
        return wg_manager.exit_ip(timeout)
    bind = vpn_bind_address(vpn_id, max_age=0)
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not bind or not curl:
        return ""
    for ep in ("https://cloudflare.com/cdn-cgi/trace", "https://1.1.1.1/cdn-cgi/trace"):
        try:
            res = run_hidden([curl, "--interface", bind[0], "-s", "-m", str(int(timeout)), ep], timeout=timeout + 2)
        except Exception:
            continue
        for line in (res.stdout or "").splitlines():
            if line.startswith("ip="):
                return line[3:].strip()
    return ""


# ---------------------------------------------------------------------------------------------------------------
# Installed apps

def _uninstall_entries() -> list[dict]:
    if not IS_WINDOWS:
        return []
    import winreg
    out = []
    roots = [(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall"),
             (winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Uninstall")]
    for hive, path in roots:
        try:
            key = winreg.OpenKey(hive, path)
        except OSError:
            continue
        with key:
            for i in range(winreg.QueryInfoKey(key)[0]):
                try:
                    with winreg.OpenKey(key, winreg.EnumKey(key, i)) as sub:
                        def val(n: str) -> str:
                            try:
                                return str(winreg.QueryValueEx(sub, n)[0] or "")
                            except OSError:
                                return ""
                        name = val("DisplayName")
                        if name:
                            out.append({"name": name, "version": val("DisplayVersion"),
                                        "location": val("InstallLocation"), "icon": val("DisplayIcon")})
                except OSError:
                    continue
    return out


def _service_names() -> list[str]:
    if not IS_WINDOWS:
        return []
    try:
        import psutil
        return [f"{s.name()} {s.display_name()}" for s in psutil.win_service_iter()]
    except Exception:
        return []


def _exe_from_icon_value(raw: str) -> str:
    """'"C:\\x\\app.exe",0' -> 'C:\\x\\app.exe' (only real .exe files)."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    if raw.startswith('"'):
        raw = raw[1:].split('"', 1)[0]
    else:
        raw = re.sub(r",\s*-?\d+$", "", raw)
    return raw if raw.lower().endswith(".exe") and Path(raw).is_file() else ""


def _find_exe(vpn: dict, entry: dict | None) -> str:
    import os
    for rel in vpn.get("exe", []):
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            p = Path(base) / rel
            if p.is_file():
                return str(p)
    if entry:
        exe = _exe_from_icon_value(entry.get("icon", ""))
        if exe and "unins" not in Path(exe).name.lower():
            return exe
        loc = entry.get("location", "")
        if loc and Path(loc).is_dir():
            cands = [p for p in Path(loc).glob("*.exe") if "unins" not in p.name.lower()]
            if cands:
                return str(max(cands, key=lambda p: p.stat().st_size))
    return ""


# ---------------------------------------------------------------------------------------------------------------
# Real app icons (taken from the app's own .exe, never drawn by us)

def _extract_icon_png(exe: str, out: Path, size: int = 64) -> bool:
    if not IS_WINDOWS or not exe or not Path(exe).is_file():
        return False
    import ctypes
    from ctypes import wintypes
    from PIL import Image

    class ICONINFO(ctypes.Structure):
        _fields_ = [("fIcon", wintypes.BOOL), ("xHotspot", wintypes.DWORD), ("yHotspot", wintypes.DWORD),
                    ("hbmMask", wintypes.HBITMAP), ("hbmColor", wintypes.HBITMAP)]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG), ("biHeight", wintypes.LONG),
                    ("biPlanes", wintypes.WORD), ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                    ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    shell32, user32, gdi32 = ctypes.windll.shell32, ctypes.windll.user32, ctypes.windll.gdi32
    shell32.SHDefExtractIconW.argtypes = [wintypes.LPCWSTR, ctypes.c_int, wintypes.UINT,
                                          ctypes.POINTER(wintypes.HICON), ctypes.POINTER(wintypes.HICON),
                                          wintypes.UINT]
    user32.GetIconInfo.argtypes = [wintypes.HICON, ctypes.POINTER(ICONINFO)]
    user32.DestroyIcon.argtypes = [wintypes.HICON]
    user32.GetDC.restype = wintypes.HDC
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT, ctypes.c_void_p,
                                ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]

    hicon = wintypes.HICON()
    if shell32.SHDefExtractIconW(exe, 0, 0, ctypes.byref(hicon), None, size) != 0 or not hicon:
        return False
    info = ICONINFO()
    try:
        if not user32.GetIconInfo(hicon, ctypes.byref(info)) or not info.hbmColor:
            return False
        bmi = BITMAPINFOHEADER(biSize=ctypes.sizeof(BITMAPINFOHEADER), biWidth=size, biHeight=-size,
                               biPlanes=1, biBitCount=32, biCompression=0)
        buf = ctypes.create_string_buffer(size * size * 4)
        hdc = user32.GetDC(None)
        lines = gdi32.GetDIBits(hdc, info.hbmColor, 0, size, buf, ctypes.byref(bmi), 0)
        user32.ReleaseDC(None, hdc)
        if not lines:
            return False
        img = Image.frombuffer("RGBA", (size, size), buf, "raw", "BGRA", 0, 1)
        if img.getchannel("A").getextrema()[1] == 0:
            # Old-style icon without alpha: everything reads as transparent, so keep it opaque.
            img = img.convert("RGB").convert("RGBA")
        out.parent.mkdir(parents=True, exist_ok=True)
        img.save(out)
        return True
    finally:
        if info.hbmColor:
            gdi32.DeleteObject(info.hbmColor)
        if info.hbmMask:
            gdi32.DeleteObject(info.hbmMask)
        user32.DestroyIcon(hicon)


def icon_data_uri(vpn_id: str, exe: str) -> str:
    """data:image/png;base64,... of the app's own icon ('' if there is none). Cached per app."""
    safe = re.sub(r"[^a-z0-9_-]", "_", vpn_id.lower())
    out = ICON_DIR / f"{safe}.png"
    try:
        fresh = out.is_file() and exe and Path(exe).is_file() and out.stat().st_mtime >= Path(exe).stat().st_mtime
        if not fresh and not _extract_icon_png(exe, out):
            if not out.is_file():
                return ""
        return "data:image/png;base64," + base64.b64encode(out.read_bytes()).decode("ascii")
    except Exception:
        return ""


# ---------------------------------------------------------------------------------------------------------------
# VPNs added by hand ("Add it yourself")

def load_manual() -> list[dict]:
    try:
        data = json.loads(MANUAL_FILE.read_text(encoding="utf-8"))
        return [d for d in data if isinstance(d, dict) and d.get("id")]
    except Exception:
        return []


def add_manual(name: str, exe: str = "", adapter_name: str = "") -> dict:
    name = (name or "").strip()[:60]
    if not name:
        raise ValueError("empty name")
    ensure_dirs()
    items = load_manual()
    vid = "manual-" + (re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or str(int(time.time())))
    items = [i for i in items if i["id"] != vid]
    item = {"id": vid, "name": name, "exe": exe.strip(), "adapter_name": adapter_name.strip(), "manual": True}
    items.append(item)
    MANUAL_FILE.write_text(json.dumps(items, indent=2), encoding="utf-8")
    return item


def remove_manual(vpn_id: str) -> bool:
    items = load_manual()
    left = [i for i in items if i["id"] != vpn_id]
    if len(left) == len(items):
        return False
    MANUAL_FILE.write_text(json.dumps(left, indent=2), encoding="utf-8")
    return True


def get_vpn(vpn_id: str) -> dict | None:
    if vpn_id == WIREGUARD["id"]:
        return WIREGUARD
    if vpn_id in CATALOG_BY_ID:
        return CATALOG_BY_ID[vpn_id]
    for m in load_manual():
        if m["id"] == vpn_id:
            return {"adapter": "", **m}
    return None


# ---------------------------------------------------------------------------------------------------------------
# Detection

def detect(with_icons: bool = True) -> list[dict]:
    """Every VPN app found on this PC (catalog + added by hand), with its state.

    Each item: id, name, plan, installed, connected, adapter, ip (the adapter's address), exe, icon, manual.
    """
    entries = _uninstall_entries()
    services = _service_names()
    adapters = list_adapters()
    found: list[dict] = []
    for vpn in CATALOG:
        rx = re.compile(vpn["match"], re.I)
        entry = next((e for e in entries if rx.search(e["name"])), None)
        ad = _adapter_for(vpn, adapters)
        srx = re.compile(vpn["service"], re.I) if vpn.get("service") else None
        has_service = bool(srx and any(srx.search(s) for s in services))
        if vpn.get("suite"):
            installed = bool(entry) and (bool(ad) or has_service)
        else:
            installed = bool(entry) or bool(ad) or has_service
        if not installed:
            continue
        exe = _find_exe(vpn, entry)
        found.append(_item(vpn, ad, exe, version=(entry or {}).get("version", ""), with_icons=with_icons))
    for m in load_manual():
        vpn = {"adapter": "", **m}
        ad = _adapter_for(vpn, adapters) if vpn.get("adapter_name") else None
        found.append(_item(vpn, ad, m.get("exe", ""), with_icons=with_icons))
    for it in found:
        it["auto"] = bool(cli_exe(it["id"]))
    found.append(wireguard_item())
    return found


def wireguard_item() -> dict:
    """The "WireGuard files" card: its files, which one is in use, and whether the tunnel is up."""
    import wg_manager
    confs = wg_manager.list_confs()
    up = wg_manager.socks_alive(0.6)
    return {"id": WIREGUARD["id"], "name": WIREGUARD["name"], "plan": "free", "version": "", "installed": True,
            "connected": up, "adapter": "", "ip": "", "exe": "", "icon": "", "manual": False,
            "kind": "wireguard", "auto": True, "confs": confs, "current": wg_manager.current_conf(),
            "ready": bool(wg_manager.find_exe())}


def _item(vpn: dict, ad: dict | None, exe: str, version: str = "", with_icons: bool = True) -> dict:
    connected = bool(ad and ad["up"] and (ad["v4"] or ad["v6"]))
    return {
        "id": vpn["id"], "name": vpn["name"], "plan": vpn.get("plan", ""), "version": version,
        "installed": True, "connected": connected,
        "adapter": (ad or {}).get("name", ""),
        "ip": ((ad or {}).get("v4") or (ad or {}).get("v6") or [""])[0] if connected else "",
        "exe": exe, "icon": icon_data_uri(vpn["id"], exe) if (with_icons and exe) else "",
        "manual": bool(vpn.get("manual")),
    }


# ---------------------------------------------------------------------------------------------------------------
# Changing server by itself: VPN apps that have a command line (and WireGuard files, see wg_manager)

# Places to try, nearest first for a user in Europe. A place the account can't use just fails and the next
# one is tried. Windscribe's are its free locations.
_CLI_PLACES = {
    "windscribe": ["Netherlands", "France", "Germany", "United Kingdom", "Switzerland", "Norway", "Romania",
                   "US East", "US Central", "US West", "Canada East", "Canada West", "Hong Kong", "Turkey"],
    "nordvpn": ["Spain", "France", "Portugal", "Italy", "Germany", "Netherlands", "Belgium", "Switzerland",
                "United Kingdom", "Ireland", "Sweden", "Poland", "United States"],
    "mullvad": ["es", "fr", "pt", "it", "de", "nl", "be", "ch", "gb", "ie", "se", "pl", "us"],
}


def cli_exe(vpn_id: str) -> str:
    """The VPN app's command-line program, if it has one and it's installed ('' otherwise)."""
    import os
    vpn = get_vpn(vpn_id) or {}
    for rel in (vpn.get("cli") or {}).get("exe", []):
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")):
            p = Path(base) / rel
            if p.is_file():
                return str(p)
    return ""


def can_change_server(vpn_id: str) -> bool:
    """True when openhop can change this VPN's server by itself."""
    if vpn_id == WIREGUARD["id"]:
        return True
    return bool(cli_exe(vpn_id))


def _cli_places(kind: str, exe: str) -> list[str]:
    if kind == "pia":
        try:
            res = run_hidden([exe, "get", "regions"], timeout=20)
            regions = [ln.strip() for ln in (res.stdout or "").splitlines() if ln.strip() and ln.strip() != "auto"]
            near = [r for r in regions if re.search(r"spain|france|portugal|italy|germany|netherlands|belgium|"
                                                    r"switzerland|uk|ireland|sweden|poland", r, re.I)]
            return near + [r for r in regions if r not in near]
        except Exception:
            return []
    return list(_CLI_PLACES.get(kind, []))


CLI_LAST_FILE = LANE_DIR / "vpn_cli_last.json"


def _cli_last() -> dict:
    try:
        d = json.loads(CLI_LAST_FILE.read_text(encoding="utf-8"))
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _cli_last_save(vpn_id: str, place: str) -> None:
    try:
        d = _cli_last()
        d[vpn_id] = place
        CLI_LAST_FILE.write_text(json.dumps(d), encoding="utf-8")
    except Exception:
        pass


def change_server(vpn_id: str, tried: list[str]) -> dict:
    """Ask the VPN app (through its command line) for a server in a place not tried yet in this round.

    Returns {"ok", "place", "message"}. The caller checks that the exit IP really changed.
    """
    vpn = get_vpn(vpn_id) or {}
    kind = (vpn.get("cli") or {}).get("kind", "")
    exe = cli_exe(vpn_id)
    if not exe or not kind:
        return {"ok": False, "place": "", "message": f"{vpn.get('name', vpn_id)} has no command line here"}
    places = _cli_places(kind, exe)
    last = _cli_last().get(vpn_id, "")
    if last in places:   # start after the place used last time, so each change goes somewhere new
        i = places.index(last) + 1
        places = places[i:] + places[:i]
    places = [p for p in places if p not in tried]
    if not places:
        return {"ok": False, "place": "", "message": "every place was tried"}
    place = places[0]
    _cli_last_save(vpn_id, place)
    steps = {
        "windscribe": [[exe, "connect", place]],
        "nordvpn": [[exe, "-c", "-g", place]],
        "pia": [[exe, "set", "region", place], [exe, "connect"]],
        "mullvad": [[exe, "relay", "set", "location", place], [exe, "connect"], [exe, "reconnect"]],
    }.get(kind, [])
    out = ""
    for cmd in steps:
        try:
            res = run_hidden(cmd, timeout=60)
        except Exception as exc:
            return {"ok": False, "place": place, "message": f"{Path(exe).name} didn't answer: {exc}"}
        out = ((res.stdout or "") + (res.stderr or "")).strip()
        if res.returncode != 0 and cmd[-1] != "connect":   # 'connect' fails harmlessly when already connected
            return {"ok": False, "place": place, "message": out[-200:] or f"exit code {res.returncode}"}
    return {"ok": True, "place": place, "message": out[-200:]}


def catalog_names() -> list[dict]:
    """Every VPN openhop knows (for "also works with ..."): id, name, plan. No logos: we don't ship theirs."""
    return [{"id": v["id"], "name": v["name"], "plan": v["plan"]} for v in CATALOG]


def launch_app(vpn_id: str) -> bool:
    """Open the VPN's own window so the user can pick another server."""
    vpn = get_vpn(vpn_id)
    if not vpn:
        return False
    exe = vpn.get("exe") if isinstance(vpn.get("exe"), str) else ""
    if not exe:
        it = next((d for d in detect(with_icons=False) if d["id"] == vpn_id), None)
        exe = (it or {}).get("exe", "")
    if not exe or not Path(exe).is_file():
        return False
    import subprocess
    try:
        subprocess.Popen([exe], close_fds=True)
        return True
    except Exception:
        return False


if __name__ == "__main__":
    # python vpn_detect.py  -> what's on this PC (no icons, for a quick look)
    for it in detect(with_icons="--icons" in sys.argv):
        icon = f" icon={len(it['icon'])}b" if it["icon"] else ""
        print(f"{it['id']:14} {it['name']:24} connected={it['connected']!s:5} adapter={it['adapter']!r} "
              f"ip={it['ip']} exe={it['exe']}{icon}")
    if "--exit" in sys.argv:
        for it in detect(with_icons=False):
            if it["connected"]:
                print(it["id"], "exit IP:", vpn_exit_ip(it["id"]))

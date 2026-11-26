"""openhop tray icon, app settings, start-with-Windows and notifications.

- Tray icon (pystray, own Win32 message loop in a daemon thread) with a coloured status dot
  (green = working, amber = warning, red = needs attention) and a menu:
  Open openhop / Get a new IP / Fix everything / Quit openhop.
- Notifications: our own branded popup (src/ui/notify.html, bottom-right, never takes focus);
  falls back to the native Windows notification when the popup can't be shown.
- Settings live in %LOCALAPPDATA%\\openhop\\app_settings.json.
"""

from __future__ import annotations

import ctypes
import json
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from ouc_common import DEST_DIR, L, LANGUAGES, append_log, forget_language, ui_lang

# Own user32 instance so argtypes set elsewhere (e.g. GetWindowRect in openhop_app) can't break our calls.
_U32 = ctypes.WinDLL("user32") if sys.platform == "win32" else None

SETTINGS_FILE = DEST_DIR / "app_settings.json"
TRAY_LOG = DEST_DIR / "logs" / "tray.log"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "openhop"

DEFAULT_SETTINGS: dict[str, Any] = {
    "close_to_tray": True,          # X hides the window to the tray
    "start_with_windows": True,     # tray starts quietly at login
    "notify_problems": True,
    "notify_hops": True,
    "notify_updates": True,
    "desktop_brand": True,          # openhop.cfd wordmark on OpenCode Desktop's start screen
    "opencode_extras": True,        # voice typing button + website/:icon: project icons inside OpenCode Desktop
    "hide_limit_banner": True,      # hide OpenCode Desktop's "Free limit reached" card (it replaces the chat box)
    "auto_update": True,            # download new versions from openhop.cfd and install them on restart
    "auto_tor": True,               # Zen Guard switches to Tor by itself when WARP's addresses are used up
                                    # (read directly by zen_guard.py; back to WARP after the daily reset)
    "new_chat_on_limit": True,      # a chat at the free limit continues in a new chat (read by zen_guard.py)
    "turbo": True,                  # openhop Turbo: share/skip exits already used up today (read by turbo.py)
    "tray_tip_shown": False,        # one-time "still running in the tray" tip
    "autostart_configured": False,  # first-run default applied
    "language": "auto",             # auto (Spanish if Windows is in Spanish) | en | es
}

POLL_SECONDS = 15.0
INCIDENT_POLL_SECONDS = 5.0         # faster while openhop is fixing the free limit
PROBLEM_REPEAT_SECONDS = 600.0      # same problem notified at most every 10 min
HOP_REPEAT_SECONDS = 45.0

_DOT = {"ok": (46, 204, 113), "warn": (242, 179, 24), "busy": (242, 179, 24), "bad": (231, 76, 60)}


def _tooltip(status: str) -> str:
    return {"ok": L("openhop: everything is working", "openhop: todo funciona"),
            "warn": L("openhop: working, with a warning", "openhop: funciona, con un aviso"),
            "bad": L("openhop: needs attention", "openhop: necesita atención")}.get(status, "openhop")


# ---------------------------------------------------------------- settings
_settings_lock = threading.Lock()


def load_settings() -> dict[str, Any]:
    data = dict(DEFAULT_SETTINGS)
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            data.update({k: v for k, v in raw.items() if k in DEFAULT_SETTINGS})
    except Exception:
        pass
    return data


def save_setting(key: str, value: Any) -> dict[str, Any]:
    if key not in DEFAULT_SETTINGS:
        raise KeyError(key)
    with _settings_lock:
        data = load_settings()
        if key == "language" and value not in LANGUAGES:
            raise KeyError(key)
        data[key] = type(DEFAULT_SETTINGS[key])(value)
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            append_log(TRAY_LOG, f"could not save settings: {exc}")
    if key == "language":
        forget_language()
    return data


# ---------------------------------------------------------------- start with Windows
def _autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{sys.executable}" --tray'
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe")
    script = Path(__file__).resolve().parent / "openhop_app.py"
    return f'"{pyw if pyw.is_file() else exe}" "{script}" --tray'


def autostart_enabled() -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_VALUE)
            return True
    except Exception:
        return False


def set_autostart(enable: bool) -> bool:
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
            if enable:
                winreg.SetValueEx(k, RUN_VALUE, 0, winreg.REG_SZ, _autostart_command())
            else:
                try:
                    winreg.DeleteValue(k, RUN_VALUE)
                except FileNotFoundError:
                    pass
        save_setting("start_with_windows", enable)
        return True
    except Exception as exc:
        append_log(TRAY_LOG, f"autostart change failed: {exc}")
        return False


def apply_first_run_defaults() -> None:
    """Apply the default 'start with Windows' choice once; afterwards only the user changes it.
    Also refresh the command if the app moved (e.g. a new exe in another folder)."""
    s = load_settings()
    if not s["autostart_configured"]:
        set_autostart(bool(s["start_with_windows"]))
        save_setting("autostart_configured", True)
    elif s["start_with_windows"] and autostart_enabled():
        set_autostart(True)


# ---------------------------------------------------------------- tray
class Tray:
    """Tray icon + background status watcher. All callbacks run on the tray thread."""

    def __init__(self, api: Any, icon_path: Path, on_open: Callable[[], None], on_quit: Callable[[], None]):
        self.api = api
        self.icon_path = icon_path
        self.on_open = on_open
        self.on_quit = on_quit
        self.icon = None
        self.status = "unknown"
        self._base = None
        self._stop = threading.Event()
        self._last_notified: dict[str, float] = {}
        self._bad_streak = 0
        self._last_hops: int | None = None
        self._inc_seen: tuple[int, str] = (0, "none")  # (incident id, phase) already notified
        self.notices = NoticePopups(self)
        self.menu = TrayMenu(self)

    # -- lifecycle
    def start(self) -> bool:
        try:
            import pystray
            from PIL import Image
            self._base = Image.open(self.icon_path).convert("RGBA").resize((64, 64), Image.LANCZOS) \
                if self.icon_path.is_file() else Image.new("RGBA", (64, 64), (242, 179, 24, 255))
            menu = pystray.Menu(
                pystray.MenuItem(lambda _i: L("Open openhop", "Abrir openhop"), lambda: self.on_open(), default=True),
                pystray.MenuItem(lambda _i: L("Get a new IP", "Conseguir una IP nueva"), lambda: self._run_bg(self._hop)),
                pystray.MenuItem(lambda _i: L("Fix everything", "Arreglarlo todo"), lambda: self._run_bg(self._fix_all)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem(lambda _i: L("Quit openhop", "Salir de openhop"), lambda: self.on_quit()),
            )
            self.icon = _make_icon(pystray, self._image("unknown"), menu, self._on_right_click)
            threading.Thread(target=self.icon.run, name="openhop-tray", daemon=True).start()
            threading.Thread(target=self._watch, name="openhop-tray-watch", daemon=True).start()
            return True
        except Exception as exc:
            append_log(TRAY_LOG, f"tray unavailable: {exc}")
            self.icon = None
            return False

    def _on_right_click(self, x: int, y: int) -> bool:
        """Return True if our branded menu handled the click (otherwise the native menu is shown)."""
        return self.menu.show(x, y)

    def menu_action(self, action: str) -> None:
        if action == "open":
            self.on_open()
        elif action == "hop":
            self._run_bg(self._hop)
        elif action in ("fix", "fix_all"):
            self._run_bg(self._fix_all)
        elif action == "quit":
            self.on_quit()

    def menu_state(self) -> dict:
        state = {}
        try:
            state = self.api.get_state_cached(max_age=60.0)
        except Exception:
            pass
        health = state.get("health") or {}
        stats = state.get("stats") or {}
        return {"status": health.get("status", self.status), "title": health.get("title", ""),
                "sub": health.get("sub", ""), "hops": int(stats.get("hops", 0) or 0),
                "last_hop": int(stats.get("last_hop", 0) or 0), "lang": ui_lang()}

    @property
    def alive(self) -> bool:
        return self.icon is not None and not self._stop.is_set()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self.icon:
                self.icon.stop()
        except Exception:
            pass

    # -- icon
    def _image(self, status: str):
        from PIL import ImageDraw
        img = self._base.copy()
        color = _DOT.get(status)
        if color:
            d = ImageDraw.Draw(img)
            d.ellipse((38, 38, 63, 63), fill=(31, 33, 37, 255))
            d.ellipse((42, 42, 59, 59), fill=color + (255,))
            if status == "busy":  # a ring instead of a dot: "working on it", not "warning"
                d.ellipse((47, 47, 54, 54), fill=(31, 33, 37, 255))
        return img

    def set_status(self, status: str, title: str = "") -> None:
        if not self.icon:
            return
        if status != self.status:
            self.status = status
            try:
                self.icon.icon = self._image(status)
            except Exception:
                pass
        try:
            tip = f"openhop: {title}" if title else _tooltip(status)
            self.icon.title = tip[:120]
        except Exception:
            pass

    # -- actions
    @staticmethod
    def _run_bg(fn: Callable[[], None]) -> None:
        threading.Thread(target=fn, daemon=True).start()

    def _hop(self) -> None:
        from proxy_chain import get_egress_config
        if get_egress_config().get("mode") == "vpn":
            self.on_open()
            return
        self.notify("info", L("Getting a new IP…", "Consiguiendo una IP nueva…"),
                    L("This takes a few seconds.", "Tarda unos segundos."), force=True)
        try:
            res = self.api.hop_now()
        except Exception as exc:
            res = {"ok": False, "message": str(exc)}
        ok = bool(res.get("ok"))
        self.notify("hop" if ok else "problem",
                    L("Got a new IP", "IP nueva lista") if ok else L("Couldn't get a new IP", "No se pudo conseguir una IP nueva"),
                    _one_line(res.get("message", "")), force=True)
        self._last_hops = None  # don't double-notify this hop from the watcher

    def _fix_all(self) -> None:
        self.notify("info", L("Fixing everything…", "Arreglándolo todo…"),
                    L("Checking each part of openhop.", "Revisando cada parte de openhop."), force=True)
        try:
            res = self.api.fix_everything()
        except Exception as exc:
            res = {"ok": False, "message": str(exc)}
        self.notify("info" if res.get("ok") else "problem",
                    L("All fixed", "Todo arreglado") if res.get("ok") else L("Some problems are left", "Quedan algunos problemas"),
                    _one_line(res.get("message", "")), action_label="" if res.get("ok") else L("Open", "Abrir"), force=True)

    # -- watcher
    def _watch(self) -> None:
        time.sleep(4.0)
        while not self._stop.is_set():
            try:
                self._tick()
            except Exception as exc:
                append_log(TRAY_LOG, f"watch error: {exc}")
            phase = self._inc_seen[1]
            self._stop.wait(INCIDENT_POLL_SECONDS if phase in ("fixing", "checking", "paused") else POLL_SECONDS)

    def _tick(self) -> None:
        state = self.api.get_state_cached(max_age=INCIDENT_POLL_SECONDS - 1
                                          if self._inc_seen[1] in ("fixing", "checking", "paused") else POLL_SECONDS - 2)
        health = state.get("health") or {}
        status = health.get("status", "unknown")
        inc = health.get("incident") or {}
        tip = health.get("title", "")
        if inc.get("phase") == "fixing":
            tip = f"{tip} " + L("(address {n} of up to {m})", "(dirección {n} de {m} como mucho)").format(
                n=inc.get('attempt', 1), m=inc.get('max', 15))
        self.set_status(status, tip)
        settings = load_settings()

        # problems: only after two bad polls in a row (ignores blips during a rotation)
        self._bad_streak = self._bad_streak + 1 if status == "bad" else 0
        if self._bad_streak == 2 and settings["notify_problems"]:
            issues = [i for i in health.get("issues", []) if i.get("level") == "bad"]
            first = issues[0] if issues else {"title": health.get("title", L("openhop needs attention",
                                                                              "openhop necesita atención")), "detail": ""}
            self.notify("problem", first.get("title", ""), _one_line(first.get("detail", "")),
                        action_label=first.get("fix_label") or L("Open", "Abrir"), action=first.get("fix") or "open",
                        key="problem:" + first.get("title", ""))

        # OpenCode Desktop never closed, so openhop's changes to it are still waiting: say it once per run, with a
        # button, because someone who never opens openhop would never see the health panel's row.
        restart = next((i for i in health.get("issues", []) if i.get("fix") == "restart_opencode"), None)
        if restart and settings["notify_problems"] and "restart_opencode" not in self._last_notified:
            self.notify("info", restart.get("title", ""), _one_line(restart.get("detail", "")),
                        action_label=restart.get("fix_label", ""), action="restart_opencode",
                        key="restart_opencode", force=True)

        continue_issue = next((i for i in health.get("issues", []) if i.get("code") == "continue_failed"), None)
        inc_id = int((health.get("incident") or {}).get("id", 0) or 0)
        key = f"continue-failed:{inc_id}"
        if continue_issue and settings["notify_hops"] and key not in self._last_notified:
            self.notify("problem", continue_issue["title"], _one_line(continue_issue["detail"]),
                        key=key, force=True)

        # The free limit: one notice when openhop starts fixing it, one if it has to wait, one when it's fixed.
        # Not one per new IP (several in a row can already be used up, and "you can keep going" was a lie then).
        self._incident_notices(health.get("incident") or {}, settings["notify_hops"], show_done=not continue_issue)
        self._auto_tor_notice(state.get("egress") or {}, settings["notify_hops"])
        self._chat_move_notices(((state.get("services") or {}).get("chat_moves")) or [], settings["notify_hops"])
        self._last_hops = int((state.get("stats") or {}).get("hops", 0) or 0)

    def _chat_move_notices(self, moves: list, enabled: bool) -> None:
        """One popup per chat openhop moved to a new chat: the user is still looking at the old one."""
        if not enabled:
            return
        for mv in moves[:3]:
            new = str(mv.get("new") or "")
            if not new or time.time() - float(mv.get("at", 0) or 0) > 10 * 60:
                continue
            key = f"chatmove:{new}"
            if key in self._last_notified:
                continue
            title = str(mv.get("title") or L("the new chat", "el chat nuevo"))
            self.notify("hop", L("Your chat continues in a new one", "Tu chat sigue en uno nuevo"),
                        L(f"OpenCode now limits each chat, so openhop carried the work on in \u201c{title}\u201d. "
                          f"Open it from OpenCode's chat list.",
                          f"OpenCode ahora limita cada chat, así que openhop siguió el trabajo en \u201c{title}\u201d. "
                          f"Ábrelo desde la lista de chats de OpenCode."),
                        key=key, force=True)

    AUTO_TOR_NOTICE_WINDOW = 15 * 60   # after an app restart, don't announce a switch from hours ago

    def _auto_tor_notice(self, egress: dict, enabled: bool) -> None:
        """One popup when Zen Guard switches to Tor by itself (WARP's addresses were used up)."""
        until = int(egress.get("auto_tor_until", 0) or 0)
        since = int(egress.get("auto_tor_since", 0) or 0)
        if not enabled or egress.get("mode") != "tor" or not until or time.time() - since > self.AUTO_TOR_NOTICE_WINDOW:
            return
        key = f"autotor:{since}"
        if key in self._last_notified:
            return
        back = time.strftime("%H:%M", time.localtime(until))
        self.notify("hop", L("openhop switched to Tor", "openhop ha cambiado a Tor"),
                    L(f"WARP's addresses are used up right now, so openhop switched to Tor. "
                      f"It goes back to WARP after {back}.",
                      f"Las direcciones de WARP están gastadas ahora mismo, así que openhop ha cambiado a Tor. "
                      f"Vuelve a WARP después de las {back}."),
                    key=key, force=True)

    def _incident_notices(self, inc: dict, enabled: bool, show_done: bool = True) -> None:
        iid, phase = int(inc.get("id", 0) or 0), str(inc.get("phase", "none"))
        seen_id, seen_phase = self._inc_seen
        self._inc_seen = (iid, phase)
        if not enabled or not iid or (iid, phase) == (seen_id, seen_phase):
            return
        new = iid != seen_id
        if phase in ("fixing", "checking") and new:
            self.notify("hop", L("Changing the connection…", "Cambiando la conexión…"),
                        L("Check your chat in OpenCode once the connection is back.",
                          "Comprueba tu chat en OpenCode cuando vuelva la conexión."), key=f"inc{iid}:start", force=True)
        elif phase == "paused" and (new or seen_phase != "paused"):
            at = time.strftime("%H:%M", time.localtime(float(inc.get("retry_at", 0) or time.time())))
            tor = self._egress_mode() in ("warp", "clean")
            self.notify("problem", L("Every address tried was already used up",
                                     "Todas las direcciones probadas ya estaban gastadas"),
                        L(f"Your chat is paused, not lost. openhop tries again by itself at {at}",
                          f"Tu chat está en pausa, no perdido. openhop lo vuelve a intentar solo a las {at}")
                        + (L(", or switch to Tor now.", ", o cambia a Tor ahora.") if tor else "."),
                        action_label=L("Try Tor", "Probar Tor") if tor else L("Try now", "Probar ya"),
                        action="use_tor" if tor else "kick_guard",
                        key=f"inc{iid}:paused", force=True)
        elif phase == "done" and not new and seen_phase in ("fixing", "checking", "paused") and show_done:
            self.notify("hop", L("Check your chat", "Comprueba tu chat"),
                        L("The connection changed. Check your chat in OpenCode for a reply.",
                          "La conexión ha cambiado. Mira en OpenCode si tu chat ha recibido una respuesta."),
                        key=f"inc{iid}:done", force=True)

    def _egress_mode(self) -> str:
        try:
            return str((self.api.get_state_cached(max_age=60.0).get("egress") or {}).get("mode", "warp"))
        except Exception:
            return "warp"

    # -- notifications
    def notify(self, kind: str, title: str, body: str, action_label: str = "", action: str = "",
               key: str | None = None, force: bool = False, body_action: str = "open") -> None:
        now = time.time()
        key = key or f"{kind}:{title}"
        if not force:
            gap = HOP_REPEAT_SECONDS if kind == "hop" else PROBLEM_REPEAT_SECONDS
            if now - self._last_notified.get(key, 0) < gap:
                return
        self._last_notified[key] = now
        if self.notices.show(kind, title, body, action_label, action, body_action):
            return
        try:  # native fallback
            if self.icon:
                self.icon.notify(body or " ", title)
        except Exception as exc:
            append_log(TRAY_LOG, f"native notification failed: {exc}")

    def handle_action(self, action: str) -> None:
        if not action or action == "open":
            self.on_open()
            return
        if action == "show_update":
            try:
                self.api.show_update()
            except Exception as exc:
                append_log(TRAY_LOG, f"show update failed: {exc}")
                self.on_open()
            return
        if action == "update":
            try:
                res = self.api.apply_update()
                if not res.get("ok"):
                    self.notify("problem", L("Couldn't update", "No se pudo actualizar"),
                                _one_line(res.get("message", "")), force=True)
            except Exception as exc:
                append_log(TRAY_LOG, f"update action failed: {exc}")
            return
        try:
            res = self.api.fix_issue(action)
            ok = bool(res.get("ok"))
            self.notify("info" if ok else "problem", L("Fixed", "Arreglado") if ok else L("Couldn't fix it", "No se pudo arreglar"),
                        _one_line(res.get("message", "")), force=True)
        except Exception as exc:
            append_log(TRAY_LOG, f"notice action failed: {exc}")


def _one_line(text: str, limit: int = 140) -> str:
    text = " ".join(str(text or "").split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


# ---------------------------------------------------------------- custom popups
class _NoticeApi:
    """JS API for src/ui/notify.html (hooks: notice_action, notice_close)."""

    def __init__(self, popups: "NoticePopups", nid: int):
        self._popups = popups
        self._nid = nid

    def notice_action(self, _id: Any = None) -> None:
        self._popups._on_action(self._nid)

    def notice_close(self) -> None:
        self._popups._close(self._nid)

    def notice_body(self, _id: Any = None) -> None:
        """The card itself was clicked (not a button)."""
        self._popups._on_action(self._nid, body=True)


class NoticePopups:
    """Branded notification cards (bottom-right). Shown without taking focus or a taskbar button."""

    WIDTH, HEIGHT, MARGIN, LIFETIME = 360, 96, 16, 7.0

    def __init__(self, tray: Tray):
        self.tray = tray
        self._seq = 0
        self._open: dict[int, tuple[Any, str]] = {}
        self._lock = threading.Lock()

    @staticmethod
    def html_path() -> Path | None:
        return _ui_file("notify.html")

    def show(self, kind: str, title: str, body: str, action_label: str, action: str,
             body_action: str = "open") -> bool:
        path = self.html_path()
        if path is None:
            return False
        try:
            import webview
            if not webview.windows:  # GUI loop not running yet
                return False
            with self._lock:
                for nid in list(self._open):
                    self._close(nid)
                self._seq += 1
                nid = self._seq
            left, top, right, bottom = _work_area()
            scale = _dpi_scale()
            w, h = int(self.WIDTH * scale), int(self.HEIGHT * scale)
            x, y = right - w - int(self.MARGIN * scale), bottom - h - int(self.MARGIN * scale)
            title_tag = f"openhop notice {nid}"
            win = webview.create_window(
                title_tag, html=path.read_text(encoding="utf-8"), js_api=_NoticeApi(self, nid),
                width=self.WIDTH, height=self.HEIGHT, x=int(x / scale), y=int(y / scale),
                frameless=True, easy_drag=False, resizable=False, on_top=True, focus=False,
                shadow=False, transparent=False, background_color="#1f2125",
            )
            payload = json.dumps({"kind": kind, "title": title, "body": body, "action_label": action_label,
                                  "lang": ui_lang()})

            def _shown() -> None:
                _make_noactivate_toolwindow(title_tag, (x, y, w, h))
                try:
                    win.evaluate_js(f"window.showNotice && window.showNotice({payload})")
                except Exception:
                    pass

            win.events.loaded += _shown
            with self._lock:
                self._open[nid] = (win, action, body_action or "open")
            threading.Timer(self.LIFETIME, lambda: self._close(nid)).start()
            return True
        except Exception as exc:
            append_log(TRAY_LOG, f"custom notice failed, using native: {exc}")
            return False

    def _close(self, nid: int) -> None:
        with self._lock:
            item = self._open.pop(nid, None)
        if item:
            try:
                item[0].destroy()
            except Exception:
                pass

    def _on_action(self, nid: int, body: bool = False) -> None:
        with self._lock:
            item = self._open.get(nid)
        action = (item[2] if body else item[1]) if item else ""
        self._close(nid)
        threading.Thread(target=self.tray.handle_action, args=(action,), daemon=True).start()


# ---------------------------------------------------------------- Win32 helpers
class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


class _RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long), ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def _work_area() -> tuple[int, int, int, int]:
    r = _RECT()
    _U32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)  # SPI_GETWORKAREA (primary)
    return r.left, r.top, r.right, r.bottom


def _dpi_scale() -> float:
    try:
        return _U32.GetDpiForSystem() / 96.0
    except Exception:
        return 1.0


def _make_noactivate_toolwindow(title: str, rect: tuple[int, int, int, int] | None = None) -> None:
    """No taskbar button, never steals focus, stays on top."""
    user32 = _U32
    for _ in range(20):
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            break
        time.sleep(0.05)
    else:
        return
    GWL_EXSTYLE, WS_EX_TOOLWINDOW, WS_EX_NOACTIVATE, WS_EX_APPWINDOW = -20, 0x80, 0x08000000, 0x40000
    get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    put = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get.restype = ctypes.c_ssize_t
    put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    style = get(ctypes.c_void_p(hwnd), GWL_EXSTYLE)
    put(ctypes.c_void_p(hwnd), GWL_EXSTYLE, (style | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE) & ~WS_EX_APPWINDOW)
    _round_corners(hwnd)
    # HWND_TOPMOST, SWP_NOMOVE|SWP_NOSIZE|SWP_NOACTIVATE|SWP_FRAMECHANGED|SWP_SHOWWINDOW
    if rect:  # physical pixels, so DPI scaling can't misplace it
        x, y, w, h = rect
        user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), x, y, w, h, 0x0010 | 0x0020 | 0x0040)
    else:
        user32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010 | 0x0020 | 0x0040)


# ---------------------------------------------------------------- branded tray menu
class _MenuApi:
    """JS API for src/ui/traymenu.html: menu_action(id), menu_close(), menu_state()."""

    def __init__(self, menu: "TrayMenu"):
        self._menu = menu

    def menu_action(self, action: str) -> None:
        self._menu.hide()
        threading.Thread(target=self._menu.tray.menu_action, args=(str(action),), daemon=True).start()

    def menu_close(self) -> None:
        # The page reports "blur" when the hidden-icons flyout or the taskbar takes focus back right after the
        # click; ignore that first moment (the Python watcher closes the menu on real clicks elsewhere).
        if time.time() - self._menu._shown_at < 0.6:
            return
        self._menu.hide()

    def menu_state(self) -> dict:
        return self._menu.tray.menu_state()


class TrayMenu:
    """Our own right-click menu: a small frameless window next to the tray icon, closed on blur."""

    WIDTH, HEIGHT = 280, 312  # CSS px (the page should fit this box)
    TITLE = "openhop menu"

    def __init__(self, tray: Tray):
        self.tray = tray
        self.win = None
        self._ready = threading.Event()
        self._lock = threading.Lock()
        self._visible = False
        self._hidden_at = 0.0
        self._shown_at = 0.0

    def show(self, x: int, y: int) -> bool:
        path = _ui_file("traymenu.html")
        if path is None:
            return False
        try:
            import webview
        except Exception:
            return False
        if self.win is None and not webview.windows:  # GUI loop not running
            return False
        if self._visible:  # right-click again = close
            self.hide()
            return True
        if time.time() - self._hidden_at < 0.3:  # this click is what just closed it
            return True
        threading.Thread(target=self._show, args=(path, x, y), daemon=True).start()
        return True

    def _show(self, path: Path, x: int, y: int) -> None:
        with self._lock:
            try:
                import webview
                if self.win is None:
                    self.win = webview.create_window(
                        self.TITLE, html=path.read_text(encoding="utf-8"), js_api=_MenuApi(self),
                        width=self.WIDTH, height=self.HEIGHT, frameless=True, easy_drag=False,
                        resizable=False, on_top=True, shadow=False, transparent=False,
                        background_color="#1f2125", hidden=True,
                    )
                    self.win.events.loaded += self._ready.set
                    if not self._ready.wait(8.0):
                        raise RuntimeError("menu page did not load")
                hwnd = _find_window(self.TITLE)
                if not hwnd:
                    raise RuntimeError("menu window not found")
                _set_ex_style(hwnd, add=0x80, remove=0x40000)  # tool window: no taskbar button
                _round_corners(hwnd)
                scale = _dpi_scale()
                w, h = int(self.WIDTH * scale), int(self.HEIGHT * scale)
                left, top, right, bottom = _work_area()
                px = max(left, min(x - w, right - w))
                py = max(top, min(y - h, bottom - h))
                state = json.dumps(self.tray.menu_state())
                self.win.evaluate_js(f"window.renderMenu && window.renderMenu({state})")
                user32 = _U32
                # Place it while still hidden, show it, then keep checking for a moment: the first time a
                # WinForms window is shown, Windows applies its own default spot (top-left) and size AFTER
                # our move, which put the menu in the corner and cut off its last items.
                _place_window(hwnd, px, py, w, h)
                self.win.show()
                for _ in range(40):
                    if user32.IsWindowVisible(ctypes.c_void_p(hwnd)):
                        break
                    time.sleep(0.025)
                _place_window(hwnd, px, py, w, h, show=True)
                user32.SetForegroundWindow(ctypes.c_void_p(hwnd))
                self._shown_at = time.time()
                self._visible = True
                for _ in range(10):
                    time.sleep(0.05)
                    if not self._visible:
                        break
                    if _window_rect(hwnd) != (px, py, w, h):
                        _place_window(hwnd, px, py, w, h, show=True)
            except Exception as exc:
                append_log(TRAY_LOG, f"branded menu failed: {exc}")
                return
        threading.Thread(target=self._watch_dismiss, args=(hwnd,), daemon=True).start()

    def _watch_dismiss(self, hwnd: int) -> None:
        """Close the menu when the user clicks somewhere else or switches to another window."""
        user32 = _U32
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        user32.GetAncestor.restype = ctypes.c_void_p
        user32.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
        had_focus = False
        start = time.time()
        while self._visible:
            time.sleep(0.08)
            fg = user32.GetForegroundWindow() or 0
            root = (user32.GetAncestor(ctypes.c_void_p(fg), 2) or 0) if fg else 0  # GA_ROOT
            mine = hwnd in (fg, root)
            if mine:
                had_focus = True
            elif had_focus:
                self.hide()
                break
            elif time.time() - start > 0.6:
                # never got focus (e.g. Windows refused the switch): close on any click outside the menu
                pressed = any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (0x01, 0x02))
                if pressed:
                    pt = _POINT()
                    user32.GetCursorPos(ctypes.byref(pt))
                    lx, ly, lw, lh = _window_rect(hwnd)
                    if not (lx <= pt.x < lx + lw and ly <= pt.y < ly + lh):
                        self.hide()
                        break

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._hidden_at = time.time()
        try:
            self.win.hide()
        except Exception:
            pass


def _make_icon(pystray: Any, image: Any, menu: Any, on_right_click: Callable[[int, int], bool]) -> Any:
    """pystray icon whose right-click opens our branded menu (native menu only as a fallback)."""
    try:
        from ctypes import wintypes
        from pystray import _win32
        from pystray._util import win32 as pwin32

        class _OpenhopIcon(_win32.Icon):
            def _on_notify(self, wparam, lparam):  # noqa: N802 (pystray naming)
                if lparam == pwin32.WM_RBUTTONUP:
                    pt = wintypes.POINT()
                    _U32.GetCursorPos(ctypes.byref(pt))
                    try:
                        if on_right_click(pt.x, pt.y):
                            return None
                    except Exception as exc:
                        append_log(TRAY_LOG, f"right-click handler failed: {exc}")
                return super()._on_notify(wparam, lparam)

        return _OpenhopIcon("openhop", image, "openhop", menu)
    except Exception as exc:
        append_log(TRAY_LOG, f"custom icon class unavailable, using default: {exc}")
        return pystray.Icon("openhop", image, "openhop", menu)


# ---------------------------------------------------------------- app identity (notification header)
APP_ID = "bernatlol.openhop.controller"


def register_app_identity(icon_path: Path) -> None:
    """Make Windows show 'openhop' + our logo (not 'Python') on notifications and the taskbar."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    except Exception:
        pass
    try:
        import winreg
        from PIL import Image
        png = DEST_DIR / "openhop.png"
        if icon_path.is_file() and (not png.is_file() or png.stat().st_mtime < icon_path.stat().st_mtime):
            png.parent.mkdir(parents=True, exist_ok=True)
            Image.open(icon_path).convert("RGBA").resize((256, 256), Image.LANCZOS).save(png)
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, rf"Software\Classes\AppUserModelId\{APP_ID}") as k:
            winreg.SetValueEx(k, "DisplayName", 0, winreg.REG_SZ, "openhop")
            if png.is_file():
                winreg.SetValueEx(k, "IconUri", 0, winreg.REG_SZ, str(png))
            winreg.SetValueEx(k, "IconBackgroundColor", 0, winreg.REG_SZ, "FF1F2125")
    except Exception as exc:
        append_log(TRAY_LOG, f"could not register app identity: {exc}")


# ---------------------------------------------------------------- shared helpers
def _ui_file(name: str) -> Path | None:
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent))
    for c in (base / "src" / "ui" / name, base / "ui" / name, Path(__file__).resolve().parent / "ui" / name):
        if c.is_file():
            return c
    return None


def _find_window(title: str, tries: int = 20) -> int:
    for _ in range(tries):
        hwnd = _U32.FindWindowW(None, title)
        if hwnd:
            return hwnd
        time.sleep(0.05)
    return 0


def _window_rect(hwnd: int) -> tuple[int, int, int, int]:
    r = _RECT()
    _U32.GetWindowRect(ctypes.c_void_p(hwnd), ctypes.byref(r))
    return r.left, r.top, r.right - r.left, r.bottom - r.top


def _place_window(hwnd: int, x: int, y: int, w: int, h: int, show: bool = False) -> None:
    """Topmost at x,y with size w,h (physical pixels) without activating it; optionally make it visible."""
    flags = 0x0010 | (0x0040 if show else 0)  # SWP_NOACTIVATE (| SWP_SHOWWINDOW)
    _U32.SetWindowPos(ctypes.c_void_p(hwnd), ctypes.c_void_p(-1), x, y, w, h, flags)


def _round_corners(hwnd: int) -> None:
    """Windows 11: native rounded corners + a subtle border (does nothing on Windows 10)."""
    try:
        dwm = ctypes.WinDLL("dwmapi")
        pref = ctypes.c_int(2)  # DWMWA_WINDOW_CORNER_PREFERENCE = DWMWCP_ROUND
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 33, ctypes.byref(pref), 4)
        border = ctypes.c_uint(0x00463F3A)  # DWMWA_BORDER_COLOR, COLORREF 0x00BBGGRR = #3a3f46
        dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), 34, ctypes.byref(border), 4)
    except Exception:
        pass


def _set_ex_style(hwnd: int, add: int = 0, remove: int = 0) -> None:
    user32 = _U32
    get = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
    put = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
    get.restype = ctypes.c_ssize_t
    put.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_ssize_t]
    style = get(ctypes.c_void_p(hwnd), -20)  # GWL_EXSTYLE
    put(ctypes.c_void_p(hwnd), -20, (style | add) & ~remove)

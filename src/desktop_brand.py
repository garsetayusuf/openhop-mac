"""Show "openhop.cfd" as the wordmark on OpenCode Desktop's start screen (optional, on by default).

Safety first: a working OpenCode always beats the visual change.
- Only CSS is added (one <style> block in the renderer's index.html). OpenCode's code is not touched: the
  original wordmark (an interactive animation) keeps running, just hidden, and our wordmark is drawn on top.
  If a future OpenCode changes the markup, the CSS simply matches nothing.
- The app.asar archive is rewritten with the index.html entry pointing at a new copy appended at the end;
  every other byte stays identical. A full backup is kept and the result is checked before it replaces the
  original. Any failure restores the backup and gives up quietly.
- Never while OpenCode is running (it reads app.asar live). The keeper retries later, when it's closed.
- Skipped when Electron's asar integrity check is enabled in OpenCode.exe (patching would break the app).
- Turning the switch off puts the original file back.
- Also (own setting, on by default): OpenCode's "Free limit reached" card that replaces the chat box is
  switched off, because openhop has already moved to a new IP. See _banner_js.
- Also (own setting, on by default): one <script> block with openhop's extras (voice typing button, website /
  :icon: box for project icons). It only adds elements next to OpenCode's own; see opencode_extras_js.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import struct
import time
from pathlib import Path

from ouc_common import DEST_DIR, LOCAL_APPDATA, L, append_log

SETTING_KEY = "desktop_brand"
MARKER = "openhop-wordmark"
LOG_FILE = DEST_DIR / "logs" / "desktop_brand.log"
BACKUP_DIR = DEST_DIR / "desktop_backup"
BACKUP_ASAR = BACKUP_DIR / "app.asar"
BACKUP_META = BACKUP_DIR / "backup.json"
INDEX_PATH = ("out", "renderer", "index.html")
_FUSE_SENTINEL = b"dL7pKGdnNz796PbbjQWNKmHXBZaB9tsX"
_ASAR_INTEGRITY_FUSE = 4  # EnableEmbeddedAsarIntegrityValidation


def _log(msg: str) -> None:
    append_log(LOG_FILE, msg)


# ---------------------------------------------------------------------------------------------- the wordmark
# Same 6-unit pixel grid as OpenCode's own wordmark (letters 24 wide, 30 apart, x-height 6..36, ascenders from 0,
# descenders to 42). Each glyph: (weak "shadow" path, main path), drawn at x = 0.
_GLYPHS = {
    "o": ("M18 30H6V18H18V30Z", "M18 12H6V30H18V12ZM24 36H0V6H24V36Z"),
    "p": ("M18 30H6V18H18V30Z", "M6 30H18V12H6V30ZM24 36H6V42H0V6H24V36Z"),
    "e": ("M24 24V30H6V24H24Z", "M24 24H6V30H24V36H0V6H24V24ZM6 18H18V12H6V18Z"),
    "n": ("M18 36H6V18H18V36Z", "M18 12H6V36H0V6H18V12ZM24 36H18V12H24V36Z"),
    "h": ("M18 36H6V18H18V36Z", "M18 12H6V36H0V0H6V6H18V12ZM24 36H18V12H24V36Z"),
    "c": ("M24 30H6V18H24V30Z", "M24 12H6V30H24V36H0V6H24V12Z"),
    "f": ("", "M12 36H6V18H0V12H6V0H24V6H12V12H18V18H12V36Z"),
    "d": ("M18 30H6V18H18V30Z", "M18 12H6V30H18V12ZM24 36H0V6H18V0H24V36Z"),
    ".": ("", "M6 36H0V30H6V36Z"),
}
_TEXT = (("open", "base"), ("hop", "strong"), (".cfd", "base"))


def _shift(path: str, dx: int) -> str:
    out, cmd = [], ""
    for tok in re.findall(r"[MHVZ]|-?\d+(?:\.\d+)?", path):
        if tok in "MHVZ":
            cmd = tok
            out.append(tok)
            continue
        n = float(tok)
        if cmd == "H" or (cmd == "M" and (not out or out[-1] == "M")):
            n += dx
        out.append(f"{n:g}")
        if cmd == "M" and out[-2] != "M":  # after M x y, any further numbers are not x
            cmd = "_"
    s = ""
    for tok in out:
        s += tok if (tok in "MHVZ" or not s or s[-1] in "MHVZ") else " " + tok
    return s


def wordmark_svgs() -> tuple[str, str, int]:
    """(mask for the base-colour layer incl. all shadows, mask for the strong layer, width)."""
    base, strong, x = [], [], 0
    for word, tone in _TEXT:
        for ch in word:
            weak, main = _GLYPHS[ch]
            if weak:
                base.append(f'<path d="{_shift(weak, x)}" fill-opacity=".32"/>')
            (strong if tone == "strong" else base).append(f'<path d="{_shift(main, x)}"/>')
            x += 12 if ch == "." else 30
    width = x - 6

    def svg(paths: list[str]) -> str:
        return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} 42" fill="#000">'
                + "".join(paths) + "</svg>")

    return svg(base), svg(strong), width


def _data_uri(svg: str) -> str:
    from urllib.parse import quote
    return "data:image/svg+xml," + quote(svg, safe="/:=;,. ")  # quotes encoded: it sits inside url("...")


def style_block() -> str:
    base, strong, _ = wordmark_svgs()
    box = "[data-component=new-session-wordmark] [data-slot=wordmark-box]"
    # opacity .16 = how faint OpenCode draws its own letters there (class opacity-[0.16])
    layer = "content:'';position:absolute;inset:0;pointer-events:none;opacity:.16;"

    def mask(svg: str) -> str:
        m = f'url("{_data_uri(svg)}") center/contain no-repeat'
        return f"-webkit-mask:{m};mask:{m};"

    css = (
        f"{box}>*{{visibility:hidden!important;pointer-events:none!important}}"
        f"{box}::before{{{layer}{mask(base)}background:var(--icon-base,currentColor)}}"
        f"{box}::after{{{layer}{mask(strong)}background:var(--icon-strong-base,currentColor)}}"
    )
    return f'<style id="{MARKER}">/* added by openhop (openhop.cfd); turn off in openhop > openhop app */{css}</style>'


# ---------------------------------------------------------------------------------------------- asar helpers
def _read_header(fh) -> tuple[dict, int]:
    """Return (header json, offset where file data starts)."""
    fh.seek(0)
    raw = fh.read(16)
    if len(raw) < 16:
        raise ValueError("not an asar file")
    _, header_size, _, json_len = struct.unpack("<IIII", raw)
    if json_len <= 0 or json_len > 64 * 1024 * 1024:
        raise ValueError("bad asar header size")
    header = json.loads(fh.read(json_len).decode("utf-8"))
    return header, 8 + header_size


def _header_bytes(header: dict) -> bytes:
    js = json.dumps(header, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    pad = (4 - len(js) % 4) % 4
    payload = 4 + len(js) + pad
    return struct.pack("<IIII", 4, payload + 4, payload, len(js)) + js + b"\0" * pad


def _entry(header: dict, path: tuple[str, ...]) -> dict:
    node = header
    for part in path:
        node = node["files"][part]
    return node


def _read_entry(fh, data_start: int, entry: dict) -> bytes:
    if entry.get("unpacked"):
        raise ValueError("entry is unpacked")
    fh.seek(data_start + int(entry["offset"]))
    return fh.read(int(entry["size"]))


def _looks_rewritten(asar: Path) -> bool:
    """True when the data section holds bytes that no entry points at (this module appends changed files).

    A de-patched archive still has those appended copies and stale originals, so it's no longer a byte-for-byte
    copy of the file OpenCode shipped: it must not replace the pristine backup kept for uninstall.
    """
    try:
        size = asar.stat().st_size
        with asar.open("rb") as fh:
            header, start = _read_header(fh)
        used, stack = 0, [header]
        while stack:
            for ent in stack.pop().get("files", {}).values():
                if "files" in ent:
                    stack.append(ent)
                elif not ent.get("unpacked"):
                    used += int(ent.get("size", 0))
        return size - start - used > 4096
    except Exception:
        return True  # can't tell: never overwrite the backup


def _integrity(data: bytes) -> dict:
    block = 4 * 1024 * 1024
    return {
        "algorithm": "SHA256",
        "hash": hashlib.sha256(data).hexdigest(),
        "blockSize": block,
        "blocks": [hashlib.sha256(data[i:i + block]).hexdigest() for i in range(0, max(len(data), 1), block)],
    }


# ---------------------------------------------------------------------------------------------- finding OpenCode
def desktop_dir() -> Path | None:
    for d in (LOCAL_APPDATA / "Programs" / "ai.opencode.desktop", LOCAL_APPDATA / "Programs" / "OpenCode",
              Path(r"C:\Program Files\OpenCode"), Path(r"C:\Program Files\ai.opencode.desktop")):
        if (d / "resources" / "app.asar").is_file() and (d / "OpenCode.exe").is_file():
            return d
    return None


def _asar_integrity_enabled(exe: Path) -> bool | None:
    """True/False from Electron's fuse wire in the exe; None if it can't be read (treated as 'don't patch')."""
    try:
        data = exe.read_bytes()
        i = data.find(_FUSE_SENTINEL)
        if i < 0:
            return None
        length = data[i + len(_FUSE_SENTINEL) + 1]
        fuses = data[i + len(_FUSE_SENTINEL) + 2: i + len(_FUSE_SENTINEL) + 2 + length]
        if len(fuses) <= _ASAR_INTEGRITY_FUSE:
            return False
        return fuses[_ASAR_INTEGRITY_FUSE:_ASAR_INTEGRITY_FUSE + 1] == b"1"
    except Exception:
        return None


def desktop_running() -> bool:
    try:
        import psutil
        d = desktop_dir()
        exe = str(d / "OpenCode.exe").lower() if d else ""
        for p in psutil.process_iter(["name", "exe"]):
            try:
                if (p.info.get("name") or "").lower() == "opencode.exe" and (not exe or (p.info.get("exe") or "").lower() == exe):
                    return True
            except Exception:
                pass
    except Exception:
        return True  # can't tell: assume it is, never patch under it
    return False


def setting_enabled() -> bool:
    try:
        data = json.loads((DEST_DIR / "app_settings.json").read_text(encoding="utf-8"))
        return bool(data.get(SETTING_KEY, True))
    except Exception:
        return True


# ---------------------------------------------------------------------------------------------- the free-limit banner
# When OpenCode Desktop sees a free-limit error it stores a reset time (default: 1 hour) and, until then, shows a
# "Free limit reached" card *instead of* the chat box. openhop has already switched to a new IP and sent "continue"
# by then, so the card is wrong and it hides the prompt. The card's "is it active?" check gets a "false &&" in front:
# the chat box always stays. Only that one expression changes, and it's put back exactly by removing our marker.
BANNER_KEY = "hide_limit_banner"
BANNER_MARK = "/*openhop-nobanner*/!1&&"
_BANNER_ANCHOR = b"data-component=session-free-limit"
_BANNER_RE = re.compile(r"(active:\(\)=>\{let ([\w$]+)=[\w$]+\(\);return)(![\w$]+\(\)&&\2!==null&&\2>[\w$]+\(\)\})")


def banner_setting_enabled() -> bool:
    try:
        data = json.loads((DEST_DIR / "app_settings.json").read_text(encoding="utf-8"))
        return bool(data.get(BANNER_KEY, True))
    except Exception:
        return True


def _find_banner_chunk(fh, header: dict, start: int) -> tuple[tuple[str, ...], str] | None:
    """(asar path, text) of the renderer chunk that holds the free-limit card, or None."""
    try:
        assets = _entry(header, ("out", "renderer", "assets"))["files"]
    except Exception:
        return None
    for name, ent in assets.items():
        if not name.endswith(".js") or "files" in ent or ent.get("unpacked") or int(ent.get("size", 0)) < 50_000:
            continue
        data = _read_entry(fh, start, ent)
        if _BANNER_ANCHOR in data:
            return ("out", "renderer", "assets", name), data.decode("utf-8")
    return None


def _banner_js(text: str, want: bool) -> str | None:
    """The chunk with the banner check switched off (want) or back to OpenCode's own; None if it can't be found."""
    if want:
        if BANNER_MARK in text:
            return text
        # the check sits a few hundred characters after the stored `free-limit` state it reads
        hits = [m for m in _BANNER_RE.finditer(text) if "`free-limit`" in text[max(0, m.start() - 700):m.start()]]
        if len(hits) != 1:
            return None
        m = hits[0]
        return text[:m.end(1)] + BANNER_MARK + text[m.end(1):]
    return text.replace(BANNER_MARK, "")


# ---------------------------------------------------------------------------------------------- the extras script
EXTRAS_KEY = "opencode_extras"
EXTRAS_MARK = "openhop-extras"
_EXTRAS_RE = re.compile(r'<script id="openhop-extras" data-v="(\w+)">.*?</script>\n  ', re.S)


def extras_setting_enabled() -> bool:
    try:
        data = json.loads((DEST_DIR / "app_settings.json").read_text(encoding="utf-8"))
        return bool(data.get(EXTRAS_KEY, True))
    except Exception:
        return True


def _extras_version(html: str) -> str:
    m = _EXTRAS_RE.search(html)
    return m.group(1) if m else ""


def _extras_html(html: str, want: bool) -> str | None:
    html = _EXTRAS_RE.sub("", html)
    if not want:
        return html
    import opencode_extras_js as x
    js = x.script()
    if "</script" in js.lower() or "</body>" not in html:
        return None
    block = f'<script id="{EXTRAS_MARK}" data-v="{x.version()}">{js}</script>\n  '
    return html.replace("</body>", block + "</body>", 1)


def extras_current_version() -> str:
    try:
        import opencode_extras_js as x
        return x.version()
    except Exception:
        return ""


# ---------------------------------------------------------------------------------------------- status / apply / restore
def _inspect(asar: Path) -> dict:
    """What's in the archive now: wordmark/banner patched?, and whether each can be patched."""
    with asar.open("rb") as fh:
        header, start = _read_header(fh)
        html = _read_entry(fh, start, _entry(header, INDEX_PATH)).decode("utf-8", errors="replace")
        chunk = _find_banner_chunk(fh, header, start)
    wordmark = f'<style id="{MARKER}">' in html
    banner = bool(chunk and BANNER_MARK in chunk[1])
    return {
        "wordmark": wordmark,
        "banner": banner,
        "extras": _extras_version(html),
        "wordmark_ok": wordmark or "</head>" in html,
        "banner_ok": banner or bool(chunk and _banner_js(chunk[1], True)),
    }


def status() -> dict:
    d = desktop_dir()
    if not d:
        return {"installed": False, "patched": False, "supported": False, "reason": "OpenCode Desktop not found"}
    try:
        info = _inspect(d / "resources" / "app.asar")
    except Exception as exc:
        return {"installed": True, "patched": False, "supported": False, "reason": f"unreadable: {exc}"}
    supported = info["wordmark"] or (info["wordmark_ok"] and "new-session-wordmark" in _renderer_text_hint(d))
    return {"installed": True, "patched": info["wordmark"], "supported": supported, "reason": "",
            "banner_hidden": info["banner"], "banner_supported": info["banner_ok"],
            "extras": bool(info["extras"]), "extras_current": info["extras"] == extras_current_version(),
            "running": desktop_running(), "backup": BACKUP_ASAR.is_file()}


def _renderer_text_hint(d: Path) -> str:
    """Cheap check that this OpenCode build still has the start-screen wordmark we restyle."""
    try:
        with (d / "resources" / "app.asar").open("rb") as fh:
            header, start = _read_header(fh)
            assets = _entry(header, ("out", "renderer", "assets"))["files"]
            for name, ent in assets.items():
                if name.startswith("route-") and name.endswith(".js") and not ent.get("unpacked"):
                    chunk = _read_entry(fh, start, ent)
                    if b"new-session-wordmark" in chunk:
                        return "new-session-wordmark"
    except Exception:
        pass
    return ""


def _set_state(wordmark: bool | None = None, banner: bool | None = None,
               extras: bool | None = None) -> tuple[bool, str]:
    """Rewrite app.asar so the wordmark / banner patches match what's asked (None = leave as it is).

    Changed files are appended at the end of the archive and the header points at the new copies; every other
    byte stays identical. The result is checked before it replaces the original, and any failure leaves OpenCode
    as it was.
    """
    d = desktop_dir()
    if not d:
        return False, L("OpenCode Desktop isn't installed.", "OpenCode Desktop no está instalado.")
    if desktop_running():
        return False, L("OpenCode Desktop is open; it will be done the next time it's closed.",
                        "OpenCode Desktop está abierto; se hará la próxima vez que se cierre.")
    if _asar_integrity_enabled(d / "OpenCode.exe") is not False:
        return False, L("This OpenCode version protects its files, so openhop leaves it alone.",
                        "Esta versión de OpenCode protege sus archivos, así que openhop no la toca.")
    asar = d / "resources" / "app.asar"
    tmp = asar.with_name("app.asar.openhop-new")
    try:
        changes: dict[tuple[str, ...], bytes] = {}
        with asar.open("rb") as fh:
            header, start = _read_header(fh)
            html = _read_entry(fh, start, _entry(header, INDEX_PATH)).decode("utf-8")
            chunk = _find_banner_chunk(fh, header, start)
        pristine = MARKER not in html and EXTRAS_MARK not in html and not (chunk and BANNER_MARK in chunk[1])
        new_html = html

        if wordmark is not None and wordmark != (f'<style id="{MARKER}">' in html):
            if wordmark:
                if "</head>" not in html or not _renderer_text_hint(d):
                    return False, L("This OpenCode version looks different; nothing was changed.",
                                    "Esta versión de OpenCode es distinta; no se ha cambiado nada.")
                new_html = new_html.replace("</head>", style_block() + "\n  </head>", 1)
            else:
                new_html = re.sub(r'<style id="' + MARKER + r'">.*?</style>\n  ', "", new_html, flags=re.S)
        if extras is not None and (extras_current_version() if extras else "") != _extras_version(html):
            got = _extras_html(new_html, extras)
            if got is None:
                return False, L("This OpenCode version looks different; the extras were left out.",
                                "Esta versión de OpenCode es distinta; los extras no se han puesto.")
            new_html = got
        if new_html != html:
            changes[INDEX_PATH] = new_html.encode("utf-8")
        if banner is not None and chunk and banner != (BANNER_MARK in chunk[1]):
            new_js = _banner_js(chunk[1], banner)
            if new_js is None:
                return False, L("This OpenCode version looks different; the limit card was left alone.",
                                "Esta versión de OpenCode es distinta; el aviso de límite se ha dejado como estaba.")
            changes[chunk[0]] = new_js.encode("utf-8")
        if not changes:
            return True, _nothing()

        # 1. backup of the untouched original (refreshed after every OpenCode update)
        size = asar.stat().st_size
        if pristine and not _looks_rewritten(asar):
            BACKUP_DIR.mkdir(parents=True, exist_ok=True)
            shutil.copy2(asar, BACKUP_ASAR)
            if BACKUP_ASAR.stat().st_size != size:
                raise OSError("backup copy is incomplete")
            BACKUP_META.write_text(json.dumps({"size": size, "mtime": asar.stat().st_mtime, "from": str(asar),
                                               "at": time.strftime("%Y-%m-%d %H:%M:%S")}), encoding="utf-8")

        # 2. new archive: same data, changed files appended at the end and re-pointed in the header
        offset = size - start
        for path, data in changes.items():
            _entry(header, path).update({"offset": str(offset), "size": len(data), "integrity": _integrity(data)})
            offset += len(data)
        with asar.open("rb") as src, tmp.open("wb") as dst:
            dst.write(_header_bytes(header))
            src.seek(start)
            shutil.copyfileobj(src, dst, 8 * 1024 * 1024)
            for data in changes.values():
                dst.write(data)

        # 3. validate before swapping: header parses, our files read back, another file reads the same
        with tmp.open("rb") as fh, asar.open("rb") as orig:
            h2, s2 = _read_header(fh)
            for path, data in changes.items():
                if _read_entry(fh, s2, _entry(h2, path)) != data:
                    raise ValueError(f"{'/'.join(path)} didn't read back")
            h1, s1 = _read_header(orig)
            probe = ("package.json",)
            if _read_entry(fh, s2, _entry(h2, probe)) != _read_entry(orig, s1, _entry(h1, probe)):
                raise ValueError("other files moved")
        if tmp.stat().st_size != s2 + offset:
            raise ValueError("unexpected size")

        if desktop_running():  # it was opened meanwhile
            tmp.unlink(missing_ok=True)
            return False, L("OpenCode Desktop was opened; trying again later.",
                            "Se ha abierto OpenCode Desktop; se volverá a intentar más tarde.")
        os.replace(tmp, asar)
        _log(f"updated {asar}: wordmark={wordmark} banner={banner} extras={extras}")
        return True, L("Done: OpenCode Desktop picks this up the next time it opens.",
                       "Hecho: OpenCode Desktop lo cogerá la próxima vez que se abra.")
    except Exception as exc:
        tmp.unlink(missing_ok=True)
        _log(f"update failed, left OpenCode as it was: {exc}")
        return False, L("Couldn't change OpenCode Desktop; it was left as it was.",
                        "No se pudo cambiar OpenCode Desktop; se ha quedado como estaba.")


def _nothing() -> str:
    return L("Nothing to change.", "No hay nada que cambiar.")


def apply() -> tuple[bool, str]:
    """Add the openhop.cfd wordmark (and keep the banner patch in line with its setting)."""
    ok, msg = _set_state(wordmark=True, banner=banner_setting_enabled(), extras=extras_setting_enabled())
    if ok and msg != _nothing():
        msg = L("Done: OpenCode Desktop shows openhop.cfd the next time it opens.",
                "Hecho: OpenCode Desktop mostrará openhop.cfd la próxima vez que se abra.")
    elif ok:
        msg = L("Already showing openhop.cfd.", "Ya muestra openhop.cfd.")
    return ok, msg


def restore() -> tuple[bool, str]:
    """Take the wordmark out (the banner patch stays if its setting is on)."""
    ok, msg = _set_state(wordmark=False, banner=banner_setting_enabled(), extras=extras_setting_enabled())
    if ok:
        return True, L("OpenCode shows its own wordmark again (from the next time it opens).",
                       "OpenCode vuelve a mostrar su propio nombre (a partir de la próxima vez que se abra).")
    return ok, msg


def restore_all() -> tuple[bool, str]:
    """Put OpenCode back exactly as it was (uninstall): the original from the backup if it's good, else unpatch."""
    d = desktop_dir()
    if not d:
        return True, L("OpenCode Desktop isn't installed.", "OpenCode Desktop no está instalado.")
    asar = d / "resources" / "app.asar"
    try:
        info = _inspect(asar)
        if not info["wordmark"] and not info["banner"] and not info["extras"]:
            return True, L("OpenCode is already as it was.", "OpenCode ya está como estaba.")
        if desktop_running():
            return False, L("OpenCode Desktop is open; it will be put back the next time it's closed.",
                            "OpenCode Desktop está abierto; se dejará como estaba la próxima vez que se cierre.")
        if BACKUP_ASAR.is_file():
            b = _inspect(BACKUP_ASAR)
            if not b["wordmark"] and not b["banner"] and not b["extras"]:
                tmp = asar.with_name("app.asar.openhop-restore")
                shutil.copy2(BACKUP_ASAR, tmp)
                os.replace(tmp, asar)
                _log(f"restored {asar} from backup")
                return True, L("OpenCode Desktop is back to how it was.", "OpenCode Desktop vuelve a estar como antes.")
    except Exception as exc:
        _log(f"restore from backup failed: {exc}")
    return _set_state(wordmark=False, banner=False, extras=False)


def sync() -> str:
    """Bring OpenCode in line with the settings. Cheap when nothing needs doing; safe to call often."""
    try:
        d = desktop_dir()
        if not d or desktop_running():
            return "skip"
        info = _inspect(d / "resources" / "app.asar")
        want_w, want_b, want_x = setting_enabled(), banner_setting_enabled(), extras_setting_enabled()
        w = want_w if want_w != info["wordmark"] and (not want_w or status().get("supported")) else None
        b = want_b if want_b != info["banner"] and (not want_b or info["banner_ok"]) else None
        x = want_x if (extras_current_version() if want_x else "") != info["extras"] else None
        if w is None and b is None and x is None:
            return "ok"
        ok, msg = _set_state(wordmark=w, banner=b, extras=x)
        return f"wordmark={w} banner={b} extras={x}: {msg}"
    except Exception as exc:
        _log(f"sync error: {exc}")
    return "ok"


def pending() -> bool:
    """True when OpenCode Desktop is open and some change openhop wants (wordmark, limit card, extras) is still
    waiting for it to close. Same checks as sync(), minus the writing. Used by the app's "restart OpenCode" notice."""
    try:
        d = desktop_dir()
        if not d or not desktop_running():
            return False
        if _asar_integrity_enabled(d / "OpenCode.exe") is not False:
            return False  # it would never be done, so a restart wouldn't help
        info = _inspect(d / "resources" / "app.asar")
        want_w, want_b, want_x = setting_enabled(), banner_setting_enabled(), extras_setting_enabled()
        supported = info["wordmark"] or (info["wordmark_ok"] and "new-session-wordmark" in _renderer_text_hint(d))
        if want_w != info["wordmark"] and (not want_w or supported):
            return True
        if want_b != info["banner"] and (not want_b or info["banner_ok"]):
            return True
        return (extras_current_version() if want_x else "") != info["extras"]
    except Exception:
        return False


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"
    if cmd == "apply":
        print(apply())
    elif cmd == "restore":
        print(restore())
    elif cmd == "restore-all":
        print(restore_all())
    elif cmd == "sync":
        print(sync())
    elif cmd == "css":
        print(style_block())
    else:
        print(status())

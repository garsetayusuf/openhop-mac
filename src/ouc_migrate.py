"""One-time move from "Opencode Unlimited" (before 3.0) to openhop.

Runs when the app starts, before the background workers are synced. It is safe to run more than once:
the marker file stops it after the first successful run, and every step is best effort (a step that
fails is logged and skipped, it never stops the app from opening).

What it does:
1. Stops the old background workers and deletes the old logon tasks (remembering whether they existed).
2. Copies the user's settings and state into %LOCALAPPDATA%\\openhop (never the old scripts or shims:
   the new ones are deployed fresh).
3. Takes the old 'bin' folder out of the user PATH (the caller puts the new one in).
4. Writes the marker and renames the old folder to 'opencode-unlimited.old' so nothing is lost.
"""

from __future__ import annotations

import ctypes
import json
import shutil
import time
from pathlib import Path

from ouc_common import (
    DEST_DIR,
    OLD_DEST_DIR,
    OLD_MUTEX_GUARD,
    OLD_MUTEX_KEEPER,
    OLD_TASK_GUARD,
    OLD_TASK_KEEPER,
    append_log,
    run_hidden,
)

MARKER = DEST_DIR / "migrated_from_ouc.txt"
OLD_BACKUP_DIR = OLD_DEST_DIR.with_name(OLD_DEST_DIR.name + ".old")
MIGRATE_LOG = DEST_DIR / "logs" / "migration.log"

# Never copied: code and shims are redeployed, the stamp forces that redeploy, the browser cache can be
# locked by a closing window, and a pause lock from the old keeper is meaningless now.
_SKIP = {"scripts", "bin", "worker_version.txt", "webview_cache", "keeper.pause"}


def needs_migration() -> bool:
    return OLD_DEST_DIR.is_dir() and not MARKER.is_file()


def _log(msg: str) -> None:
    append_log(MIGRATE_LOG, msg)


def _mutex_held(name: str) -> bool:
    kernel32 = ctypes.windll.kernel32
    kernel32.OpenMutexW.restype = ctypes.c_void_p
    handle = kernel32.OpenMutexW(0x00100000, False, f"Local\\{name}")
    if handle:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return True
    return False


def _task_exists(name: str) -> bool:
    try:
        return run_hidden(["schtasks", "/Query", "/TN", name], timeout=6.0).returncode == 0
    except Exception:
        return False


def _stop_old_workers() -> int:
    """Kill the old keeper/guard (python scripts under the old folder, or the old exe in --worker mode)."""
    import psutil

    old_dir = str(OLD_DEST_DIR).lower()
    killed = 0
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info.get("name") or "").lower()
            cmd = " ".join(proc.info.get("cmdline") or []).lower()
            is_old_script = "python" in name and old_dir in cmd and ("warp_keeper.py" in cmd or "zen_guard.py" in cmd)
            is_old_exe = "opencodeunlimited" in name and "--worker" in cmd
            if is_old_script or is_old_exe:
                proc.kill()
                killed += 1
        except Exception:
            pass
    for _ in range(40):
        if not (_mutex_held(OLD_MUTEX_KEEPER) or _mutex_held(OLD_MUTEX_GUARD)):
            break
        time.sleep(0.15)
    return killed


def _old_bin_in_user_path() -> bool:
    import winreg

    target = str(OLD_DEST_DIR / "bin").rstrip("\\").lower()
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment", 0, winreg.KEY_READ) as key:
            cur, _ = winreg.QueryValueEx(key, "Path")
        return any(p.rstrip("\\").lower() == target for p in str(cur).split(";") if p)
    except Exception:
        return False


def _copy_if_missing(src: str, dst: str) -> str:
    """Never overwrite: anything openhop already wrote in the new folder wins over the old copy."""
    if not Path(dst).exists():
        shutil.copy2(src, dst)
    return dst


def _copy_state() -> int:
    copied = 0
    DEST_DIR.mkdir(parents=True, exist_ok=True)
    for item in OLD_DEST_DIR.iterdir():
        if item.name.lower() in _SKIP:
            continue
        dest = DEST_DIR / item.name
        try:
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True, copy_function=_copy_if_missing)
            else:
                _copy_if_missing(str(item), str(dest))
            copied += 1
        except Exception as exc:
            _log(f"could not copy {item.name}: {exc}")
    return copied


def migrate() -> dict:
    """Run the migration. Returns what the caller still has to do once the new workers are deployed."""
    result = {"migrated": False, "had_tasks": False, "had_path": False}
    if not needs_migration():
        return result
    _log(f"migrating {OLD_DEST_DIR} -> {DEST_DIR}")

    # 1. Old workers and tasks
    had_tasks = False
    for task in (OLD_TASK_KEEPER, OLD_TASK_GUARD):
        if _task_exists(task):
            had_tasks = True
            run_hidden(["schtasks", "/End", "/TN", task], timeout=8.0)
            run_hidden(["schtasks", "/Delete", "/TN", task, "/F"], timeout=8.0)
    try:
        killed = _stop_old_workers()
        _log(f"old tasks found: {had_tasks}; old workers stopped: {killed}")
    except Exception as exc:
        _log(f"could not stop old workers: {exc}")

    # 2. Settings and state
    _log(f"copied {_copy_state()} items")

    # 3. PATH: drop the old bin (the caller adds the new one if the old one was there)
    had_path = _old_bin_in_user_path()

    # 4. Marker first, so a locked folder can't make this run again on every start
    try:
        MARKER.write_text(json.dumps({"from": str(OLD_DEST_DIR), "at": time.strftime("%Y-%m-%d %H:%M:%S"),
                                      "had_tasks": had_tasks, "had_path": had_path}), encoding="utf-8")
    except Exception as exc:
        _log(f"could not write marker: {exc}")
    try:
        if OLD_BACKUP_DIR.exists():
            shutil.rmtree(OLD_BACKUP_DIR, ignore_errors=True)
        OLD_DEST_DIR.rename(OLD_BACKUP_DIR)
        _log(f"old folder kept as {OLD_BACKUP_DIR}")
    except Exception as exc:
        _log(f"old folder left in place ({exc}); uninstall removes it")

    result.update(migrated=True, had_tasks=had_tasks, had_path=had_path)
    return result

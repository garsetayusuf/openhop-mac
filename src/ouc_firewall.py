"""Optional Windows Defender Firewall kill-switch rules for opencode-cli."""

from __future__ import annotations

import sys

from ouc_common import (
    FW_RULE_PREFIX,
    L,
    OLD_FW_RULE_PREFIX,
    find_opencode_cli,
    is_admin,
    run_hidden,
)

CLI_RULE_NAME = f"{FW_RULE_PREFIX}CLI"
OLD_CLI_RULE_NAME = f"{OLD_FW_RULE_PREFIX}CLI"   # made by Opencode Unlimited (before 3.0)
_PUBLIC_RANGES = (
    "0.0.0.0-9.255.255.255,"
    "11.0.0.0-126.255.255.255,"
    "128.0.0.0-172.15.255.255,"
    "172.32.0.0-192.167.255.255,"
    "192.169.0.0-255.255.255.255,"
    "2000::/3"
)


def _rule_exists(name: str) -> bool:
    try:
        res = run_hidden(["netsh", "advfirewall", "firewall", "show", "rule", f"name={name}"], timeout=6.0)
        return res.returncode == 0 and name in res.stdout
    except Exception:
        return False


def is_killswitch_installed() -> bool:
    """Return True if the leak-blocker firewall rule exists (new name, or the old pre-3.0 one)."""
    return _rule_exists(CLI_RULE_NAME) or _rule_exists(OLD_CLI_RULE_NAME)


def install_killswitch() -> tuple[bool, str]:
    """Install outbound non-loopback/non-LAN block rule for opencode-cli.exe."""
    if not is_admin():
        return False, L("The leak blocker needs administrator permission.",
                        "El bloqueo de fugas necesita permiso de administrador.")

    cli = find_opencode_cli()
    if not cli or not cli.lower().endswith(".exe"):
        return False, L("opencode-cli.exe wasn't found, so there's nothing to block.",
                        "No se encuentra opencode-cli.exe, así que no hay nada que bloquear.")

    remove_killswitch()
    res = run_hidden(
        [
            "netsh",
            "advfirewall",
            "firewall",
            "add",
            "rule",
            f"name={CLI_RULE_NAME}",
            "dir=out",
            "action=block",
            f"program={cli}",
            f"remoteip={_PUBLIC_RANGES}",
            "profile=any",
            "enable=yes",
            "description=openhop: force opencode-cli through the local openhop proxy",
        ],
        timeout=10.0,
    )
    if res.returncode == 0:
        return True, L("Leak blocker is on: the OpenCode CLI can't skip openhop anymore.",
                       "Bloqueo de fugas activado: la CLI de OpenCode ya no puede saltarse openhop.")
    return False, (res.stderr or res.stdout).strip() or "netsh failed"


def remove_killswitch() -> tuple[bool, str]:
    """Remove the leak-blocker firewall rules (new and old names)."""
    if not is_admin():
        return False, L("Removing the leak blocker needs administrator permission.",
                        "Quitar el bloqueo de fugas necesita permiso de administrador.")
    for name in (CLI_RULE_NAME, OLD_CLI_RULE_NAME):
        run_hidden(["netsh", "advfirewall", "firewall", "delete", "rule", f"name={name}"], timeout=8.0)
    return True, L("Leak blocker is off.", "Bloqueo de fugas desactivado.")


def main() -> int:
    action = sys.argv[1].lower() if len(sys.argv) > 1 else "status"
    if action == "install":
        ok, msg = install_killswitch()
    elif action == "remove":
        ok, msg = remove_killswitch()
    elif action == "status":
        ok = is_killswitch_installed()
        msg = "installed" if ok else "not installed"
    else:
        print("Usage: ouc_firewall.py [install|remove|status]", file=sys.stderr)
        return 2
    print(msg)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

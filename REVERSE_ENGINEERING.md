# Reverse engineering notes — openhop 3.8.0 (commit 5e8111d) to Mac

Target: MacBook Pro, M1 Pro, macOS 27, arm64.
Prepared in a Linux VM (x86_64, Ubuntu 24.04, Node v24.22.1,
Python 3.12.3). Nothing below was run on a Mac.

## 1. Upstream source verification

- Site: https://openhop.cfd/
- File downloaded: `https://openhop.cfd/download/openhop-source-5e8111d.zip`
  (the root path `/openhop-source-5e8111d.zip` returned 404; the
  working path is `/download/openhop-source-5e8111d.zip`.)
- Published SHA-256 on the site: `72bdcdc76d176056fd894186b76e91f7fe68fa5eea9900e0c311d81c97a5bf54`
- Measured SHA-256 after download: `72bdcdc76d176056fd894186b76e91f7fe68fa5eea9900e0c311d81c97a5bf54`
- Result: **MATCH**. Only then was the zip unpacked.
- The Windows binary `openhop.exe` (21.3 MB, unsigned PyInstaller)
  was **not** downloaded, opened, or run. Cloudflare WARP and OpenCode
  were **not** installed in the VM, and no live hopping was tested.
- Upstream is MIT licensed, based on Opencode Unlimited by Avatar
  (ShadowySphinX). Licence and attribution are kept in `LICENSE`.

## 2. REA (Reverse Engineer Anything) — install and verification

REA site: https://rea.tools/get-started/
REA is an MCP / CLI for reverse-engineering apps, websites, and binaries.
Its supported clients are Codex, Cursor, Windsurf, VS Code, Claude Code,
Claude Desktop, OpenCode, Gemini CLI, and others. **Muse is not in
REA's supported client list**, so `npx rea-agents@latest setup`
(interactive client setup) does not apply cleanly here.

What was done instead (the usable equivalent):

- Local install, pinned, **not** global:
  `npm install rea-agents@6.10.1` in `rea/` under this workspace.
  A global install would vanish on the next VM replacement; a local
  install lives with the project.
- Version verified: `rea-agents --version` prints `6.10.1`.
  Node in the VM: v24.22.1 (REA needs Node 22.23.4+, 24.22.1+, or 26+).
- Doctor: `./node_modules/.bin/rea-agents doctor --json`
  Result: `"healthy": false`, `"environment_healthy": false`.
  Node check ok (24.22.1), host ok (Ubuntu 24.04). Missing, as expected
  in this VM: Hopper, Ghidra (`GHIDRA_INSTALL_DIR` not set), IDA
  registration, jadx, Kuna, and the REA skill identity (no supported
  client to install it into). Doctor failing on those providers does
  **not** block static source inspection — it means native-binary
  decompilation providers are not available here.
- Real REA inspection: `rea-agents analyze <openhop source tree> --json`
  completed with exit 0. It inventoried and parsed the JavaScript /
  Electron-facing parts of the tree (4 files: `src/ui/index.html`,
  `src/ui/notify.html`, `src/ui/traymenu.html`,
  `tools/make_update_keys.mjs`), built the application graph, and
  sealed Evidence `ev_46bf1e3aa240f5790fad636975c097b305444d3b84d8d1d70b3f2728d5fb3abf`
  (saved in the workspace as `rea/rea-analysis-openhop.json`, 287 KB).
  The bulk of openhop is Python, not JavaScript, so REA's JS analysis
  only covers the UI shell; the Python logic below was mapped by
  reading the source as text (no upstream code was executed).
- `rea-agents inspect <directory>` does **not** work on a plain source
  directory (`target_unavailable` — it wants a file or an Apple bundle).
  Use `analyze` / `analyze-javascript-application` for directories.
- A reusable skill doc from this install is saved at
  `~/workspace/custom-skills/rea/SKILL.md` (outside this zip).

## 3. How openhop works (from the source)

1. **Watch.** `zen_guard.py` (1,574 lines) / `scripts/zen-guard.sh`
   poll OpenCode's log (`~/.local/share/opencode/log/opencode.log`
   on Linux) every 10 s for the free-usage-limit message.
2. **Hop.** On a hit, the guard deletes the current WARP registration
   and registers a fresh one (`warp-cli registration delete` /
   `registration new`), which gives a fresh IPv6 /64 — a new quota
   bucket on OpenCode's side. Rules in the guard: a used-up /64 is
   skipped until the daily reset (~01:05 UTC), not forever; if new
   addresses keep hitting the limit within 3 minutes, hunt immediately;
   after 15 in a row, pause 3 minutes and try Tor.
3. **Continue.** The guard clears the limit lock (including rows in
   OpenCode Desktop's `drafts.sqlite`) and sends "continue" through
   the real `opencode` binary, over WARP, to every chat whose last
   message is the limit error. Chats already stuck when the guard
   starts are rescued too.
4. **Lane.** `scripts/zen-lane.sh` is the per-launch trick: it picks a
   random Cloudflare edge from the live AAAA pool for `opencode.ai`,
   bind-mounts a fake `/etc/hosts` and `/etc/machine-id` inside a
   Linux mount namespace (`unshare -rm`), and sets `HTTPS_PROXY` /
   `HTTP_PROXY` to WARP's local proxy (`http://127.0.0.1:40000`,
   or the openhop proxy on `127.0.0.1:41987` if it is running).
   Every launch is a fresh edge + fresh machine-id + fresh proxy path.
5. **Keeper.** `scripts/warp-keeper.sh` (run as a root systemd service)
   ticks every 45 s: keeps WARP in proxy mode on port 40000 (or TUN
   mode), reconnects if the tunnel died, and installs an iptables
   leak-guard chain (`OUC_LEAKGUARD`) in TUN mode, whitelisting SSH.
6. **Proxy.** `src/zen_proxy.py` is the local proxy on
   `127.0.0.1:41987` that only OpenCode is pointed at; pinned hosts
   are `*.opencode.ai` / `*.opencode.com`. Everything else stays on
   the normal connection.
7. **Desktop patch.** `src/desktop_brand.py` / `src/opencode_extras.py`
   patch OpenCode Desktop's `app.asar`: stop the free-limit card hiding
   the chat box, add the openhop.cfd mark, voice typing, per-project
   icons. A backup is kept and uninstall restores it.
8. **GUI.** `src/openhop_app.py` (4,478 lines) is the control-centre
   window (pywebview + WebView2 on Windows, `src/ui/index.html`),
   `src/openhop_tray.py` (857 lines) is the tray icon, `src/doctor.py`
   is the health check. `src/updater.py` checks openhop.cfd for new
   versions every few hours.
9. **Other routes.** `src/tor_manager.py`, `src/proxy_chain.py`,
   `src/wg_manager.py` (WireGuard / wireproxy), `src/clean_endpoint.py`
   — Tor, custom proxies, and clean endpoints when WARP addresses run
   out. `src/rank.py` is an opt-in leaderboard. `src/report_bundle.py`
   bundles logs for support (never chats).

## 4. Linux / Windows pieces and their macOS equivalents

| Upstream piece | macOS equivalent in this port |
|---|---|
| systemd system unit `opencode-unlimited-keeper.service` (`/etc/systemd/system/`) | `launchd/com.openhop.keeper.plist`, `~/Library/LaunchAgents/`, loaded with `launchctl bootstrap gui/$UID` |
| systemd user unit `opencode-unlimited-guard.service` (`~/.config/systemd/user/`) | `launchd/com.openhop.guard.plist`, same folder |
| `systemctl is-active / start / stop / enable` in `install.sh`, `scripts/*.sh`, `src/ouc_linux.py` | `launchctl list / bootstrap / bootout` in `scripts/*-macos.sh` and `src/ouc_macos.py` |
| `warp-cli` via apt/dnf/pacman, `/usr/bin/warp-cli`, sudoers NOPASSWD rule for it | `warp-cli` on PATH from the macOS WARP app (`/Applications/Cloudflare WARP.app`, daemon `.../Contents/Resources/CloudflareWARP`, launchd daemon `com.cloudflare.1dot1dot1dot1.macos.warp.daemon.plist`); no sudoers rule — macOS `warp-cli` does not need it for these commands |
| `iptables` / `ip6tables` `OUC_LEAKGUARD` chain in `warp-keeper.sh`, `src/ouc_firewall.py` | **Removed.** No pf kill-switch is installed. Proxy mode fails closed: if the proxy on port 40000 is down, OpenCode fails to connect instead of leaking the real IP. A pf-based kill-switch was not built — it needs a root LaunchDaemon and `pfctl` anchor management, and was out of scope for a source port |
| `unshare -rm` mount namespace, bind-mount `/etc/hosts` + `/etc/machine-id` in `zen-lane.sh` | **No macOS equivalent.** The Mac lane keeps the proxy env vars only. Edge-pinning and per-launch machine-id are dropped (see README section 9) |
| `/dev/net/tun` check in `install.sh` | Dropped — macOS WARP uses its own daemon / NetworkExtension, not `/dev/net/tun` |
| `~/.local/share/opencode-unlimited`, `~/.config/ai.opencode.desktop` | `~/Library/Application Support/openhop/`, `~/Library/Application Support/ai.opencode.desktop/` |
| OpenCode Desktop `%LOCALAPPDATA%\Programs\ai.opencode.desktop`, `C:\Program Files\OpenCode` | `/Applications/OpenCode.app`, `~/Applications/OpenCode.app`, binary `/Applications/OpenCode.app/Contents/MacOS/OpenCode`, bundle id `ai.opencode.desktop` (verified from OpenCode / Homebrew cask docs) |
| OpenCode CLI `~/.opencode/bin/opencode` (Linux) / `%APPDATA%\ai.opencode.desktop\cli` (Windows) | `~/.opencode/bin/opencode` on macOS (same as Linux) |
| `drafts.sqlite` at `%APPDATA%\ai.opencode.desktop\drafts.sqlite` / `~/.config/ai.opencode.desktop/drafts.sqlite` | `~/Library/Application Support/ai.opencode.desktop/drafts.sqlite` — **to verify on a real Mac**; the Linux paths are kept as fallbacks |
| WebView2 (Windows) | WKWebView via `pywebview` on macOS |
| System tray via `pystray` + Windows Task Scheduler | Menu-bar icon via `pystray` on macOS; autostart via launchd, not Task Scheduler |
| `pythonnet`, `clr_loader` in `requirements.txt` | **Dropped** in `requirements-macos.txt` — Windows-only (.NET interop). macOS deps: `pywebview`, `psutil`, `pystray`, `Pillow`, plus PyInstaller only for `build-app.sh` |
| PyInstaller `.exe` (`build.ps1`) | PyInstaller `--target-architecture arm64` `.app` (`build-app.sh`, run by the user on his Mac) |
| Homebrew / package manager | Homebrew at `/opt/homebrew` (Apple Silicon), `brew install --cask cloudflare-warp opencode-desktop` |

## 5. Files changed / added for macOS

- **Added:** `src/ouc_macos.py` (launchd helpers, macOS WARP discovery,
  macOS OpenCode paths, `~/Library/Application Support` state dir),
  `scripts/warp-keeper-macos.sh`, `scripts/zen-guard-macos.sh`,
  `scripts/zen-hop-macos.sh`, `scripts/zen-lane-macos.sh`,
  `scripts/doctor-macos.sh`, `launchd/com.openhop.keeper.plist`,
  `launchd/com.openhop.guard.plist`, `install-macos.sh`,
  `uninstall-macos.sh`, `build-app.sh`, `requirements-macos.txt`.
- **Patched (text edits only):** `src/ouc_common.py` (macOS state-dir
  and OpenCode Desktop/CLI candidates added; Windows/Linux branches
  kept), `src/openhop_app.py` (startup-log path gains a
  `~/Library/Application Support/openhop/logs` candidate),
  `src/auto_resume.py` (macOS `drafts.sqlite` candidate added),
  `src/zen_guard.py` (macOS log/DB candidates noted — the guard shell
  script is the Mac version; the Python guardlib still uses the Linux
  state path as a fallback, see below).
- **Kept as-is:** the rest of `src/*.py` (proxy, Tor, WireGuard,
  ranking, updater, chat handoff, explore, notices, report bundle).
  They are largely platform-agnostic Python; the platform-specific
  parts they call go through `ouc_common` / `ouc_linux` /
  `ouc_macos`.
- **Kept, Linux-only, for reference:** `src/ouc_linux.py`,
  `scripts/warp-keeper.sh`, `scripts/zen-guard.sh`,
  `scripts/zen-hop.sh`, `scripts/zen-lane.sh`, `scripts/doctor.sh`,
  `install.sh`, `uninstall.sh`. Do **not** run these on a Mac.
- **Dropped:** Windows-only `install.bat`, `install.ps1`,
  `uninstall.bat`, `uninstall.ps1`, `build.ps1`,
  `tools/build_bootloader.ps1`.

## 6. Known gaps — read before running

1. The guard's Python entry point is `src/zen_guard.py` (`main()`,
   no CLI arguments — it reads its paths from `src/ouc_common.py`).
   This port made `ouc_common.py` darwin-aware, so on a Mac the state
   goes under `~/Library/Application Support/openhop/` and the OpenCode
   log under `~/Library/Application Support/opencode/log/`. That wiring
   has **not** been run on a Mac: if the guard never hops, check the
   guard log first, then confirm `zen_guard.py` is actually finding
   the OpenCode log and DB at those macOS paths — the DB path
   (`OPENCODE_DATA_DIR`) is still the Linux
   `~/.local/share/opencode/` path in `ouc_common.py` and may need
   the same darwin treatment on first run.
2. `src/openhop_app.py` still imports `ouc_linux` in places and
   references `ouc_firewall` (iptables). On macOS those imports must
   resolve to the macOS stubs (`ouc_macos.py` provides
   `is_killswitch_installed()` returning False with a reason). If the
   GUI crashes on import on the Mac, that import is the first place
   to look.
3. The `app.asar` patch (`desktop_brand.py`) looks for Windows/Linux
   OpenCode install folders first; the macOS candidate
   (`/Applications/OpenCode.app/Contents/Resources/app.asar`) was
   added, but patching a signed macOS app will break its signature —
   OpenCode may refuse to launch or macOS may quarantine it. Test
   with a backup, and be ready to turn the desktop patch off
   (Advanced → openhop app in the GUI).
4. WARP on macOS in proxy mode: the Linux installer sets
   `warp-cli mode proxy` + `proxy port 40000`. Whether the macOS
   `warp-cli` accepts the same `mode proxy` / `proxy port` commands
   was **not** verified on macOS 27 — the macOS WARP app is GUI-first
   and its CLI surface differs from Linux in places. The installer
   checks the result and reports it; if `mode proxy` is rejected on
   macOS 27, the fallback is WARP in full-tunnel mode (whole PC
   through WARP), which the README flags at install time.
5. macOS 27 itself: no macOS-27-specific behaviour was verified
   anywhere in this port — no Mac was available. Homebrew, WARP,
   OpenCode, Python 3.12, PyInstaller, launchd, and Gatekeeper
   behaviour are all as documented for macOS 13+ / Tahoe-era releases.
   If macOS 27 changed any of those, the doctor output is the
   diagnostic to work from.

## 7. Terms-of-service warning

openhop's stated purpose is to rotate the IP address OpenCode sees
so its free-tier limit does not stop a chat. That is, in plain terms,
evading a usage limit. It may violate OpenCode's terms of service and
may lead to the account being limited or banned. This port keeps that
warning in the README and does not hide it. Use at your own risk.

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="assets/brand/openhop-wordmark-dark.svg">
    <img src="assets/brand/openhop-wordmark.svg" alt="openhop" width="340">
  </picture>
</p>

<p align="center">
  <b>Keep OpenCode working when you hit the free limit — on your Mac.</b><br>
  This is the native macOS control center for openhop: it gives OpenCode a fresh internet address the moment the limit appears and tells it to continue, so your chat keeps going.
</p>

<p align="center">
  <a href="https://github.com/garsetayusuf/openhop-mac/releases"><img alt="Download for macOS" src="https://img.shields.io/badge/Download-macOS%20(DMG)-f2b318?style=for-the-badge&labelColor=1f2125"></a>
  <a href="https://openhop.cfd"><img alt="Website: openhop.cfd" src="https://img.shields.io/badge/Web-openhop.cfd-efede6?style=for-the-badge&labelColor=1f2125"></a>
  <a href="LICENSE"><img alt="MIT License" src="https://img.shields.io/badge/License-MIT-efede6?style=for-the-badge&labelColor=1f2125"></a>
</p>

---

## What it does

[OpenCode](https://opencode.ai/) has a free tier with a usage limit, counted per internet address. When you reach it, OpenCode stops answering and shows a "free usage limit" message.

OpenHop for Mac runs quietly next to OpenCode and:

1. **Watches** OpenCode's log for the limit message.
2. **Hops** to a fresh address through Cloudflare's free WARP app, in about 10 seconds.
3. **Continues** your chat: it clears the limit banner, sends `continue` for you and checks that it arrived.

Only OpenCode is routed. Your browser, games and calls stay on your normal internet and are never interrupted when the address changes. If the proxy is down, OpenCode fails closed — it never leaks your real IP. Your chats and history are never changed or uploaded.

> **Warning:** this works around a free-tier usage limit and may violate OpenCode's terms. It is independent and unofficial — not affiliated with OpenCode or Cloudflare. Use at your own risk.

## What you need

| | |
|---|---|
| **Apple Silicon Mac** (M1–M4) | macOS 13 Ventura or newer. Intel Macs are not covered by this arm64 build. |
| **OpenCode** | The CLI or Desktop app, opened once. [Get OpenCode](https://opencode.ai/) |
| **Cloudflare WARP** (free) | The "1.1.1.1" app. Open it once and accept its terms; no account needed. [Get WARP](https://one.one.one.one/) |
| **Python 3 and curl** | Only for building from source. The installer creates its own venv. |

No sudo, no `/etc/hosts` edits, no system firewall rules — by design.

## Install

1. Download **`OpenHop.dmg`** from the [latest release](https://github.com/garsetayusuf/openhop-mac/releases).
2. Open the disk image and **drag `OpenHop.app` into Applications** — the same as any other Mac app.
3. Open OpenHop from Applications. The first launch shows the **setup gate**; on an ad-hoc signed build, right-click → Open the first time.
4. The gate checks four things: **OpenCode**, **WARP**, **App** and **Runtime**. Anything missing gets its own **Install** button — press it and the gate re-checks that row.
5. Press **Continue** once all four are green. The setup gate appears only once; after that the app opens straight to the control center.

Building from source instead:

```bash
git clone https://github.com/garsetayusuf/openhop-mac.git
cd openhop-mac

bash install-macos.sh     # venv, deps, src/scripts, launchd jobs, opencode shim
bash build-app.sh         # compiles the Swift app, ad-hoc signs, installs to /Applications
bash build-dmg.sh         # creates dist/OpenHop.dmg (drag-to-install)

open -a OpenHop
```

## Everyday use

OpenHop lives in the **menu bar**. The dot on its icon tells you how things are:

| Dot | Meaning |
|---|---|
| 🟢 Green | Everything is working. |
| 🟡 Amber | Working, but something needs a look (or OpenHop is switching address). |
| 🔴 Red | OpenCode can't work right now. Open the app and press **Fix** in Diagnostics. |

**Click the icon** to open the window. The menu offers **Open OpenHop**, **Connect / Disconnect**, **Rotate IP**, **Auto-rotate**, **Background** and **Quit OpenHop**.

During setup the menu only shows **Open OpenHop** — the control actions appear after you press Continue.

### The window

- **Dashboard** — current state, IP, connection, proxy port, and the Start / Stop / Get New IP buttons.
- **Activity** — what happened, with timestamps and results.
- **Diagnostics** — health checks, **Run Doctor**, and **Fix** (full recovery).
- **Services** — restart keeper/guard, launch OpenCode through the protected lane.
- **Usage** — local-only counts: tokens, messages, chats, rotations, used-up addresses, limits detected and continues sent. Nothing is uploaded.
- **Settings** — connection route (WARP or a custom SOCKS5/HTTP proxy), clean WARP endpoints for restricted networks, start at login, notifications, and the version button.

`Cmd+Q` follows **Settings → Stay active after Cmd+Q**: ON hides the window and keeps Keeper, Guard and WARP running; OFF stops everything and quits. **Quit OpenHop** in the menu bar always stops everything and exits. The green traffic-light button enters full screen; `Esc` exits.

### Commands

| Command | Purpose |
|---|---|
| `opencode` | OpenCode routed through OpenHop |
| `openhop` | Open the control center |
| `openhop doctor` | Full health check |
| `openhop hop` | Fresh IP now |
| `openhop status` | Tunnel, jobs and egress summary |
| `openhop start` / `openhop stop` | Connect / disconnect from the CLI |
| `openhop quit` | Really quit the app |

Add `~/.local/bin` to `PATH` if the installer says it is missing.

## Something isn't working?

Press **Fix** in Diagnostics first — it solves most problems. If it doesn't, find your message below.

<details>
<summary><b>OpenHop won't open, or shows a Gatekeeper warning</b></summary>

It is ad-hoc signed. Right-click `OpenHop.app` → **Open**, then **Open** again in the dialog. If the menu bar icon is hidden by macOS, OpenHop switches to showing a Dock icon and posts a notification; you can re-enable it in System Settings → Control Center.
</details>

<details>
<summary><b>The setup gate says MISSING</b></summary>

Press the **Install** button on that row, then wait for the re-check to finish. OpenCode needs to have been opened once; WARP needs to have been opened once to accept its terms. Runtime installs the venv, CLI shims and launchd jobs from the copy embedded inside the app.
</details>

<details>
<summary><b>OpenCode says "Unable to connect" / <code>ECONNREFUSED</code></b></summary>

The background helpers aren't running. Open OpenHop and press **Fix** under Diagnostics — it usually takes a couple of seconds.
</details>

<details>
<summary><b>WARP keeps "Connecting…" or shows <code>CF_HAPPY_EYEBALLS_MITM_FAILURE</code></b></summary>

Your network is blocking or slowing Cloudflare. Press **Fix**. If it keeps happening, go to Settings → **WARP endpoint** and apply one of the clean endpoints, or use a custom proxy.
</details>

<details>
<summary><b>The "free usage limit" banner is still there</b></summary>

Wait about 10 seconds — OpenHop needs a moment to switch address and send `continue`. You can also press **Get New IP**, or type `continue` yourself.
</details>

<details>
<summary><b>Sidebar feels slow or buttons don't respond</b></summary>

Wait for the current operation to finish; controls are disabled while a job is running. If it stays stuck, press **Fix** and reopen the window.
</details>

<details>
<summary><b>Still stuck?</b></summary>

Open Diagnostics and press **Run Doctor**, then **Fix**. Logs are local: `~/Library/Application Support/openhop/logs` and the command audit at `commands.jsonl` in the same folder. Screenshots of the Diagnostics panel help most.
</details>

## What OpenHop changes on your Mac

Everything runs locally. To do its job, OpenHop:

- switches Cloudflare WARP into local-proxy mode (`127.0.0.1:40000`) so only OpenCode's traffic goes through it;
- points the `opencode` shim at OpenHop's own local proxy (`127.0.0.1:41987`), restoring the stock binary on uninstall;
- runs two user launch agents — one keeps the connection healthy (`keeper`), one watches for the limit (`guard`);
- clears OpenCode Desktop's local free-limit lock after switching address, without touching chat history;
- keeps usage counters on this Mac; nothing is uploaded;
- checks GitHub Releases for a newer version (see Updates).

No sudo, no system-wide changes, no `/etc/hosts`, no `pf`.

**Uninstall:** `bash uninstall-macos.sh` from the source folder, then delete `/Applications/OpenHop.app`. That script stops the jobs and removes `~/Library/Application Support/openhop`, which includes usage history and the venv — back up that folder first if you want to keep the counts. OpenCode sessions and WARP are left alone.

## For advanced users

<details>
<summary><b>How it works</b></summary>

OpenCode's free limit is counted per internet address (an IPv6 `/64` block on WARP). OpenHop routes OpenCode through local proxies and out through WARP. When the limit appears in OpenCode's log, it asks WARP for a new address and sends `continue`.

```
OpenCode CLI / Desktop
        │  HTTPS via local proxy 127.0.0.1:41987
        ▼
   openhop keeper
        │  SOCKS5 127.0.0.1:40000
        ▼
   Cloudflare WARP ──▶ fresh IPv6 /64
        ▲
   openhop guard  (watches log for the free-limit message)
```

</details>

<details>
<summary><b>Repository layout</b></summary>

| Path | What it is |
|---|---|
| `macos/` | SwiftUI + AppKit control center (app shell, model, lifecycle coordinator) |
| `src/` | Python upstream port: guard, keeper, lane, proxy, doctor, helpers |
| `scripts/` | macOS launchd scripts (guard, keeper, hop, lane, doctor) |
| `launchd/` | keeper/guard plist templates |
| `build-app.sh` / `build-dmg.sh` | Build the app bundle and the drag-to-install DMG |
| `install-macos.sh` / `uninstall-macos.sh` | Runtime install and clean removal |
| `assets/brand/`, `assets/dmg/` | App icon, wordmark, DMG background |
| `setup/` | Source for the guided installer (legacy) |
| `REVERSE_ENGINEERING.md` | Reverse-engineering notes |

</details>

<details>
<summary><b>Reset the first-run gate</b></summary>

The setup gate is intentionally shown once. To see it again:

```bash
defaults delete com.openhop.controlcenter openhop.setupCompleted
```

Then relaunch OpenHop.

</details>

## Updates

The version button in **Settings → About** shows the installed version and checks GitHub Releases; a daily background check also adds a badge and notification when a newer version is available. **Download** fetches the DMG, then **Install & Restart** applies it (or **Cancel** to keep the current version). Nothing is uploaded.

Publish from source with:

```bash
bash release.sh 3.8.2 "notes"
```

This bumps `macos/Info.plist`, builds `dist/OpenHop.dmg`, commits and tags `v3.8.2`, pushes, and attaches the DMG to a GitHub release.

### Releases

Every merge to `main` automatically builds and publishes a signed release: it bumps the patch version, builds the DMG, signs a `sha256` manifest, and attaches both files to a new GitHub release. The signature must match the public key embedded in `macos/UpdateManager.swift`; configure the matching private key as a repository secret named `UPDATE_SIGNING_KEY`:

```bash
gh secret set UPDATE_SIGNING_KEY -R garsetayusuf/openhop-mac -b "$(base64 < "$HOME/Library/Application Support/openhop/signing/private.pem")"
```

---

## Disclaimer

This is an independent, unofficial macOS port based on [openhop](https://openhop.cfd) by Avatar (ShadowySphinX), released under the MIT licence. It is not affiliated with or endorsed by OpenCode or Cloudflare. It works around a free-tier limit; use it at your own risk and follow the terms of the services you use. See [LICENSE](LICENSE).

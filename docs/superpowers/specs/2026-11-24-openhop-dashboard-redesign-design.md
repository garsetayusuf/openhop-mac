# OpenHop Mac dashboard — full redesign spec

Date: 2026-11-24
Status: design approved with additions (pending written-spec review)

## Goal

Replace the current AppKit/SwiftUI dashboard with one clean, native macOS control centre that looks like a port of openhop (official palette), keeps the existing working backend untouched, and provides a real in-app command console. No web redirects, no Terminal.app, no sudo, no /etc/hosts, no pf.

## Framework

SwiftUI only, compiled into the existing `MacOS/OpenHop` binary by `build-app.sh`. SwiftUI materials provide glassmorphism; `NavigationSplitView` provides the sidebar/detail layout and adapts to window resizing. No third-party dependencies.

## Official palette (from shipped `src/ui/index.html`)

- background `#151619`, deep `#101113`, surface `#1d1f23`
- text `#ecebe4`, muted `#a8a59b`, dim `#7a776f`
- brand amber `#f2b318`, amber hi `#ffc535`
- healthy `#3fb97a`, warning `#f07d3a`, error `#e5534b`

Use these tokens consistently; system accent is not used for state colours. Dark appearance only, matching the official app.

## Layout

Window: default 1180×700, minimum 940×560, native traffic lights.

Left sidebar (source-list style) with four sections; top toolbar holds the openhop icon, status chip and one prominent **Stop / Resume protection** control. Menu-bar status item must be present and visible, using the openhop icon as a template image, with a local-only menu (Open Dashboard, Run Doctor, Get new IP, Fix everything, Quit).

Detail panes:

1. **Dashboard** — one health card (`Everything is working` / `Degraded` / `Not connected`), current IP, connection, proxy port, and three actions: Launch OpenCode, Get new IP, Fix everything.
2. **Console** — command history list + detail output pane. Search, replay, copy, save, clear, plus an approved local command runner (`doctor`, `hop`, `lane`, `warp status`) with arguments.
3. **Diagnostics** — doctor output and keeper/guard status with one Restart button each.
4. **Protection** — read-only status of keeper, guard and WARP with explanation; the only Stop/Resume control lives in the toolbar to avoid duplicates.

## Behaviour changes requested

- **Get new IP** performs hop, then automatically runs the same fix routine as Fix everything. Standalone Fix everything button remains.
- **Stop protection** (toolbar) boots out both launchd jobs and disconnects WARP (`warp-cli disconnect`). **Resume protection** bootstraps both jobs and reconnects WARP (`warp-cli connect`). Status chip and protection pane reflect the stopped state.
- **Tor / custom proxy routes** appear only as a clearly disabled “not available on Mac port” roadmap row. No fake controls.
- Extra local tools: WARP control (connect/disconnect/status, proxy port), Test egress, Copy IP, Export support bundle (local zip of command history + doctor output), command runner.

## Architecture / data flow

One `DashboardModel` (ObservableObject). All backend work goes through a single `run(command:args:timeout:)` helper using argument arrays, no shell. Each run creates a `CommandRecord`: id, timestamp, command, args, exit code, duration, stdout, stderr, outcome. Records are held in memory and appended to `~/Library/Application Support/openhop/logs/commands.jsonl`. Views never call `Process` directly. `DoctorResult.parse` reads `✓/!/✗` prefixes; doctor exit status is ignored. Timeouts and nonzero exits are recorded as failures; the UI never blocks.

Backend paths and semantics are unchanged: `~/.local/bin/opencode` (lane shim), `~/.local/bin/openhop-hop`, `~/.local/bin/openhop-doctor`, `warp-cli`, `launchctl bootout/bootstrap/kickstart` in `gui/$UID`.

## Error handling and safety

- No action opens a website or Terminal window.
- No sudo, no hosts file, no pf/iptables. Proxy mode fails closed.
- Pausing protection is reversible; Resume restores jobs and WARP.
- Unexpected command errors are captured in the console record, not shown as modal alerts.

## Verification

1. Clean `bash build-app.sh`, install to `/Applications/OpenHop.app`.
2. Launch binary from Terminal; capture window screenshot; confirm menu-bar icon is visible.
3. Click each sidebar section and each action; confirm hop changes IP and triggers auto-fix; confirm Stop disconnects WARP and Resume restores it.
4. Run `openhop-doctor` — all green.
5. Run `bash uninstall-macos.sh`, then `bash install-macos.sh` — rollback works.

## Out of scope

Real Tor/WireGuard/custom-proxy backends, light mode, Windows/Linux parity, interactive OpenCode CLI TTY session.

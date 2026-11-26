# REA Lifecycle Analysis — OpenHop Mac

Date: 2026-11-24
Scope: `~/openhop-mac` — Swift dashboard, launchd agents, shell scripts, WARP CLI, OpenCode lane shim.

## 1. REA investigation record and limits

REA skill invoked. Installed CLI/MCP version is **6.10.1** (the requested "v63" is not installed on this Mac). `rea doctor --json` reports `healthy:false`, `environment_healthy:false` because no native analysis engine is configured.

REA evidence produced:

| Target | Evidence ID | Result |
|---|---|---|
| Upstream source ZIP `openhop-source-5e8111d.zip` | `ev_29501ec268ad3fc526a5bd8631226365744654a0c975a0259710d10df69dde3c` | SHA-256 `72bdcd…bf54`; ZIP graph 91 nodes / 91 occurrences / 90 edges |
| Local port ZIP `openhop-mac-source.zip` | `ev_2f3246d7f48eedd01139d82078a4117042e1abb1edcc82c6d3b1491bb6a39197` | SHA-256 `dab6e2…6cf`; 81 nodes / 81 occurrences / 80 edges |
| `src/ui` HTML/JS graph | `ev_bfd6e3faa449c181e1db4a73910ebfc748e547b29788751815717511f2d4958b` (parent `ev_5a909e2b455e19cd71c1689e547948fe6d06a8ad4b206466efac005b665a337c`) | 4 files, 606,486 bytes, 0 parsed JS modules — inline JS not parsed |

Limits, stated explicitly:

- REA has **no Swift or shell source parser**. It cannot produce a call graph for `ControlCenter.swift`, `DashboardModel.swift`, or the `*-macos.sh` scripts. Those call chains below were established by direct source reading with file:line citations, not by REA graph traversal.
- `open_binary` on `/Applications/OpenHop.app/Contents/MacOS/OpenHop` failed with `provider_unavailable`: Ghidra not configured, Hopper launcher missing, IDA not configured, Kuna not configured. Hopper was optional and did not block source analysis. No decompilation was performed.
- No runtime process trace was produced by REA. System state was observed with `launchctl`, `warp-cli status`, and the doctor script.

## 2. Actual call chains (source-traced)

### UI button → shell → system effect

| Control | Call chain | Shell command | System effect |
|---|---|---|---|
| Get new IP | `DashboardView` button → `DashboardModel.hop(autoFix:true)` (`DashboardModel.swift:133`) | `runAsync(hopPath)` → `~/.local/bin/openhop-hop` → symlink to `scripts/zen-hop-macos.sh` | `warp-cli disconnect`, `registration delete`, `registration new`, `mode proxy`, `proxy port 40000`, `connect`, clears drafts.sqlite keys |
| …then auto-fix | `hop` completion → `fix()` (`:137`, `:142`) | doctor → `ensureJobs` → doctor | `launchctl print/bootstrap` for keeper+guard |
| Fix everything | UI button → `fix()` | same as above | same |
| Stop | toolbar button → `stopProtection()` (`:169`) | `launchctl bootout` ×2; `warp-cli disconnect` (+ retry); `warp-cli status` | jobs unloaded, WARP disconnected |
| Resume / Start | toolbar button → `resumeProtection()` (`:183`) | `launchctl bootstrap` ×2 (async); `warp-cli connect` (async) | jobs loaded, WARP connected |
| Launch OpenCode | UI button → `launch()` (`:129`) | `~/.local/bin/opencode` → `zen-lane-macos.sh` → real `opencode` with `HTTPS_PROXY=http://127.0.0.1:40000` | OpenCode started through WARP proxy |
| Run Doctor | UI/menu → `doctor()` (`:97`) | `~/.local/bin/openhop-doctor` → `scripts/doctor-macos.sh` | checks jobs, WARP status, egress via proxy, lane config |
| Restart keeper/guard | Diagnostics button → `restartJob()` (`:195`) | `launchctl kickstart -k gui/<uid>/com.openhop.<job>` | restarts one agent |
| Console runner | `commandRunner()` (`:224`) | `doctor` / `hop` / `lane` / `warp status|connect|disconnect` | duplicate entry points for the same lifecycle operations |

### Service lifecycle

- Keeper: `launchd/com.openhop.keeper.plist` runs `scripts/warp-keeper-macos.sh`; `RunAtLoad` + `KeepAlive` true; loop checks `warp-cli status` and runs `mode proxy`, `proxy port 40000`, `connect` every 45 s if disconnected (`warp-keeper-macos.sh:15-20`).
- Guard: `com.openhop.guard.plist` → `zen-guard-macos.sh:23-25` → `venv/bin/python zen_guard.py`; `RunAtLoad` + `KeepAlive` true.
- Proxy: there is **no separate OpenHop proxy process on macOS**. The proxy is WARP's local HTTP/SOCKS endpoint on `127.0.0.1:40000`; the lane shim sets proxy env vars to it (`zen-lane-macos.sh:19-25`) and fails closed if it is down. Doctor verifies it by `curl -x http://127.0.0.1:40000 https://cloudflare.com/cdn-cgi/trace` (`doctor-macos.sh:20-25`).

## 3. Confirmed bugs

**B1 — Get New IP always resurrects keeper, violating stopped-state preservation.** `scripts/zen-hop-macos.sh:14-15` installs `trap restore EXIT`, and `restore()` runs `launchctl kickstart -k gui/$UID/com.openhop.keeper` on **every** exit, including failure. A stopped keeper is therefore started by an IP switch. Confirmed by source and by an observed run where hop's EXIT trap restarted keeper.

**B2 — Auto-fix after hop silently bootstraps stopped services.** `DashboardModel.swift:133-150`: `hop(autoFix:true)` calls `fix()`; `fix()` calls `ensureJobs()` (`:153-165`), which bootstraps keeper/guard whenever `launchctl print` fails. If OpenHop was stopped, Get New IP starts it. Confirmed.

**B3 — No Start implementation with postcondition checks.** There is no `ensureHealthy()`/`start()`. `resumeProtection()` (`:183-189`) fires bootstrap and WARP connect concurrently with `runAsync`, no ordering, no validation of executables/config, no proxy listener check, no lane config check, no final verification. It reports success without checking results. Confirmed.

**B4 — Stop is not complete and can leave a green health chip.** `stopProtection()` (`:169-181`) boots out keeper/guard and disconnects WARP, but never calls `applyDoctor()` or recomputes `health`; the UI keeps showing the previous `EVERYTHING IS WORKING` until a later doctor run. It does not identify or stop other OpenHop-managed processes (e.g. an in-flight `zen_guard.py`/hop process), does not verify the proxy listener, and does not distinguish stopped / partially stopped / failed. It also blocks the UI thread because `run()` is called synchronously. Confirmed.

**B5 — Shared `running: Process` races under concurrent operations.** `run()` sets `running = p` (`:54`) and clears it (`:63`) without a lock. `stopCurrent()` (`:79-82`) can terminate only the last process. Doctor was deliberately made concurrent (`:97-103`), so Stop/Start/Switch can overlap with it and cancellation targets the wrong process. Confirmed code-level race.

**B6 — Duplicate lifecycle entry points.** `fix()`, `resumeProtection()`, `stopProtection()`, `warpControl()`, `restartJob()`, and `commandRunner()` all expose start/stop/repair behaviour. The UI had Fix Everything plus the toolbar Stop/Resume plus console commands. Confirmed by grep of `DashboardModel.swift` and `ControlCenter.swift`.

**B7 — IP switch does not reload dependants.** `zen-hop-macos.sh` changes WARP registration and clears `drafts.sqlite` keys, but never restarts keeper/guard, never refreshes the lane shim's environment or any cached connection state, and never re-runs doctor itself. The model's `hop()` runs doctor only for display. Confirmed.

**B8 — WARP CLI "connected" is not the same as working connectivity.** `warp-cli status` only reports daemon state (`doctor-macos.sh:18`); egress is verified separately by the curl through port 40000 (`:20-25`). `refreshStates()` (`DashboardModel.swift:199-214`) uses only `warp-cli status` for `warp`, so it can report "connected" while egress is broken. Confirmed.

**B9 — State checks use inconsistent launchd verbs.** `doctor-macos.sh:15-16` uses `launchctl list com.openhop.<job>`; `ensureJobs`/`refreshStates` use `launchctl print gui/<uid>/...`. `launchctl print` can succeed for a loaded job whose process is not actually running, so `keeper = "running"` may be stale. Suspected (requires a live reproduction with a loaded-but-dead job).

**B10 — IP prerequisites unchecked.** `cur_v6()` in `zen-hop-macos.sh:17-19` needs the local proxy on 40000 in proxy mode. If the tunnel/proxy is down, both old and new IP are empty, producing the misleading "cloudflare handed back the same block" failure. No prerequisite check exists in `hop()`. Confirmed by source logic.

**B11 — Lane process lifecycle.** `zen-lane-macos.sh:26` execs the real OpenCode with proxy env exported at launch. There is no mechanism to refresh a long-running OpenCode process after an IP change; its environment persists until it is relaunched. Confirmed; this is an unavoidable macOS limitation unless OpenCode is restarted (restarting unrelated user processes is prohibited).

## 4. Operation matrix

| Operation | Restores all components? | Verifies final state? | Preserves stopped state? |
|---|---|---|---|
| Current Start (Resume) | No — no proxy/lane validation | No | N/A |
| Current Stop | Partially — jobs + WARP, no dependants, no state classification | Partial | N/A |
| Current Get New IP | No — only hop + conditional fix | Only doctor display | **No** (B1, B2) |
| Current WARP CLI state vs connectivity | CLI status only (B8) | No | — |

## 5. Recommended corrections (to implement)

1. Introduce one `LifecycleCoordinator` with a single operation lock and exactly three public operations: `start()` → `ensureHealthy()`, `stop()` → `stopEverything()`, `switchIP()` → `switchIPAndRefresh()`.
2. Delete `fix()`, `resumeProtection()`, `warpControl()`, `restartJob()` exposure and the Console command runner; keep `doctor()` as a diagnostic only.
3. `ensureHealthy()`: validate executables/configs → check and repair WARP → verify with `warp-cli status` **and** proxied egress → verify port 40000 listener → bootstrap/restart only unhealthy agents → verify with `launchctl print` **and** `launchctl list` → verify lane shim text → final doctor → publish verified state.
4. `stopEverything()`: sequential best-effort bootout keeper, guard, terminate only OpenHop-owned child processes, disconnect WARP; then verify each; classify fully stopped / partial / failed; never touch unrelated processes; idempotent; keep plists and config.
5. `switchIPAndRefresh()`: snapshot lifecycle state; require WARP+proxy prerequisites; run hop; wait and verify new IP and proxied egress; if previously running, refresh dependants in order (keeper → guard → doctor → lane note), otherwise keep stopped and report the prerequisite; final health check; report actual result. Must tolerate `zen-hop-macos.sh`'s EXIT trap by re-booting keeper if and only if it was running before.
6. UI: only Start / Stop / Get New IP; explicit Starting/Stopping/Switching states with current step; disable conflicting actions; no green health unless final verification passed.

## 6. Corrections implemented (after the audit above)

- `macos/LifecycleCoordinator.swift` (new) is the single owner of `start()` → `ensureHealthy`, `stop()` → `stopEverything`, `switchIP()` → `switchIPAndRefresh`, behind one `NSLock` with cancellation, progress, timeouts, explicit steps and postcondition verification.
- `macos/DashboardModel.swift` now delegates all lifecycle work to the coordinator; `fix`, `resumeProtection`, `stopProtection`, `warpControl`, `commandRunner` and the Console replay/command runner were removed. Only Start / Stop / Get New IP remain as primary actions.
- `macos/ControlCenter.swift`: Fix Everything button and menu item removed; three sections (Dashboard / Console / Diagnostics); Console is a read-only result log; Diagnostics keeps Run Doctor and Restart; toolbar shows verified state and current step; app shell is an AppKit `NSWindow` hosting the SwiftUI view because SwiftUI's `WindowGroup` scene intermittently failed to instantiate a window on this Mac.

Additional confirmed bugs found during regression testing:

**B12 — `warp-cli` state predicate was wrong.** Code used `output.lowercased().contains("connected")`; `"Disconnected"` lowercased still contains `"connected"`. A disconnected WARP was reported connected, so Stop always ended as partial and the quick-status card was wrong. Fixed by matching the exact `Status update: Connected` line in both the coordinator and the model.

**B13 — Stop treated "already stopped" as failure.** `launchctl bootout` returns `No such process` when the job is already gone. Stop now treats that as success and waits for WARP status to actually show disconnected before declaring the final state.

**B14 — Stale state after operations.** The coordinator now publishes a verified `OperationReport` snapshot (egress IP, WARP, keeper, guard, proxy, doctor output) and the model applies it immediately. Reconcile at launch publishes the same snapshot. A later doctor run that reports no egress also moves a "running" state to stopped.

**B15 — WARP startup race.** Start now polls up to 24 s for keeper, guard, WARP and egress before declaring success instead of probing once immediately after bootstrap.

**B16 — SwiftUI `WindowGroup` scene intermittently did not create a window** on this Mac (process alive, no CGWindow for its PID). Replaced with an AppKit window hosting the SwiftUI view; window creation verified by screenshot.

### Integration tests actually performed (real macOS)

- Start from stopped: ⌘K triggered Start; keeper + guard loaded, WARP connected, doctor all green.
- Stop from running: bootout + disconnect executed; after B12 fix the UI showed STOPPED with keeper/guard/WARP disconnected.
- Stop when already stopped: idempotent; `No such process` no longer downgrades the result.
- Switch IP while stopped: prerequisite error reported, lifecycle state unchanged, no services started.
- Switch IP while running: hop executed, new IP observed (`2a09:bac1:…` → `2a09:bac5:…`), dependants reloaded and final doctor green.
- App restart: reconciled state from real probes; final window showed "Everything is working", current IP, and keeper/guard/WARP running.
- `bash build-app.sh` clean build and install with ad-hoc signature.

Not performed: all 17 matrix permutations (notably repeated rapid clicks, switch-failure injection, and stop-immediately-after-start timing). Unit tests with mocks were not added; verification used real CLI state.

## 7. Remaining unknowns

- Whether `launchctl print` can report a loaded but dead job as running on macOS 27 (B9) — not reproduced.
- Whether any long-lived OpenCode process can pick up a new proxy target without relaunch (B11) — no API found in the lane shim.
- REA could not analyse Swift/bash statically; all Swift call chains are source-read, not REA-derived.

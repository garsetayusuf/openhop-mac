# Upstream Feature Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add clean-endpoint testing, continue resend, support-bundle export, a working multi-proxy pool and an opt-in Tor route (including an optional setup row) to the native Mac control center.

**Architecture:** Reuse the Python modules already vendored in `src/`, called from `DashboardModel` via the existing venv `runAsync` helper. SwiftUI views bind to new published model state. Shell scripts in `scripts/` read the same `lane/egress_mode.json` config. No new dependencies; plain `swiftc` build stays intact.

**Tech Stack:** Swift 5 / SwiftUI / AppKit, Python 3 venv, bash launchd scripts, `hdiutil` DMG packaging.

## Global Constraints

- macOS 13+, arm64 only; no sudo, no `/etc/hosts`, no `pf`.
- No new Swift package dependencies; build with `bash build-app.sh`.
- Never upload chat history, credentials or sessions.
- `setupComplete` remains based on the four required checks only.
- All new model work runs off the main queue, publishes on main, and guards `operationState`.
- Spec: `docs/superpowers/specs/2026-11-27-upstream-features-design.md`.

---

### Task 1: Clean endpoint latency test

**Files:**
- Modify: `macos/DashboardModel.swift` (after `applyCleanEndpoint`, around line 598)
- Modify: `macos/ControlCenter.swift` (`SettingsPanel`, around lines 1305-1312)

**Interfaces:**
- Consumes: `runAsync`, `announce`, `applyCleanEndpoint(_:)`, `Brand.amber`.
- Produces: `endpointLatencies: [String: Double]`, `endpointTesting: Bool`, `testCleanEndpoints()`.

- [ ] **Step 1: Add model state and test command**

Insert after `applyCleanEndpoint(_:)`:

```swift
    @Published var endpointLatencies: [String: Double] = [:]
    @Published var endpointTesting = false

    func testCleanEndpoints() {
        guard !endpointTesting else { return }
        endpointTesting = true
        endpointLatencies = [:]
        let src = (base as NSString).appendingPathComponent("src")
        let py = (base as NSString).appendingPathComponent("venv/bin/python")
        let code = "import sys,json;sys.path.insert(0,sys.argv[1]);" +
                   "from clean_endpoint import KNOWN_CLEAN_ENDPOINTS,test_endpoint_latency;" +
                   "print(json.dumps({e:test_endpoint_latency(e) for e in KNOWN_CLEAN_ENDPOINTS}))"
        runAsync(py, args: ["-c", code, src], timeout: 40) { r in
            self.endpointTesting = false
            guard r.ok, let data = r.output.data(using: .utf8),
                  let raw = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
                self.announce("Endpoint test", "Could not test endpoints (exit \(r.exitCode)).", .error)
                return
            }
            var out: [String: Double] = [:]
            for (key, value) in raw {
                if let n = value as? NSNumber { out[key] = n.doubleValue }
                else if let d = value as? Double { out[key] = d }
            }
            self.endpointLatencies = out
            self.announce("Endpoint test",
                          out.isEmpty ? "No endpoint responded." : "Tested \(out.count) endpoints.",
                          out.isEmpty ? .error : .success)
        }
    }
```

- [ ] **Step 2: Add the Test button and results to Settings**

Replace the endpoint picker block in `SettingsPanel` with:

```swift
                HStack {
                    Picker("WARP endpoint", selection: Binding(get: { model.draftCleanEndpoint },
                                                               set: { model.draftCleanEndpoint = $0 })) {
                        ForEach(endpoints, id: \.self) { Text($0).tag($0) }
                    }
                    Button("Test") { model.testCleanEndpoints() }
                        .buttonStyle(.bordered).controlSize(.small)
                        .disabled(model.endpointTesting)
                }
                if model.endpointTesting {
                    ProgressView().controlSize(.small)
                }
                if !model.endpointLatencies.isEmpty {
                    Text(model.endpointLatencies
                        .map { "\($0.key) — \(Int($0.value)) ms" }
                        .joined(separator: " · "))
                        .font(.system(size: 10)).foregroundStyle(Brand.muted)
                }
```

- [ ] **Step 3: Verify**

Run: `cd /Users/macbookpro/Code/project/openhop-mac && bash build-app.sh 2>&1 | grep -E 'error:|✓ compiled'`
Expected: a `✓ compiled` line and no `error:` line.
Self-check: `grep -n 'testCleanEndpoints' macos/DashboardModel.swift macos/ControlCenter.swift` must list two call sites plus the declaration.

- [ ] **Step 4: Commit**

```bash
git add macos/DashboardModel.swift macos/ControlCenter.swift
git commit -m "feat: test clean WARP endpoints from Settings"
```

---

### Task 2: Send continue again

**Files:**
- Modify: `macos/DashboardModel.swift` (after `testCleanEndpoints`)
- Modify: `macos/ControlCenter.swift` (`DiagnosticsPanel`, line 1040-1045)

**Interfaces:**
- Consumes: `runAsync`, `announce`, `isBusy`, `PrimaryAction`.
- Produces: `resendContinuation()`.

- [ ] **Step 1: Add the model action**

```swift
    func resendContinuation() {
        guard !isBusy else { return }
        auxiliaryBusy = true
        let src = (base as NSString).appendingPathComponent("src")
        let py = (base as NSString).appendingPathComponent("venv/bin/python")
        let code = "import sys;sys.path.insert(0,sys.argv[1]);" +
                   "from auto_resume import resend_failed;" +
                   "s,n=resend_failed();print(str(s)+'|'+n)"
        runAsync(py, args: ["-c", code, src], timeout: 60) { r in
            self.auxiliaryBusy = false
            let clean = r.output.trimmingCharacters(in: .whitespacesAndNewlines)
            let parts = clean.split(separator: "|", maxSplits: 1)
            let sent = parts.first == "1"
            let note = parts.count > 1 ? String(parts[1]) : ""
            self.announce("Send continue again",
                          r.ok && sent ? (note.isEmpty ? "Sent." : note)
                                       : "Could not resend (exit \(r.exitCode)). \(note)",
                          r.ok && sent ? .success : .error)
        }
    }
```

- [ ] **Step 2: Add the Diagnostics button**

Replace the button HStack in `DiagnosticsPanel` with:

```swift
                HStack(spacing: 10) {
                    PrimaryAction(title: model.doctorRunning ? "Running Doctor…" : "Run Doctor",
                                  symbol: "stethoscope", accent: Brand.amber) { model.doctor() }
                    PrimaryAction(title: "Fix", symbol: "wrench.and.screwdriver", accent: Brand.green) { model.repair() }
                    PrimaryAction(title: "Send continue again", symbol: "arrow.clockwise", accent: Brand.amber) {
                        model.resendContinuation()
                    }
                }
                .disabled(model.isBusy)
```

- [ ] **Step 3: Verify**

Run: `bash build-app.sh 2>&1 | grep -E 'error:|✓ compiled'`
Expected: compile succeeds.
Self-check: `grep -n 'Send continue again' macos/ControlCenter.swift` — must show one button.

- [ ] **Step 4: Commit**

```bash
git add macos/DashboardModel.swift macos/ControlCenter.swift
git commit -m "feat: resend continue from Diagnostics"
```

---

### Task 3: Export support bundle from Settings

**Files:**
- Modify: `macos/ControlCenter.swift` (`SettingsPanel`, About card around line 1335)

**Interfaces:**
- Consumes: `model.exportSupportBundle()` (existing, `DashboardModel.swift:1088`).
- Produces: a Settings button that reveals the zip in Finder.

- [ ] **Step 1: Change the exporter to return the URL**

In `DashboardModel.swift`, change the signature and tail of `exportSupportBundle()`:

```swift
    @discardableResult
    func exportSupportBundle() -> URL? {
```

and replace `recordNote("support bundle: \(zip)")` with:

```swift
        recordNote("support bundle: \(zip)")
        return FileManager.default.fileExists(atPath: zip) ? URL(fileURLWithPath: zip) : nil
```

- [ ] **Step 2: Add the Settings button**

Add inside the About `GlassCard` in `SettingsPanel`, after the version chip HStack:

```swift
                PrimaryAction(title: "Export support bundle", symbol: "square.and.arrow.up",
                              accent: Brand.amber) {
                    if let url = model.exportSupportBundle() {
                        NSWorkspace.shared.activateFileViewerSelectingURLs([url])
                    } else {
                        model.notify(title: "Support bundle", body: "Could not create the support bundle.")
                    }
                }
```

- [ ] **Step 3: Verify**

Run: `bash build-app.sh 2>&1 | grep -E 'error:|✓ compiled'`
Self-check: `grep -n 'exportSupportBundle' macos/ControlCenter.swift macos/DashboardModel.swift` — must show a call site and the declaration.
Manual: launch app → Settings → About → Export support bundle; a zip appears in `~/Downloads` and Finder selects it. Unzip and confirm only `doctor.txt` and `commands.txt`.

- [ ] **Step 4: Commit**

```bash
git add macos/DashboardModel.swift macos/ControlCenter.swift
git commit -m "feat: expose support bundle export in Settings"
```

---

### Task 4: Multi-proxy pool

**Files:**
- Modify: `macos/ControlCenter.swift` (`SettingsPanel`, custom proxy field around line 1293)
- Modify: `scripts/zen-lane-macos.sh` (lines 22-32)

**Interfaces:**
- Consumes: `proxy_chain.get_current_custom_proxy() -> tuple[str, int, int, int]` (host, port, type/index; existing).
- Produces: newline-separated `custom_proxy` config and a lane that reads the rotated entry.

- [ ] **Step 1: Make the Settings field multi-line**

Replace the `TextField("host:port", ...)` block with:

```swift
                        TextEditor(text: Binding(get: { model.draftCustomProxy },
                                                 set: { model.draftCustomProxy = $0 }))
                            .frame(height: 70)
                            .overlay(RoundedRectangle(cornerRadius: 6)
                                .stroke(Brand.muted.opacity(0.4), lineWidth: 1))
                        Text("One host:port per line. OpenHop rotates through the list.")
                            .font(.system(size: 10)).foregroundStyle(Brand.muted)
```

- [ ] **Step 2: Make the lane read the rotated proxy**

Replace the `if [ "$MODE" = "custom" ]; then ... fi` block in `scripts/zen-lane-macos.sh` with:

```bash
    if [ "$MODE" = "custom" ]; then
      # proxy_chain owns rotation: it keeps the active index and returns the current entry.
      read -r PHOST PPORT PTYPE _ <<EOF
$(python3 -c 'import sys,json;sys.path.insert(0,sys.argv[1]);from proxy_chain import get_current_custom_proxy;print(" ".join(map(str,get_current_custom_proxy())))' "$(dirname "$0")/../src" 2>/dev/null || echo "'' 0 socks5 0")
EOF
      if [ -n "$PHOST" ] && [ "$PPORT" != "0" ]; then
        if echo "$PTYPE" | grep -qi '^http'; then PROXY_TARGET="http://$PHOST:$PPORT"
        else PROXY_TARGET="socks5://$PHOST:$PPORT"; fi
      fi
    fi
```

- [ ] **Step 3: Verify**

Run: `bash -n scripts/zen-lane-macos.sh && echo SYNTAX_OK`
Expected: `SYNTAX_OK`.
Self-check: `grep -n 'get_current_custom_proxy' scripts/zen-lane-macos.sh` — must be present; `grep -n 'CUSTOM=' scripts/zen-lane-macos.sh` — must be absent.
Manual: set two proxies in Settings, Apply, run a hop, confirm `custom_proxy_index.txt` changes and OpenCode still connects.

- [ ] **Step 4: Commit**

```bash
git add macos/ControlCenter.swift scripts/zen-lane-macos.sh
git commit -m "feat: support rotating custom proxy pools on macOS"
```

---

### Task 5: Tor route, optional setup row and fallback

**Files:**
- Modify: `macos/DashboardModel.swift` (setup state near line 602, `installStep`, `setEgress`)
- Modify: `macos/ControlCenter.swift` (`SetupGateView`, `SettingsPanel` route picker)
- Modify: `scripts/zen-lane-macos.sh`

**Interfaces:**
- Consumes: `tor_manager.get_active_tor_target()`, `installStep(_:)`.
- Produces: `setupOptionalSteps`, `torAvailable`, `setEgress(mode:"tor", ...)`, flag file `lane/tor_fallback.json`.

- [ ] **Step 1: Add Tor availability and optional setup state**

Insert in `DashboardModel`, after `setupCurrent`:

```swift
    @Published var torAvailable = false
    private let setupOptionalSteps = ["Tor"]

    func refreshTorAvailability() {
        let src = (base as NSString).appendingPathComponent("src")
        let py = (base as NSString).appendingPathComponent("venv/bin/python")
        let code = "import sys;sys.path.insert(0,sys.argv[1]);from tor_manager import get_active_tor_target;print(get_active_tor_target() or '')"
        runAsync(py, args: ["-c", code, src], timeout: 10) { r in
            DispatchQueue.main.async { self.torAvailable = r.ok && !r.output.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        }
    }
```

Add a case to `installStep(_:)`'s switch:

```swift
        case "Tor":
            runAsync("/opt/homebrew/bin/brew", args: ["install", "tor"], timeout: 600, completion: done)
```

Add after the required checks in `checkSetup()`:

```swift
        refreshTorAvailability()
        setupChecks["Tor"] = torAvailable ? "OK — Tor SOCKS on 127.0.0.1:9050" : "Not installed"
```

- [ ] **Step 2: Add the optional setup row**

In `SetupGateView`, after the required `ForEach(steps, ...)` block:

```swift
            HStack(alignment: .top, spacing: 10) {
                Image(systemName: model.torAvailable ? "checkmark.circle.fill" : "questionmark.circle")
                    .foregroundStyle(model.torAvailable ? Brand.green : Brand.amber)
                    .frame(width: 18, alignment: .leading)
                VStack(alignment: .leading, spacing: 2) {
                    Text("Tor (optional)").font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Brand.text)
                    Text(model.torAvailable ? "OK — Tor SOCKS on 127.0.0.1:9050" : "Not installed — needed only for the Tor route")
                        .font(.system(size: 11)).foregroundStyle(Brand.muted)
                }
                Spacer()
                if !model.torAvailable {
                    Button("Install Tor") { model.installStep("Tor") }
                        .buttonStyle(.bordered).controlSize(.small)
                }
            }
```

`setupComplete` is untouched, so Tor never blocks Continue.

- [ ] **Step 3: Add Tor to the route picker**

In `SettingsPanel`, add `Tor` to the route `Picker`:

```swift
                    Text("Tor (optional)").tag("tor")
```

Add below the picker:

```swift
                if model.egressMode == "tor" && !model.torAvailable {
                    Text("Tor is not running — install it with `brew install tor`, or press Install Tor in setup.")
                        .font(.system(size: 10)).foregroundStyle(Brand.amber)
                }
```

- [ ] **Step 4: Map Tor in the lane**

Add before the `custom` branch in `scripts/zen-lane-macos.sh`:

```bash
    if [ "$MODE" = "tor" ]; then
      PROXY_TARGET="socks5://127.0.0.1:9050"
    fi
```

- [ ] **Step 5: Verify**

Run: `bash -n scripts/zen-lane-macos.sh && bash build-app.sh 2>&1 | grep -E 'error:|✓ compiled'`
Expected: `SYNTAX_OK` equivalent plus compile success.
Self-check: `grep -n 'setupComplete' macos/DashboardModel.swift` — must still use only the four required keys.
Manual: with Tor absent, setup shows the optional row and Continue stays enabled; with Tor running on 9050, selecting Tor in Settings keeps the lane proxy at `socks5://127.0.0.1:9050`.

- [ ] **Step 6: Commit**

```bash
git add macos/DashboardModel.swift macos/ControlCenter.swift scripts/zen-lane-macos.sh
git commit -m "feat: opt-in Tor route with optional setup install"
```

---

### Task 6: Percentage progress on every setup install

**Files:**
- Modify: `macos/DashboardModel.swift` (setup properties near line 602, `installStep`)
- Modify: `macos/ControlCenter.swift` (`SetupGateView`, both install branches)

**Interfaces:**
- Consumes: `installStep(_:)`, `setupInstalling`.
- Produces: `setupProgress: [String: Double]`, `finishInstall(_:)`.

- [ ] **Step 1: Add progress state and the timer**

Insert after `setupMessage`:

```swift
    @Published var setupProgress: [String: Double] = [:]
    private var setupProgressTimer: Timer?
    private var setupInstallStarted: Date?

    private func setupExpectedSeconds(_ step: String) -> Double {
        switch step {
        case "OpenCode": return 180
        case "WARP", "Tor": return 600
        case "App": return 120
        default: return 300
        }
    }

    private func finishInstall(_ step: String) {
        setupProgressTimer?.invalidate()
        setupProgressTimer = nil
        setupInstallStarted = nil
        setupProgress[step] = 1.0
        setupInstalling = ""
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.8) { [weak self] in
            self?.setupProgress[step] = nil
        }
    }
```

In `installStep(_:)`, after `setupMessage = "Installing \(step)…"`:

```swift
        setupInstallStarted = Date()
        setupProgress[step] = 0
        setupProgressTimer?.invalidate()
        setupProgressTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            guard let self, let active = self.setupInstalling.isEmpty ? nil : self.setupInstalling,
                  let started = self.setupInstallStarted else { return }
            let pct = min(0.95, Date().timeIntervalSince(started) / self.setupExpectedSeconds(active) * 0.95)
            DispatchQueue.main.async { self.setupProgress[active] = pct }
        }
```

- [ ] **Step 2: Finish cleanly on success and on every early return**

In the `done` closure, replace `self.setupInstalling = ""` with `self.finishInstall(step)`.
In each early-return branch (`WARP` without Homebrew, `Runtime` missing script, `Tor`
without Homebrew), replace `setupInstalling = ""` with `finishInstall(step)`.

- [ ] **Step 3: Show the percentage in both setup rows**

In `SetupGateView`, replace the required-row installing branch:

```swift
                    if model.setupInstalling == step {
                        VStack(spacing: 2) {
                            ProgressView(value: model.setupProgress[step] ?? 0)
                                .frame(width: 90)
                            Text("~\(Int((model.setupProgress[step] ?? 0) * 100))%")
                                .font(.system(size: 9)).foregroundStyle(Brand.muted)
                        }
                    } else if !status.hasPrefix("OK") && !status.hasPrefix("Checking") {
```

Apply the same `ProgressView(value:) + ~%` treatment to the optional Tor row when
`model.setupInstalling == "Tor"`.

- [ ] **Step 4: Verify**

Run: `bash build-app.sh 2>&1 | grep -E 'error:|✓ compiled'`
Expected: compile succeeds.
Self-check: `grep -n 'setupProgress' macos/DashboardModel.swift macos/ControlCenter.swift` — declarations, timer updates and both UI branches present.
Manual: press any Install button in setup; a `~N%` label increments every 0.5s and reaches 100% when the row re-checks.

- [ ] **Step 5: Commit**

```bash
git add macos/DashboardModel.swift macos/ControlCenter.swift
git commit -m "feat: show install progress percentage for every setup step"
```

---

### Task 7: Final build and packaging verification

**Files:**
- None (verification only)

- [ ] **Step 1: Full build**

Run: `cd /Users/macbookpro/Code/project/openhop-mac && bash build-dmg.sh 2>&1 | tail -5`
Expected: `built: .../dist/OpenHop.dmg` with a `hdiutil verify ... VALID` line.

- [ ] **Step 2: DMG contents**

Run:
```bash
MP=$(mktemp -d); hdiutil attach dist/OpenHop.dmg -mountpoint "$MP" -nobrowse -quiet
ls "$MP"; readlink "$MP/Applications"; hdiutil detach "$MP" -quiet
```
Expected: `OpenHop.app`, `Applications -> /Applications`.

- [ ] **Step 3: Update the README**

In `README.md`, add the five new features to the Settings bullet and the troubleshooting section, and change the Roadmap note to state that OTA is designed but not shipped.

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document endpoint test, resend, support bundle and Tor"
```

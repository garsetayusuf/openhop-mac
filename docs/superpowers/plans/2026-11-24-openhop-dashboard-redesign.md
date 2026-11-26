# OpenHop Dashboard Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current dashboard with a fresh SwiftUI control centre using the official openhop palette, a real command console, coordinated protection/WARP Stop, and no web/Terminal/sudo.

**Architecture:** One SwiftUI app binary built by `build-app.sh`. A single `DashboardModel` owns all backend calls via `run(command:args:timeout:)`, recording every invocation as a `CommandRecord` in memory and in `commands.jsonl`. Views only call the model. Menu-bar icon is created by an `NSApplicationDelegateAdaptor`.

**Tech Stack:** SwiftUI + AppKit (macOS 27, arm64), `swiftc -swift-version 5 -parse-as-library`, existing `build-app.sh`, no new dependencies.

## Global Constraints

- macOS 27 arm64; compiled with `swiftc -swift-version 5 -O -parse-as-library -target arm64-apple-macos27.0`.
- Official palette only: bg `#151619`, deep `#101113`, surface `#1d1f23`, text `#ecebe4`, muted `#a8a59b`, amber `#f2b318`, green `#3fb97a`, warn `#f07d3a`, bad `#e5534b`.
- No web redirects, no Terminal.app, no sudo, no `/etc/hosts`, no pf/iptables.
- Backend paths unchanged: `~/.local/bin/opencode`, `~/.local/bin/openhop-hop`, `~/.local/bin/openhop-doctor`, `/usr/local/bin/warp-cli`, `/bin/launchctl`.
- Doctor parsed from `✓/!/✗` prefixes; exit code ignored.
- Existing install/uninstall scripts untouched.

---

### Task 1: Model and command record layer

**Files:**
- Create: `macos/DashboardModel.swift`
- Test: `macos/ModelSelfCheck.swift` (compiled by `bash build-app.sh` into a temporary check binary)

**Interfaces:**
- Consumes: CLI shims at the paths in Global Constraints.
- Produces: `struct CommandRecord`, `final class DashboardModel: ObservableObject` with `run(_:args:timeout:) -> CommandRecord`, `doctor(completion:)`, `hop(autoFix: Bool)`, `fix()`, `launch()`, `stopProtection()`, `resumeProtection()`, `exportSupportBundle()`, `commandRunner(command:args:)`.

- [ ] **Step 1: Create `DashboardModel.swift` with `CommandRecord` and `run`**

```swift
import Foundation

struct CommandRecord: Codable, Identifiable {
    let id = UUID()
    var started: Date
    var command: String
    var args: [String]
    var exitCode: Int32
    var duration: TimeInterval
    var output: String
    var ok: Bool { exitCode == 0 }
}

final class DashboardModel: ObservableObject {
    @Published var records: [CommandRecord] = []
    @Published var ip = "—"
    @Published var health = "OFFLINE"
    @Published var doctorOutput = ""
    @Published var keeper = "unknown"
    @Published var guard = "unknown"
    @Published var warp = "unknown"
    private var running: Process?
    private let base = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/openhop")

    @discardableResult
    func run(_ command: String, args: [String] = [], timeout: TimeInterval = 60) -> CommandRecord {
        let start = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: command)
        p.arguments = args
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        var output = ""
        var code: Int32 = -1
        do {
            running = p
            try p.run()
            output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            p.waitUntilExit()
            code = p.terminationStatus
        } catch { output = "could not start \(command): \(error.localizedDescription)" }
        running = nil
        let r = CommandRecord(started: start, command: command, args: args, exitCode: code,
                              duration: Date().timeIntervalSince(start), output: output)
        records.append(r)
        appendJSONL(r)
        return r
    }

    private func appendJSONL(_ r: CommandRecord) {
        let path = (base as NSString).appendingPathComponent("logs/commands.jsonl")
        try? FileManager.default.createDirectory(atPath: (base as NSString).deletingLastPathComponent, withIntermediateDirectories: true)
        if let data = try? JSONEncoder().encode(r), var line = String(data: data, encoding: .utf8) {
            line += "\n"
            if let h = FileHandle(forWritingAtPath: path) { h.seekToEndOfFile(); h.write(Data(line.utf8)); h.closeFile() }
            else { try? line.write(toFile: path, atomically: true, encoding: .utf8) }
        }
    }
}
```

- [ ] **Step 2: Compile-check the model**

Run: `"$HOME/Library/Application Support/openhop/venv/bin/python" -c "print('swiftc via build')" && swiftc -swift-version 5 -parse-as-library -target arm64-apple-macos27.0 -typecheck macos/DashboardModel.swift`
Expected: exit 0, no errors.

- [ ] **Step 3: Add `doctor`, `hop(autoFix:)`, `fix`, `launch`, protection actions and bundle export to the model**

Implement using `run` only:
  - `doctor` parses `✓/!/✗`, sets `ip` from the `(warp verified): <ip>` line, sets `health` and `doctorOutput`.
  - `hop(autoFix:)` runs hop with timeout 180, then calls `fix()` when `autoFix == true`.
  - `fix()` runs doctor, checks keeper/guard with `launchctl print`, bootstraps missing plists, runs doctor again.
  - `launch()` runs `~/.local/bin/opencode` on a background queue with timeout 60.
  - `stopProtection()` boots out both jobs then runs `warp-cli disconnect`; `resumeProtection()` bootstraps both jobs then `warp-cli connect`.
  - `exportSupportBundle()` writes a local zip of `commands.jsonl` and doctor output under `~/Library/Application Support/openhop/logs/`.
  - `commandRunner` accepts only the four approved commands: doctor, hop, lane, warp status.

- [ ] **Step 4: Re-run typecheck**

Run: `swiftc -swift-version 5 -parse-as-library -target arm64-apple-macos27.0 -typecheck macos/DashboardModel.swift`
Expected: exit 0.

---

### Task 2: SwiftUI app shell and menu-bar icon

**Files:**
- Create: `macos/ControlCenter.swift` (replace current file with fresh SwiftUI app)
- Modify: `build-app.sh` (compile both Swift files; keep ad-hoc signing and install)

**Interfaces:**
- Consumes: `DashboardModel` from Task 1.
- Produces: `@main struct OpenHopApp: App`, `final class AppDelegate: NSObject, NSApplicationDelegate`, `DashboardView`.

- [ ] **Step 1: Write app shell with `@NSApplicationDelegateAdaptor` and status item**

```swift
@main
struct OpenHopApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = DashboardModel()
    var body: some Scene {
        WindowGroup { DashboardView().environmentObject(model).frame(minWidth: 940, minHeight: 560) }
            .defaultSize(width: 1180, height: 700)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem?
    func applicationDidFinishLaunching(_ n: Notification) {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        let img = NSImage(named: "openhop-icon") ?? NSImage(systemSymbolName: "network", accessibilityDescription: nil)
        img?.isTemplate = true
        item.button?.image = img
        let menu = NSMenu()
        [("Open Dashboard", #selector(show)), ("Run Doctor", #selector(doctor)),
         ("Get new IP", #selector(hop)), ("Fix everything", #selector(fix))].forEach {
            let i = NSMenuItem(title: $0.0, action: $0.1, keyEquivalent: ""); i.target = self; menu.addItem(i)
        }
        menu.addItem(NSMenuItem.separator())
        let q = NSMenuItem(title: "Quit", action: #selector(quit), keyEquivalent: "q"); q.target = self; menu.addItem(q)
        item.menu = menu
        self.item = item
    }
    @objc private func show() { for w in NSApp.windows { w.makeKeyAndOrderFront(nil) } }
    @objc private func doctor() { NotificationCenter.default.post(name: .openhopDoctor, object: nil) }
    @objc private func hop() { NotificationCenter.default.post(name: .openhopHop, object: nil) }
    @objc private func fix() { NotificationCenter.default.post(name: .openhopFix, object: nil) }
    @objc private func quit() { NSApp.terminate(nil) }
}
extension Notification.Name {
    static let openhopDoctor = Notification.Name("openhop.doctor")
    static let openhopHop = Notification.Name("openhop.hop")
    static let openhopFix = Notification.Name("openhop.fix")
}
```

- [ ] **Step 2: Write `DashboardView` with `NavigationSplitView`, four sections, official palette, glass cards**

Sections: Overview, Console, Diagnostics, Protection. Toolbar contains status chip and the single **Stop / Resume** control. Protection view is read-only status. No duplicate doctor/fix buttons outside Overview and Console.

- [ ] **Step 3: Add disabled Tor/custom-proxy roadmap row in Protection view**

```swift
HStack {
    Label("Tor / custom proxy — not available on Mac port", systemImage: "network.slash")
    Spacer()
    Text("Roadmap").font(.caption)
}
.disabled(true)
.opacity(0.5)
```

- [ ] **Step 4: Update `build-app.sh` to compile both Swift files**

Run: `bash build-app.sh`
Expected: exit 0, `/Applications/OpenHop.app/Contents/MacOS/OpenHop` updated.

---

### Task 3: Console and command runner

**Files:**
- Modify: `macos/ControlCenter.swift` (ConsoleView, CommandRunnerView)

**Interfaces:**
- Consumes: `DashboardModel.records`, `run`, `commandRunner`.
- Produces: `ConsoleView`, `CommandRunnerView`.

- [ ] **Step 1: Implement console list with search, replay, copy, save, clear**

Use `@State private var search = ""`; filter `model.records` by `command` and `output.localizedCaseInsensitiveContains(search)`. Detail pane shows full `CommandRecord.output`. Replay calls `model.run(record.command, args: record.args)`.

- [ ] **Step 2: Implement command runner restricted to approved commands**

```swift
private let approved = ["doctor", "hop", "lane", "warp status"]
```
Map strings to exact executable paths from Global Constraints; reject anything else with output `command not allowed`.

- [ ] **Step 3: Compile and capture console screenshot**

Run: `bash build-app.sh && open /Applications/OpenHop.app`
Expected: exit 0; Console view opens and shows command records.

---

### Task 4: On-Mac verification and rollback

**Files:**
- None (verification only)

- [ ] **Step 1: Clean build and launch**

Run: `bash build-app.sh; echo EXIT=$?; /Applications/OpenHop.app/Contents/MacOS/OpenHop`
Expected: exit 0; window opens; menu-bar icon visible in screenshot.

- [ ] **Step 2: Verify sections and actions**

Click each sidebar item; screenshot each. Confirm Get new IP changes IP and runs fix. Confirm Stop disconnects WARP and Resume reconnects. Confirm Console shows records.

- [ ] **Step 3: Doctor and rollback**

Run: `export PATH="$HOME/.local/bin:$PATH"; openhop-doctor; bash uninstall-macos.sh; bash install-macos.sh; openhop-doctor`
Expected: doctor green before and after; uninstall removes jobs/shims; reinstall restores them.

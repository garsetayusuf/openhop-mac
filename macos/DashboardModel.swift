import Foundation
import AppKit
import SwiftUI
import UserNotifications

struct CommandRecord: Codable, Identifiable {
    let id: UUID
    var started: Date
    var command: String
    var args: [String]
    var exitCode: Int32
    var duration: TimeInterval
    var output: String
    var ok: Bool { exitCode == 0 }
}

private final class ClosureRunner: CommandExecuting {
    let block: (String, [String], TimeInterval) -> CommandOutcome
    init(_ block: @escaping (String, [String], TimeInterval) -> CommandOutcome) { self.block = block }
    func run(_ executable: String, _ args: [String], _ timeout: TimeInterval) -> CommandOutcome {
        block(executable, args, timeout)
    }
}

// MARK: - UI-facing models (raw command output never reaches these)

enum EventTone: String, Codable, Equatable {
    case success, info, warn, error
}

struct ActivityEvent: Identifiable, Codable {
    let id: UUID
    let at: Date
    let symbol: String
    let title: String
    let detail: String
    let tone: EventTone
}

struct HealthCheck: Identifiable {
    let id: String
    let label: String
    var detail: String
    var ok: Bool
    var warning = false
}

struct AppToast: Identifiable, Equatable {
    let id: UUID
    let title: String
    let detail: String
    let tone: EventTone
    static func == (lhs: AppToast, rhs: AppToast) -> Bool { lhs.id == rhs.id }
}

final class DashboardModel: NSObject, ObservableObject, UNUserNotificationCenterDelegate {
    static let shared = DashboardModel()
    @Published var records: [CommandRecord] = []
    @Published var ip = "—"
    /// Menu-bar dot: green = working, amber = transitional/needs attention, red = not working.
    var statusNSColor: NSColor {
        if isBusy || operationState == .starting || operationState == .switching { return .systemOrange }
        if operationState == .running { return health.contains("WORKING") ? .systemGreen : .systemOrange }
        return .systemRed
    }
    var statusColor: Color { Color(nsColor: statusNSColor) }

    @Published var health = "OFFLINE"
    @Published var doctorOutput = "" // audit / support bundle only — never rendered
    @Published var keeper = "unknown"
    @Published var guardJob = "unknown"
    @Published var warp = "unknown"
    @Published var laneStatus = "not checked"
    @Published var operationState: LifecycleState = .unknown
    @Published var progressMessage = "Ready"
    @Published var lastReport: OperationReport?
    @Published var search = ""

    @Published var events: [ActivityEvent] = []
    @Published var healthChecks: [HealthCheck] = []
    @Published var toast: AppToast?
    @Published var notificationStatus = "not requested"
    @Published var doctorRunning = false
    @Published var auxiliaryBusy = false
    @Published var keepInBackground: Bool = UserDefaults.standard.object(forKey: "openhop.keepInBackground") as? Bool ?? true

    // Route is the only setting with a pending draft: changing it can break the live tunnel.
    @Published var draftEgressMode = "warp"
    @Published var draftCustomProxy = "127.0.0.1:10808"
    @Published var draftCustomProxyType = "socks5"
    @Published var draftCleanEndpoint = "automatic"

    var routeDirty: Bool {
        draftEgressMode != egressMode || draftCustomProxy != customProxy ||
        draftCustomProxyType != customProxyType || draftCleanEndpoint != cleanEndpoint
    }

    func loadRouteDraft() {
        draftEgressMode = egressMode
        draftCustomProxy = customProxy
        draftCustomProxyType = customProxyType
        draftCleanEndpoint = cleanEndpoint
    }

    func applyRouteDraft() {
        if draftEgressMode != egressMode || draftCustomProxy != customProxy || draftCustomProxyType != customProxyType {
            setEgress(mode: draftEgressMode, proxy: draftCustomProxy, type: draftCustomProxyType)
        }
        if draftCleanEndpoint != cleanEndpoint { applyCleanEndpoint(draftCleanEndpoint) }
    }

    private var pendingOperation: String?
    private var running: Process?
    private var guardTimer: Timer?
    private var guardLogOffset: UInt64 = 0
    private var guardLogStarted = false
    private let base = (NSHomeDirectory() as NSString).appendingPathComponent("Library/Application Support/openhop")
    private var doctorPath: String { (NSHomeDirectory() as NSString).appendingPathComponent(".local/bin/openhop-doctor") }
    private var lanePath: String { (NSHomeDirectory() as NSString).appendingPathComponent(".local/bin/opencode") }
    private var logDir: String { (base as NSString).appendingPathComponent("logs") }

    override init() {
        super.init()
        healthChecks = parseDoctor("")
        events = loadEvents()
    }

    var isBusy: Bool {
        operationState == .starting || operationState == .stopping || operationState == .switching || auxiliaryBusy
    }

    lazy var coordinator: LifecycleCoordinator = {
        let c = LifecycleCoordinator(runner: ClosureRunner { [weak self] exe, args, timeout in
            guard let self else { return CommandOutcome(ok: false, exitCode: -1, output: "model unavailable") }
            let r = self.run(exe, args: args, timeout: timeout)
            return CommandOutcome(ok: r.ok, exitCode: r.exitCode, output: r.output)
        })
        c.onProgress = { [weak self] state, message, report in
            DispatchQueue.main.async {
                guard let self else { return }
                self.operationState = state
                if message.contains(" restart exit") {
                    self.progressMessage = self.emit(message: message, state: state)
                    self.handleRestartResult(message)
                } else if let report {
                    self.progressMessage = report.summary
                    self.lastReport = report
                    self.applyReport(report)
                    // If the report says stopped/failed but every component is actually up, trust the components.
                    if (state == .stopped || state == .failed), report.keeperRunning, report.guardRunning,
                       report.warpConnected, report.proxyListening, report.egressIP != nil {
                        self.operationState = .running
                        self.health = "EVERYTHING IS WORKING"
                    }
                    self.refreshStates()
                    self.recordNote(report.summary)
                    if !report.summary.hasPrefix("State reconciled") {
                        self.notify(title: self.pendingOperation ?? "OpenHop", body: report.summary)
                    }
                    self.finishPendingOperation(report)
                } else {
                    self.progressMessage = self.emit(message: message, state: state)
                }
            }
        }
        return c
    }()

    // MARK: Command execution + audit log

    @discardableResult
    func run(_ command: String, args: [String] = [], timeout: TimeInterval = 60) -> CommandRecord {
        let start = Date()
        let p = Process()
        p.executableURL = URL(fileURLWithPath: command)
        p.arguments = args
        let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
        var output = ""; var code: Int32 = -1
        let killer = DispatchWorkItem { if p.isRunning { p.terminate() } }
        DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + timeout, execute: killer)
        do {
            running = p
            try p.run()
            output = String(data: pipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8) ?? ""
            p.waitUntilExit(); code = p.terminationStatus
        } catch { output = "could not start \(command): \(error.localizedDescription)" }
        killer.cancel(); running = nil
        let r = CommandRecord(id: UUID(), started: start, command: command, args: args,
                              exitCode: code, duration: Date().timeIntervalSince(start), output: output)
        DispatchQueue.main.async {
            self.records.append(r)
            if self.records.count > 100 { self.records.removeFirst(self.records.count - 100) }
        }
        appendJSONL(r)
        return r
    }

    func runAsync(_ command: String, args: [String] = [], timeout: TimeInterval = 60,
                  completion: ((CommandRecord) -> Void)? = nil) {
        DispatchQueue.global(qos: .userInitiated).async {
            let r = self.run(command, args: args, timeout: timeout)
            DispatchQueue.main.async { completion?(r) }
        }
    }

    func stopCurrent() { coordinator.cancel() }

    private func appendJSONL(_ r: CommandRecord) {
        try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
        let path = (logDir as NSString).appendingPathComponent("commands.jsonl")
        if let data = try? JSONEncoder().encode(r), var line = String(data: data, encoding: .utf8) {
            line += "\n"
            if let h = FileHandle(forWritingAtPath: path) { h.seekToEndOfFile(); h.write(Data(line.utf8)); h.closeFile() }
            else { try? line.write(toFile: path, atomically: true, encoding: .utf8) }
        }
    }

    func recordNote(_ text: String) {
        let r = CommandRecord(id: UUID(), started: Date(), command: "openhop", args: [],
                              exitCode: 0, duration: 0, output: text)
        DispatchQueue.main.async {
            self.records.append(r)
            if self.records.count > 100 { self.records.removeFirst(self.records.count - 100) }
        }
        appendJSONL(r)
    }

    // MARK: Actions — each one announces itself and reports its result in the window

    func reconcile() { coordinator.reconcile() }

    func start() {
        guard !isBusy else { return }
        pendingOperation = "Start"
        announce("Starting OpenHop", "Bringing the tunnel and background jobs online.", .info)
        emit(title: "Start requested", detail: "Bringing the tunnel and background jobs online.", symbol: "play.circle.fill", tone: .info)
        lastReport = nil
        coordinator.start()
    }

    /// Diagnostics "Fix" uses the same canonical recovery operation as Start.
    func repair() {
        guard !isBusy else { return }
        announce("Fixing OpenHop", "Running the full recovery and verification sequence.", .info)
        emit(title: "Fix requested", detail: "Running the full recovery and verification sequence.", symbol: "wrench.and.screwdriver", tone: .info)
        start()
    }

    func stop() {
        guard !isBusy else { return }
        pendingOperation = "Stop"
        announce("Stopping OpenHop", "Shutting down the tunnel and background jobs.", .info)
        emit(title: "Stop requested", detail: "Shutting down the tunnel and background jobs.", symbol: "stop.circle.fill", tone: .info)
        lastReport = nil
        coordinator.stop()
    }

    func warpControl(_ action: String) {
        guard !isBusy else { return }
        announce("WARP \(action)", "Sending \(action) to the local WARP CLI.", .info)
        runAsync("/usr/local/bin/warp-cli", args: [action], timeout: 30) { r in
            self.announce(r.ok ? "WARP \(action) done" : "WARP \(action) failed",
                          r.ok ? "Command completed." : "Exit \(r.exitCode).", r.ok ? .success : .error)
            self.refreshStates()
        }
    }

    func switchIP() {
        guard !isBusy else { return }
        pendingOperation = "Get New IP"
        announce("Getting a new IP", "Requesting a fresh identity from the hop service.", .info)
        emit(title: "New IP requested", detail: "Requesting a fresh identity from the hop service.", symbol: "arrow.triangle.2.circlepath", tone: .info)
        lastReport = nil
        coordinator.switchIP()
    }

    func restartJob(_ job: String) {
        guard !isBusy else { return }
        let label = job == "keeper" ? "Keeper" : "Guard"
        auxiliaryBusy = true
        announce("Restarting \(label)", "The background job is restarting.", .info)
        emit(title: "\(label) restart requested", detail: "The background job is restarting.", symbol: "gearshape.fill", tone: .info)
        DispatchQueue.global(qos: .userInitiated).async { self.coordinator.restartJob(job) }
    }

    func liveJobRunning(_ job: String) -> Bool {
        run("/bin/launchctl", args: ["print", "gui/\(getuid())/com.openhop.\(job)"], timeout: 8).ok
    }

    func liveWarpConnected() -> Bool {
        let r = run("/usr/local/bin/warp-cli", args: ["status"], timeout: 10)
        return r.output.contains("Status update: Connected")
    }

    /// Decide from live launchd state, never from a cached UI value.
    func toggleService(_ job: String) {
        guard !isBusy else { return }
        setService(job, enabled: !liveJobRunning(job))
    }

    func setService(_ job: String, enabled: Bool) {
        guard !isBusy else { return }
        let label = job == "keeper" ? "Background protection" : "Auto-rotate on limit"
        if liveJobRunning(job) == enabled {
            refreshStates()
            return
        }
        auxiliaryBusy = true
        announce(enabled ? "Enabling \(label)" : "Disabling \(label)", "Updating the launchd job in this user session.", .info)
        emit(title: "\(label) \(enabled ? "on" : "off")", detail: "Updating the launchd job in this user session.",
             symbol: enabled ? "checkmark.circle.fill" : "xmark.circle.fill", tone: .info)
        let name = "com.openhop.\(job)"
        let plist = (NSHomeDirectory() as NSString).appendingPathComponent("Library/LaunchAgents/\(name).plist")
        let args = enabled ? ["bootstrap", "gui/\(getuid())", plist] : ["bootout", "gui/\(getuid())/\(name)"]
        DispatchQueue.global(qos: .userInitiated).async {
            let r = self.run("/bin/launchctl", args: args, timeout: 20)
            let actual = self.liveJobRunning(job)
            DispatchQueue.main.async {
                if job == "keeper" { self.keeper = actual ? "running" : "stopped" }
                else { self.guardJob = actual ? "running" : "stopped" }
                self.syncHealthWithLiveState()
                self.auxiliaryBusy = false
                self.refreshStates()
                let ok = r.ok && actual == enabled
                let message = ok ? "Done." : "Command failed (exit \(r.exitCode)); service is \(actual ? "running" : "stopped")."
                self.announce(enabled ? "\(label) on" : "\(label) off", message, ok ? .success : .error)
                self.notify(title: label, body: message)
            }
        }
    }

    func doctor() {
        guard !isBusy else { return }
        auxiliaryBusy = true
        doctorRunning = true
        announce("Running health checks", "Checking services, tunnel and IP.", .info)
        emit(title: "Health check started", detail: "Checking services, tunnel and IP.", symbol: "stethoscope", tone: .info)
        runAsync(doctorPath) { r in
            self.auxiliaryBusy = false
            self.doctorRunning = false
            self.applyDoctor(r.output)
            self.refreshStates()
            let ok = r.ok
            let passed = self.healthChecks.filter { $0.ok }.count
            let detail = ok ? "\(passed) of \(self.healthChecks.count) checks passed."
                            : "Health checks could not complete."
            self.announce(ok ? "Health check complete" : "Health check failed", detail, ok ? .success : .error)
            self.emit(title: ok ? "Health check completed" : "Health check failed", detail: detail,
                      symbol: ok ? "checkmark.circle.fill" : "xmark.circle.fill", tone: ok ? .success : .error)
        }
    }

    func launchOpenCode() {
        guard !isBusy else { return }
        let support = base
        let script = (support as NSString).appendingPathComponent("launch-opencode.command")
        let launcher = """
        #!/bin/bash
        # OpenHop lane launcher — runs the protected OpenCode CLI in this Terminal.
        export PATH="$HOME/.local/bin:$PATH"
        exec "$HOME/.local/bin/opencode" "$@"
        """
        do {
            try FileManager.default.createDirectory(atPath: support, withIntermediateDirectories: true)
            try launcher.write(toFile: script, atomically: true, encoding: .utf8)
            try FileManager.default.setAttributes([.posixPermissions: NSNumber(value: 0o755)], ofItemAtPath: script)
            let opened = NSWorkspace.shared.open(URL(fileURLWithPath: script))
            announce(opened ? "OpenCode launched" : "OpenCode could not launch",
                     opened ? "A Terminal window is opening with the protected lane." : "Could not open the Terminal launcher.",
                     opened ? .success : .error)
            emit(title: opened ? "OpenCode launched in Terminal" : "OpenCode could not launch",
                 detail: opened ? "The lane shim sets the WARP proxy; OpenCode's TUI opens in Terminal." : "Could not open launch-opencode.command.",
                 symbol: opened ? "checkmark.circle.fill" : "xmark.circle.fill", tone: opened ? .success : .error)
        } catch {
            announce("OpenCode could not launch", error.localizedDescription, .error)
            emit(title: "OpenCode could not launch", detail: error.localizedDescription,
                 symbol: "xmark.circle.fill", tone: .error)
        }
    }

    // MARK: Guard log → live Activity events

    func startGuardWatch() {
        guard guardTimer == nil else { return }
        guardTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: true) { [weak self] _ in
            self?.readGuardLog()
        }
        readGuardLog()
    }

    private func readGuardLog() {
        let path = (base as NSString).appendingPathComponent("guard/guard.log")
        guard FileManager.default.fileExists(atPath: path),
              let handle = FileHandle(forReadingAtPath: path) else { return }
        defer { try? handle.close() }
        let size = handle.seekToEndOfFile()
        if !guardLogStarted {
            guardLogStarted = true
            guardLogOffset = size
            // Show recent relevant history once, without replaying the whole log.
            let probe = FileHandle(forReadingAtPath: path)
            let data = probe?.readDataToEndOfFile() ?? Data()
            try? probe?.close()
            let all = (String(data: data, encoding: .utf8) ?? "").split(separator: "\n").suffix(200)
            for line in all.suffix(12) { emitGuardLine(String(line), notify: false) }
            return
        }
        if size < guardLogOffset { guardLogOffset = 0 }
        guard size > guardLogOffset else { return }
        handle.seek(toFileOffset: guardLogOffset)
        let data = handle.readDataToEndOfFile()
        guardLogOffset = size
        let text = String(data: data, encoding: .utf8) ?? ""
        for line in text.split(separator: "\n") { emitGuardLine(String(line)) }
    }

    private func emitGuardLine(_ raw: String, notify: Bool = true) {
        let line = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !line.isEmpty else { return }
        let lower = line.lowercased()
        let title: String?
        let tone: EventTone
        if lower.contains("free limit") || lower.contains("quota") || lower.contains("rate limit") {
            title = "OpenCode free limit detected"; tone = .warn
        } else if lower.contains("getting a new ip") || lower.contains("new ip") || lower.contains("rotat") {
            title = "Getting a new IP"; tone = .info
        } else if lower.contains("continue") || lower.contains("resume") {
            title = "Continue sent to chat"; tone = .success
        } else if lower.contains("fresh") || lower.contains("new /64") {
            title = "New IP ready"; tone = .success
        } else { return }
        let detail = String(line.suffix(160))
        if notify { self.notify(title: title!, body: detail) }
        emit(title: title!, detail: detail, symbol: tone == .warn ? "exclamationmark.triangle.fill" : "waveform.path.ecg", tone: tone)
    }

    // MARK: macOS system notifications

    var notificationPermissionDenied: Bool {
        notificationStatus == "denied" || notificationStatus == "notDetermined" || notificationStatus == "provisional"
    }

    func setupNotifications() {
        let center = UNUserNotificationCenter.current()
        center.delegate = self
        center.requestAuthorization(options: [.alert, .sound, .badge]) { [weak self] granted, error in
            let result = "notification authorization granted=\(granted) error=\(error?.localizedDescription ?? "none")"
            self?.recordNote(result)
            self?.loadNotificationSettings()
        }
    }

    private func loadNotificationSettings() {
        UNUserNotificationCenter.current().getNotificationSettings { [weak self] settings in
            let status: String
            switch settings.authorizationStatus {
            case .authorized: status = "authorized"
            case .denied: status = "denied"
            case .notDetermined: status = "notDetermined"
            case .provisional: status = "provisional"
            case .ephemeral: status = "ephemeral"
            @unknown default: status = "unknown"
            }
            let detail = "notification settings status=\(status) alert=\(settings.alertSetting.rawValue) sound=\(settings.soundSetting.rawValue) badge=\(settings.badgeSetting.rawValue)"
            self?.recordNote(detail)
            DispatchQueue.main.async { self?.notificationStatus = status }
        }
    }

    func notify(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request) { [weak self] error in
            self?.recordNote("notification posted title=\(title) error=\(error?.localizedDescription ?? "none")")
        }
    }

    func sendTestNotification() {
        notify(title: "OpenHop", body: "System notifications are working.")
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                willPresent notification: UNNotification,
                                withCompletionHandler completionHandler: @escaping (UNNotificationPresentationOptions) -> Void) {
        completionHandler([.banner, .list, .sound])
    }

    func userNotificationCenter(_ center: UNUserNotificationCenter,
                                didReceive response: UNNotificationResponse,
                                withCompletionHandler completionHandler: @escaping () -> Void) {
        DispatchQueue.main.async {
            NotificationCenter.default.post(name: .openhopOpenWindow, object: nil)
        }
        completionHandler()
    }

    func setKeepInBackground(_ value: Bool) {
        keepInBackground = value
        UserDefaults.standard.set(value, forKey: "openhop.keepInBackground")
        announce(value ? "Keeping OpenHop in the menu bar" : "Quit now stops everything",
                 value ? "Cmd+Q hides the window; protection keeps running." : "Cmd+Q stops keeper, guard and WARP, then quits.",
                 .info)
    }

    // MARK: - Launch at login (user launch agent, no sudo)

    private var autostartPlist: String {
        (NSHomeDirectory() as NSString).appendingPathComponent("Library/LaunchAgents/com.openhop.autostart.plist")
    }

    @Published var launchAtLogin = FileManager.default.fileExists(atPath: (NSHomeDirectory() as NSString)
        .appendingPathComponent("Library/LaunchAgents/com.openhop.autostart.plist"))

    func setLaunchAtLogin(_ value: Bool) {
        guard operationState != .starting, operationState != .stopping, operationState != .switching else { return }
        auxiliaryBusy = true
        let plist = autostartPlist
        if value {
            let xml = """
            <?xml version="1.0" encoding="UTF-8"?>
            <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
            <plist version="1.0"><dict>
            <key>Label</key><string>com.openhop.autostart</string>
            <key>ProgramArguments</key><array><string>/Applications/OpenHop.app/Contents/MacOS/OpenHop</string></array>
            <key>RunAtLoad</key><true/>
            </dict></plist>
            """
            try? xml.write(toFile: plist, atomically: true, encoding: .utf8)
        } else {
            try? FileManager.default.removeItem(atPath: plist)
        }
        let name = "com.openhop.autostart"
        let args = value ? ["bootstrap", "gui/\(getuid())", plist] : ["bootout", "gui/\(getuid())/\(name)"]
        runAsync("/bin/launchctl", args: args, timeout: 15) { r in
            self.auxiliaryBusy = false
            self.launchAtLogin = value && r.ok
            self.announce(value ? "Launch at login on" : "Launch at login off", r.ok ? "Done." : "Command failed (exit \(r.exitCode)).", r.ok ? .success : .error)
        }
    }

    // MARK: - Route: WARP / custom proxy / clean endpoint (local config only)

    @Published var egressMode = "warp"
    @Published var customProxy = "127.0.0.1:10808"
    @Published var customProxyType = "socks5"
    @Published var cleanEndpoint = "automatic"

    private var egressConfigPath: String {
        (base as NSString).appendingPathComponent("lane/egress_mode.json")
    }

    func loadEgressConfig() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: egressConfigPath)),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            egressMode = "warp"
            return
        }
        egressMode = (json["mode"] as? String) ?? "warp"
        customProxy = (json["custom_proxy"] as? String) ?? customProxy
        customProxyType = (json["custom_type"] as? String) ?? customProxyType
        let ep = (json["clean_endpoint"] as? String) ?? "automatic"
        cleanEndpoint = ep
    }

    func setEgress(mode: String, proxy: String, type: String) {
        guard operationState != .starting, operationState != .stopping, operationState != .switching else { return }
        auxiliaryBusy = true
        let src = (base as NSString).appendingPathComponent("src")
        let py = (base as NSString).appendingPathComponent("venv/bin/python")
        let code = "import sys;sys.path.insert(0,sys.argv[1]);from proxy_chain import set_egress_config;" +
                   "print(set_egress_config(sys.argv[2],sys.argv[3],sys.argv[4]))"
        runAsync(py, args: ["-c", code, src, mode, proxy, type], timeout: 20) { r in
            self.auxiliaryBusy = false
            if r.ok { self.loadEgressConfig() }
            self.announce("Route updated", r.ok ? "\(mode) — OpenCode restarts will use it." : "Failed to save route (exit \(r.exitCode)).", r.ok ? .success : .error)
            self.notify(title: "OpenHop route", body: r.ok ? "Using \(mode)." : "Could not change route.")
        }
    }

    func applyCleanEndpoint(_ endpoint: String) {
        guard operationState != .starting, operationState != .stopping, operationState != .switching else { return }
        auxiliaryBusy = true
        let src = (base as NSString).appendingPathComponent("src")
        let py = (base as NSString).appendingPathComponent("venv/bin/python")
        let cfg = egressConfigPath
        let code = "import sys,json;sys.path.insert(0,sys.argv[1]);" +
                   "from clean_endpoint import set_custom_endpoint,reset_custom_endpoint;" +
                   "e=sys.argv[2];(reset_custom_endpoint() if e=='automatic' else set_custom_endpoint(e));" +
                   "p=sys.argv[3];d=json.load(open(p));d['clean_endpoint']=e;json.dump(d,open(p,'w'))"
        runAsync(py, args: ["-c", code, src, endpoint, cfg], timeout: 40) { r in
            self.auxiliaryBusy = false
            if r.ok { self.cleanEndpoint = endpoint; self.loadEgressConfig() }
            self.announce("WARP endpoint", r.ok ? (endpoint == "automatic" ? "Back to the default entry point." : "Using \(endpoint).") : "Could not set endpoint (exit \(r.exitCode)).", r.ok ? .success : .error)
        }
    }

    // MARK: - Launch gate

    @Published var setupChecks: [String: String] = [:]
    @Published var setupChecking = false
    @Published var setupCurrent = ""
    @Published var setupInstalling = ""
    @Published var setupMessage = ""

    private let setupSteps = ["OpenCode", "WARP", "App", "Runtime"]

    func checkSetupAsync() {
        guard !setupChecking else { return }
        setupChecking = true
        setupCurrent = ""
        for step in setupSteps { setupChecks[step] = "Checking…" }
        DispatchQueue.global(qos: .userInitiated).async {
            for step in self.setupSteps {
                DispatchQueue.main.async { self.setupCurrent = step }
                let result = self.checkStep(step)
                Thread.sleep(forTimeInterval: 2.0)
                DispatchQueue.main.async { self.setupChecks[step] = result }
            }
            DispatchQueue.main.async {
                self.setupCurrent = ""
                self.setupChecking = false
            }
        }
    }

    var setupComplete: Bool {
        ["OpenCode", "WARP", "App", "Runtime"].allSatisfy { key in
            setupChecks[key]?.hasPrefix("OK") == true
        }
    }

    func checkSetup() {
        for step in setupSteps { setupChecks[step] = checkStep(step) }
    }

    func installStep(_ step: String) {
        guard setupInstalling.isEmpty else { return }
        setupInstalling = step
        setupMessage = "Installing \(step)…"
        let done: (CommandRecord) -> Void = { [weak self] r in
            guard let self else { return }
            self.setupMessage = r.ok ? "\(step) installed." : "Install failed (exit \(r.exitCode)): \(r.output.prefix(160))"
            self.setupChecks[step] = self.checkStep(step)
            self.setupInstalling = ""
        }
        switch step {
        case "OpenCode":
            runAsync("/bin/bash", args: ["-c", "curl -fsSL https://opencode.ai/install | bash"], timeout: 300, completion: done)
        case "WARP":
            let brew = "/opt/homebrew/bin/brew"
            guard FileManager.default.isExecutableFile(atPath: brew) else {
                setupMessage = "Homebrew not found — install it from https://brew.sh first."
                setupInstalling = ""
                return
            }
            runAsync(brew, args: ["install", "--cask", "cloudflare-warp"], timeout: 600, completion: done)
        case "App":
            let src = Bundle.main.bundleURL.path.replacingOccurrences(of: "'", with: "'\\''")
            let cmd = "do shell script \"rm -rf /Applications/OpenHop.app && cp -R '\(src)' /Applications/OpenHop.app\" with administrator privileges"
            runAsync("/usr/bin/osascript", args: ["-e", cmd], timeout: 180, completion: done)
        default:
            let script = (Bundle.main.resourceURL ?? Bundle.main.bundleURL)
                .appendingPathComponent("payload/install-macos.sh").path
            guard FileManager.default.isExecutableFile(atPath: script) else {
                setupMessage = "Runtime installer is missing from this app bundle."
                setupInstalling = ""
                return
            }
            runAsync("/bin/bash", args: [script], timeout: 300, completion: done)
        }
    }

    private func checkStep(_ step: String) -> String {
        let fm = FileManager.default
        let home = NSHomeDirectory()
        switch step {
        case "OpenCode":
            let paths = ["\(home)/.opencode/bin/opencode", "\(home)/.local/bin/opencode", "/opt/homebrew/bin/opencode"]
            return paths.first { fm.isExecutableFile(atPath: $0) }.map { "OK — \($0)" } ?? "MISSING — install OpenCode"
        case "WARP":
            let paths = ["/usr/local/bin/warp-cli", "/opt/homebrew/bin/warp-cli"]
            return paths.first { fm.isExecutableFile(atPath: $0) }.map { "OK — \($0)" } ?? "MISSING — install Cloudflare WARP"
        case "App":
            let app = "/Applications/OpenHop.app"
            let payload = "\(app)/Contents/Resources/payload/install-macos.sh"
            return fm.fileExists(atPath: payload) ? "OK — \(app)" : "MISSING — run the DMG installer"
        default:
            let shims = ["\(home)/.local/bin/openhop", "\(home)/.local/bin/openhop-hop", "\(home)/.local/bin/openhop-doctor"]
            let venv = "\(home)/Library/Application Support/openhop/venv/bin/python"
            let plists = ["keeper", "guard"].allSatisfy { job in
                fm.fileExists(atPath: "\(home)/Library/LaunchAgents/com.openhop.\(job).plist")
            }
            return shims.allSatisfy({ fm.fileExists(atPath: $0) }) && fm.fileExists(atPath: venv) && plists
                ? "OK — CLI, venv and launchd jobs" : "MISSING — run the runtime installer"
        }
    }

    // MARK: - Local usage summary (never uploaded)

    struct UsageStats: Codable {
        var tokens = 0
        var messages = 0
        var chats = 0
        var rotationsToday = 0
        var rotationsAll = 0
        var goodIPs = 0
        var usedUp = 0
        var limits = 0
        var continues = 0
    }

    @Published var usage = UsageStats()
    @Published var usageText = "No rotations recorded yet"

    func loadUsage() {
        let script = (base as NSString).appendingPathComponent("scripts/usage-macos.sh")
        runAsync(script, args: [], timeout: 25) { r in
            guard r.ok, let data = r.output.data(using: .utf8),
                  let stats = try? JSONDecoder().decode(UsageStats.self, from: data) else { return }
            DispatchQueue.main.async {
                self.usage = stats
                self.usageText = "\(stats.rotationsToday) rotations today · \(stats.rotationsAll) all time · \(stats.usedUp) used-up addresses"
            }
        }
    }

    func stopGuardWatch() { guardTimer?.invalidate(); guardTimer = nil }

    /// Synchronous, minimal cleanup used only when the app is really quitting.
    func shutdownForQuit() {
        stopGuardWatch()
        let gui = "gui/\(getuid())"
        for job in ["keeper", "guard"] {
            _ = run("/bin/launchctl", args: ["bootout", "\(gui)/com.openhop.\(job)"], timeout: 15)
        }
        for name in ["warp-keeper-macos.sh", "zen_guard.py", "zen-hop-macos.sh"] {
            _ = run("/usr/bin/pkill", args: ["-f", name], timeout: 10)
        }
        _ = run("/usr/local/bin/warp-cli", args: ["--accept-tos", "disconnect"], timeout: 20)
    }

    func dismissToast() { toast = nil }

    // MARK: Events

    private var activityPath: String { (logDir as NSString).appendingPathComponent("activity.jsonl") }

    private func loadEvents() -> [ActivityEvent] {
        guard let text = try? String(contentsOfFile: activityPath, encoding: .utf8) else { return [] }
        return text.split(separator: "\n").compactMap { line in
            guard let data = line.data(using: .utf8) else { return nil }
            return try? JSONDecoder().decode(ActivityEvent.self, from: data)
        }.suffix(80).reversed().map { $0 }
    }

    private func emit(_ event: ActivityEvent) {
        events.insert(event, at: 0)
        if events.count > 80 { events.removeLast(events.count - 80) }
        if let data = try? JSONEncoder().encode(event), let line = String(data: data, encoding: .utf8) {
            try? FileManager.default.createDirectory(atPath: logDir, withIntermediateDirectories: true)
            if let h = FileHandle(forWritingAtPath: activityPath) {
                h.seekToEndOfFile(); h.write(Data((line + "\n").utf8)); h.closeFile()
            } else { try? (line + "\n").write(toFile: activityPath, atomically: true, encoding: .utf8) }
        }
    }

    private func emit(title: String, detail: String, symbol: String, tone: EventTone) {
        emit(ActivityEvent(id: UUID(), at: Date(), symbol: symbol, title: title, detail: detail, tone: tone))
    }

    /// Turns a coordinator progress line (which may contain raw details) into a short human event.
    @discardableResult
    private func emit(message: String, state: LifecycleState) -> String {
        let raw = message.trimmingCharacters(in: .whitespacesAndNewlines)
        let parts = raw.split(separator: ":", maxSplits: 1, omittingEmptySubsequences: false)
        let key = (parts.first.map(String.init) ?? raw).trimmingCharacters(in: .whitespaces).lowercased()
        let rest = parts.count > 1 ? String(parts[1]).trimmingCharacters(in: .whitespaces) : ""
        let detail = clean(rest.isEmpty ? raw : rest)

        let title: String
        let symbol: String
        var tone: EventTone = .info
        var finalDetail = detail

        switch key {
        case "starting starting":
            title = "Starting OpenHop"; symbol = "play.circle.fill"
        case "starting stopping":
            title = "Stopping OpenHop"; symbol = "stop.circle.fill"
        case "starting switching":
            title = "Getting a new IP"; symbol = "arrow.triangle.2.circlepath"
        case "inspect":
            title = "Current state checked"; symbol = "magnifyingglass"
        case "validate":
            title = "Components validated"; symbol = "checkmark.shield"
            tone = detail.contains("missing") ? .warn : .success
        case "warp":
            title = "WARP tunnel"; symbol = "network"; tone = toneFromDetail(detail)
        case "proxy":
            title = "Local proxy"; symbol = "network"; tone = toneFromDetail(detail)
        case "keeper":
            title = "Keeper service"; symbol = "gearshape.fill"; tone = toneFromDetail(detail)
        case "guard":
            title = "Guard service"; symbol = "shield.checkered"; tone = toneFromDetail(detail)
        case "lane":
            title = "OpenCode lane"; symbol = "desktopcomputer"; tone = toneFromDetail(detail)
        case "verify":
            title = "Verification finished"; symbol = "checkmark.circle.fill"; tone = toneFromDetail(detail)
        case "warp verify":
            title = "WARP connection verified"; symbol = "network"; tone = toneFromDetail(detail)
        case "managed processes":
            title = "Background tasks cleaned up"; symbol = "gearshape.fill"
            finalDetail = "OpenHop-owned tasks were signalled."
        case "snapshot":
            title = "State captured"; symbol = "clock"
        case "prerequisite":
            title = "Not ready to switch IP"; symbol = "exclamationmark.triangle.fill"
            tone = .warn; finalDetail = "Start OpenHop first, then try again."
        case "hop":
            title = "New IP requested"; symbol = "arrow.triangle.2.circlepath"
            finalDetail = "The hop service is preparing a fresh identity."
        case "new ip":
            title = "New IP acquired"; symbol = "network"; tone = .success
        case "state preserved":
            title = "State preserved"; symbol = "shield.checkered"
            finalDetail = "Keeper was returned to its previous state."
        case "reload keeper", "reload guard":
            title = "\(key == "reload keeper" ? "Keeper" : "Guard") reloaded"
            symbol = "arrow.triangle.2.circlepath"
            finalDetail = "The background job was restarted after the IP change."
        case "final health":
            title = "Health verified"; symbol = "heart.text.square"; tone = toneFromDetail(detail)
        case "cancellation requested":
            title = "Cancellation requested"; symbol = "hand.raised"
            tone = .warn; finalDetail = "The current operation is winding down."
        case "state reconciled":
            title = "Current state checked"; symbol = "checkmark.circle.fill"
            finalDetail = detail.isEmpty ? "System state is up to date." : detail
        default:
            if key.hasPrefix("keeper restart") || key.hasPrefix("guard restart") {
                let name = key.hasPrefix("keeper") ? "Keeper" : "Guard"
                title = "\(name) restarted"; symbol = "gearshape.fill"; tone = toneFromDetail(detail)
            } else {
                title = raw.isEmpty ? "Activity" : raw.capitalized
                symbol = "info.circle.fill"
                if detail.isEmpty { finalDetail = "Updated." }
            }
        }
        emit(title: title, detail: finalDetail.isEmpty ? "Updated." : finalDetail, symbol: symbol, tone: tone)
        return title
    }

    private func finishPendingOperation(_ report: OperationReport) {
        let summary = report.summary
        guard let kind = pendingOperation else {
            // Reconcile on launch is not a user action — don't add a new Activity event for it.
            if !summary.hasPrefix("State reconciled") {
                emit(title: "Current state checked", detail: summary, symbol: "checkmark.circle.fill", tone: .info)
            }
            return
        }
        pendingOperation = nil
        let tone: EventTone
        switch report.state {
        case .running, .stopped: tone = .success
        case .failed: tone = .error
        default: tone = .info
        }
        let title: String
        switch tone {
        case .success: title = "\(kind) completed"
        case .warn: title = "\(kind) needs attention"
        case .error: title = "\(kind) failed"
        case .info: title = "\(kind) finished"
        }
        let symbol = tone == .success ? "checkmark.circle.fill" : (tone == .warn ? "exclamationmark.triangle.fill" : "xmark.circle.fill")
        emit(title: title, detail: summary, symbol: symbol, tone: tone)
        announce(title, summary, tone)
    }

    private func handleRestartResult(_ message: String) {
        auxiliaryBusy = false
        let name = message.hasPrefix("guard") ? "Guard" : "Keeper"
        let ok = message.contains("exit 0")
        announce(ok ? "\(name) restarted" : "\(name) could not restart",
                 ok ? "The background job is running again." : "Try again from Services.",
                 ok ? .success : .error)
    }

    // MARK: Toasts

    private func announce(_ title: String, _ detail: String, _ tone: EventTone) {
        let t = AppToast(id: UUID(), title: title, detail: detail, tone: tone)
        toast = t
        DispatchQueue.main.asyncAfter(deadline: .now() + 4) { [weak self] in
            guard self?.toast?.id == t.id else { return }
            self?.toast = nil
        }
    }

    // MARK: Structured health checks

    private func parseDoctor(_ output: String) -> [HealthCheck] {
        var rows: [String: HealthCheck] = [
            "keeper": HealthCheck(id: "keeper", label: "Keeper service",
                                  detail: keeper == "running" ? "Background service is running" : "Background service is stopped",
                                  ok: keeper == "running"),
            "guard": HealthCheck(id: "guard", label: "Guard service",
                                 detail: guardJob == "running" ? "Background service is running" : "Background service is stopped",
                                 ok: guardJob == "running"),
            "warp": HealthCheck(id: "warp", label: "WARP tunnel",
                                detail: warp == "connected" ? "Tunnel is connected" : "Tunnel is disconnected",
                                ok: warp == "connected"),
            "egress": HealthCheck(id: "egress", label: "Egress/IP",
                                  detail: ip == "—" ? "No WARP egress" : "Public IP \(ip)", ok: ip != "—"),
            "lane": HealthCheck(id: "lane", label: "OpenCode lane",
                                detail: laneStatus == "ready" ? "OpenCode uses the macOS lane" : "Lane has not been checked yet",
                                ok: laneStatus == "ready")
        ]
        for raw in output.split(separator: "\n") {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            guard !line.isEmpty else { continue }
            let ok = line.hasPrefix("✓")
            let warning = line.hasPrefix("!")
            let body = String(line.dropFirst()).trimmingCharacters(in: .whitespaces)
            let l = body.lowercased()
            if body.isEmpty { continue }
            if l.contains("egress") {
                let address = body.components(separatedBy: ": ").last?.trimmingCharacters(in: .whitespaces) ?? ""
                rows["egress"] = HealthCheck(id: "egress", label: "Egress/IP",
                                             detail: ok && !address.isEmpty ? "Public IP \(address)" : "No WARP egress",
                                             ok: ok, warning: warning)
            } else if l.contains("opencode") {
                rows["lane"] = HealthCheck(id: "lane", label: "OpenCode lane",
                                           detail: ok ? "OpenCode uses the macOS lane" : "Lane is not configured",
                                           ok: ok, warning: warning)
            } else if l.contains("keeper") {
                rows["keeper"] = HealthCheck(id: "keeper", label: "Keeper service",
                                             detail: ok ? "Background service is running" : "Background service is stopped",
                                             ok: ok, warning: warning)
            } else if l.contains("guard") {
                rows["guard"] = HealthCheck(id: "guard", label: "Guard service",
                                            detail: ok ? "Background service is running" : "Background service is stopped",
                                            ok: ok, warning: warning)
            } else if l.contains("warp") {
                rows["warp"] = HealthCheck(id: "warp", label: "WARP tunnel",
                                           detail: ok ? "Tunnel is connected" : "Tunnel is disconnected",
                                           ok: ok, warning: warning)
            }
        }
        return ["keeper", "guard", "warp", "egress", "lane"].compactMap { rows[$0] }
    }

    /// Live launchd/WARP state wins over any cached doctor text for the service rows.
    private func syncHealthWithLiveState() {
        for i in healthChecks.indices {
            switch healthChecks[i].id {
            case "keeper":
                healthChecks[i].ok = keeper == "running"
                healthChecks[i].detail = keeper == "running" ? "Background service is running" : "Background service is stopped"
                healthChecks[i].warning = false
            case "guard":
                healthChecks[i].ok = guardJob == "running"
                healthChecks[i].detail = guardJob == "running" ? "Background service is running" : "Background service is stopped"
                healthChecks[i].warning = false
            case "warp":
                healthChecks[i].ok = warp == "connected"
                healthChecks[i].detail = warp == "connected" ? "Tunnel is connected" : "Tunnel is disconnected"
                healthChecks[i].warning = false
            default: break
            }
        }
    }

    // Last-resort display boundary: strip raw paths, exit codes and command-shaped tokens.
    private func clean(_ text: String) -> String {
        var s = text
        let pairs: [(String, String)] = [
            ("exit 0", "completed"),
            ("bootstrap", "started"),
            ("bootout", "stopped"),
            ("kickstart", "restarted"),
            ("127.0.0.1:40000 listening", "proxy is listening"),
            ("listener not found", "proxy is not listening"),
            ("executables and plists present", "required components are present"),
            ("opencode shim does not reference zen-lane-macos", "OpenCode lane is not configured"),
            ("opencode -> macOS lane", "OpenCode uses the macOS lane"),
            ("OpenHop-owned processes signalled", "background tasks cleaned up"),
            ("keeper was started by hop EXIT trap; stopped", "keeper was returned to its previous state"),
            ("lifecycle=running", "system running"),
            ("lifecycle=partial", "system partly running"),
            ("lifecycle=stopped", "system stopped"),
            ("keeper=true", "keeper running"), ("keeper=false", "keeper stopped"),
            ("guard=true", "guard running"), ("guard=false", "guard stopped"),
            ("warp=true", "WARP connected"), ("warp=false", "WARP disconnected"),
            ("proxy=true", "proxy listening"), ("proxy=false", "proxy not listening"),
            ("egress=", "IP "), ("IP none", "no public IP")
        ]
        for (a, b) in pairs { s = s.replacingOccurrences(of: a, with: b) }
        s = s.replacingOccurrences(of: #"exit\s+-?\d+"#, with: "did not complete", options: .regularExpression)
        s = s.replacingOccurrences(of: #"/[A-Za-z0-9._~/\-]+"#, with: "", options: .regularExpression)
        return s.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func toneFromDetail(_ detail: String) -> EventTone {
        let d = detail.lowercased()
        let trouble = ["missing", "not ", "no ", "did not", "stopped", "failed", "incomplete",
                       "disconnected", "cannot", "not listening", "wrong"]
        return trouble.contains { d.contains($0) } ? .warn : .success
    }

    private func applyDoctor(_ output: String) {
        doctorOutput = output
        healthChecks = parseDoctor(output)
        syncHealthWithLiveState()
        var foundIP: String?
        var bad: [String] = []
        for raw in output.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            if line.hasPrefix("✗") { bad.append(line) }
            if line.contains("egress is cloudflare"), let c = line.range(of: ")") {
                let ipText = String(line[line.index(after: c.lowerBound)...]).trimmingCharacters(in: CharacterSet(charactersIn: ": \t"))
                if !ipText.isEmpty { foundIP = ipText }
            }
        }
        ip = foundIP ?? "—"
        if let lane = healthChecks.first(where: { $0.id == "lane" }) { laneStatus = lane.ok ? "ready" : "not ready" }
        if bad.isEmpty && foundIP != nil { health = "EVERYTHING IS WORKING" }
        else if foundIP != nil { health = "DEGRADED" }
        else {
            health = "NOT CONNECTED"
            if operationState == .running { operationState = .stopped }
        }
    }

    /// Publish verified facts from the coordinator immediately — no stale doctor result wins.
    private func applyReport(_ r: OperationReport) {
        ip = r.egressIP ?? "—"
        warp = r.warpConnected ? "connected" : "disconnected"
        keeper = r.keeperRunning ? "running" : "stopped"
        guardJob = r.guardRunning ? "running" : "stopped"
        if let lane = r.steps.first(where: { $0.name == "Lane" }) { laneStatus = lane.ok ? "ready" : "not ready" }
        if !r.doctorOutput.isEmpty {
            doctorOutput = r.doctorOutput
            healthChecks = parseDoctor(r.doctorOutput)
        }
        if r.state == .stopped {
            health = "NOT CONNECTED"
        } else if r.state == .running {
            health = r.egressIP == nil ? "NOT CONNECTED" : (r.state == .running ? "EVERYTHING IS WORKING" : "DEGRADED")
        }
    }

    func refreshStates() {
        DispatchQueue.global(qos: .utility).async {
            let gui = "gui/\(getuid())"
            var states: [String] = []
            for job in ["keeper", "guard"] {
                let a = self.run("/bin/launchctl", args: ["print", "\(gui)/com.openhop.\(job)"], timeout: 8)
                let b = self.run("/bin/launchctl", args: ["list", "com.openhop.\(job)"], timeout: 8)
                states.append(a.ok && b.ok ? "running" : "stopped")
            }
            let w = self.run("/usr/local/bin/warp-cli", args: ["status"], timeout: 10)
            let line = w.output.split(separator: "\n").first { $0.contains("Status update:") } ?? ""
            DispatchQueue.main.async {
                self.keeper = states[0]; self.guardJob = states[1]
                self.warp = line.contains("Status update: Connected") ? "connected" : "disconnected"
                self.syncHealthWithLiveState()
            }
        }
    }

    // MARK: Secondary utility (not a lifecycle operation)

    func copyIP() {
        guard ip != "—" else { return }
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(ip, forType: .string)
    }

    func copyLog() {
        let text = records.map { "\($0.started) | \($0.command) \($0.args.joined(separator: " ")) | exit \($0.exitCode)\n\($0.output)" }.joined(separator: "\n---\n")
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(text, forType: .string)
    }

    func exportSupportBundle() {
        let zip = (logDir as NSString).appendingPathComponent("openhop-support.zip")
        let tmp = (logDir as NSString).appendingPathComponent("support-bundle")
        try? FileManager.default.createDirectory(atPath: tmp, withIntermediateDirectories: true)
        try? doctorOutput.write(toFile: (tmp as NSString).appendingPathComponent("doctor.txt"), atomically: true, encoding: .utf8)
        let lines = records.map { "\($0.started) | \($0.command) \($0.args.joined(separator: " ")) | exit \($0.exitCode)\n\($0.output)\n---" }.joined(separator: "\n")
        try? lines.write(toFile: (tmp as NSString).appendingPathComponent("commands.txt"), atomically: true, encoding: .utf8)
        let task = Process(); task.executableURL = URL(fileURLWithPath: "/usr/bin/zip")
        task.arguments = ["-q", "-r", zip, tmp]; try? task.run(); task.waitUntilExit()
        recordNote("support bundle: \(zip)")
    }
}

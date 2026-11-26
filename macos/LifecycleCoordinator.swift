import Foundation

struct CommandOutcome {
    let ok: Bool
    let exitCode: Int32
    let output: String
}

protocol CommandExecuting: AnyObject {
    func run(_ executable: String, _ args: [String], _ timeout: TimeInterval) -> CommandOutcome
}

enum LifecycleState: String {
    case unknown, stopped, starting, running, stopping, switching, failed
}

struct OperationStep {
    var name: String
    var detail: String
    var ok: Bool
}

struct OperationReport {
    var state: LifecycleState
    var steps: [OperationStep]
    var summary: String
    var egressIP: String?
    var warpConnected = false
    var keeperRunning = false
    var guardRunning = false
    var proxyListening = false
    var doctorOutput = ""
    var ok: Bool { state == .running || state == .stopped }
}

private struct Snapshot {
    var keeperRunning = false
    var guardRunning = false
    var warpConnected = false
    var egressIP: String?
    var proxyListening = false
    var laneConfigured = false
    var doctorBad: [String] = []
    var doctorRaw = ""

    var lifecycle: LifecycleState {
        if warpConnected || egressIP != nil || keeperRunning || guardRunning {
            return .running
        }
        return .stopped
    }
}

/// The single owner of Start, Stop and Switch IP. All other code must call these three methods.
final class LifecycleCoordinator {
    private let runner: CommandExecuting
    private let lock = NSLock()
    private var cancelled = false
    private(set) var state: LifecycleState = .unknown
    var onProgress: ((LifecycleState, String, OperationReport?) -> Void)?

    private let home = NSHomeDirectory()
    private var warp: String { "/usr/local/bin/warp-cli" }
    private var doctor: String { (NSHomeDirectory() as NSString).appendingPathComponent(".local/bin/openhop-doctor") }
    private var hop: String { (NSHomeDirectory() as NSString).appendingPathComponent(".local/bin/openhop-hop") }
    private var lane: String { (NSHomeDirectory() as NSString).appendingPathComponent(".local/bin/opencode") }
    private var plist: (String) -> String {
        { (NSHomeDirectory() as NSString).appendingPathComponent("Library/LaunchAgents/com.openhop.\($0).plist") }
    }
    private var gui: String { "gui/\(getuid())" }

    init(runner: CommandExecuting) { self.runner = runner }

    // MARK: Guarded entry points

    private func begin(_ newState: LifecycleState) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        guard state != .starting, state != .stopping, state != .switching else { return false }
        cancelled = false
        state = newState
        onProgress?(state, "Starting \(newState.rawValue)…", nil)
        return true
    }
    private func report(_ state: LifecycleState, _ steps: [OperationStep], _ summary: String, _ s: Snapshot) -> OperationReport {
        OperationReport(state: state, steps: steps, summary: summary, egressIP: s.egressIP,
                        warpConnected: s.warpConnected, keeperRunning: s.keeperRunning,
                        guardRunning: s.guardRunning, proxyListening: s.proxyListening,
                        doctorOutput: s.doctorRaw)
    }

    private func finish(_ report: OperationReport) {
        lock.lock(); state = report.state; lock.unlock()
        onProgress?(state, report.summary, report)
    }
    /// Establish the real system state at launch so the UI never guesses.
    func reconcile() {
        DispatchQueue.global(qos: .utility).async {
            let s = self.probe()
            DispatchQueue.main.async {
                self.lock.lock(); self.state = s.lifecycle; self.lock.unlock()
                let r = self.report(s.lifecycle, [], "State reconciled: \(s.lifecycle.rawValue)", s)
                self.onProgress?(self.state, r.summary, r)
            }
        }
    }

    func cancel() {
        lock.lock(); cancelled = true; lock.unlock()
        onProgress?(state, "Cancellation requested", nil)
    }
    private func isCancelled() -> Bool { lock.lock(); defer { lock.unlock() }; return cancelled }

    // MARK: Probes

    private func probe() -> Snapshot {
        var s = Snapshot()
        for (job, key) in [("keeper", \Snapshot.keeperRunning), ("guard", \Snapshot.guardRunning)] {
            let print = runner.run("/bin/launchctl", ["print", "\(gui)/com.openhop.\(job)"], 10)
            let list = runner.run("/bin/launchctl", ["list", "com.openhop.\(job)"], 10)
            let running = print.ok && list.ok
            if job == "keeper" { s.keeperRunning = running } else { s.guardRunning = running }
        }
        let w = runner.run(warp, ["status"], 10)
        let statusLine = w.output.split(separator: "\n").first { $0.contains("Status update:") } ?? ""
        s.warpConnected = statusLine.contains("Status update: Connected")
        let d = runner.run(doctor, [], 60)
        s.doctorRaw = d.output
        for line in d.output.split(separator: "\n") {
            let t = String(line).trimmingCharacters(in: .whitespaces)
            if t.hasPrefix("✗") { s.doctorBad.append(t) }
            if t.contains("egress is cloudflare"), let c = t.range(of: ")") {
                let ip = String(t[t.index(after: c.lowerBound)...]).trimmingCharacters(in: CharacterSet(charactersIn: ": \t"))
                if !ip.isEmpty { s.egressIP = ip }
            }
        }
        s.proxyListening = runner.run("/usr/bin/nc", ["-z", "-G", "1", "127.0.0.1", "40000"], 3).ok
        s.laneConfigured = runner.run("/usr/bin/grep", ["-q", "zen-lane-macos", lane], 10).ok
        return s
    }

    /// Direct egress check with retries — the doctor's curl line can be transient.
    private func egressIP(_ attempts: Int = 3) -> String? {
        for i in 0..<attempts {
            let r = runner.run("/usr/bin/curl", ["-x", "http://127.0.0.1:40000", "-s", "-m", "10",
                                                "https://cloudflare.com/cdn-cgi/trace"], 15)
            for line in r.output.split(separator: "\n") where line.hasPrefix("ip=") {
                let ip = String(line.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                if !ip.isEmpty { return ip }
            }
            if i < attempts - 1 { Thread.sleep(forTimeInterval: 2) }
        }
        return nil
    }

    /// Cheap state check for polling — no doctor / curl, so the UI stays responsive.
    private func quickProbe() -> Snapshot {
        var s = Snapshot()
        for job in ["keeper", "guard"] {
            let print = runner.run("/bin/launchctl", ["print", "\(gui)/com.openhop.\(job)"], 8)
            let list = runner.run("/bin/launchctl", ["list", "com.openhop.\(job)"], 8)
            let running = print.ok && list.ok
            if job == "keeper" { s.keeperRunning = running } else { s.guardRunning = running }
        }
        let w = runner.run(warp, ["status"], 8)
        let line = w.output.split(separator: "\n").first { $0.contains("Status update:") } ?? ""
        s.warpConnected = line.contains("Status update: Connected")
        s.proxyListening = runner.run("/usr/bin/nc", ["-z", "-G", "1", "127.0.0.1", "40000"], 3).ok
        return s
    }

    private func step(_ name: String, _ detail: String, _ ok: Bool, _ steps: inout [OperationStep]) {
        steps.append(OperationStep(name: name, detail: detail, ok: ok))
        onProgress?(state, "Step \(steps.count): \(name) — \(detail)", nil)
    }

    // MARK: Start / ensureHealthy

    func start() {
        guard begin(.starting) else { return }
        DispatchQueue.global(qos: .userInitiated).async { self.performStart() }
    }

    private func performStart() {
        var steps: [OperationStep] = []
        let before = probe()
        step("Inspect", "keeper=\(before.keeperRunning) guard=\(before.guardRunning) warp=\(before.warpConnected) egress=\(before.egressIP ?? "none")", true, &steps)

        let required = [warp, doctor, hop, lane, plist("keeper"), plist("guard")]
        let missing = required.filter { !FileManager.default.fileExists(atPath: $0) }
        step("Validate", missing.isEmpty ? "executables and plists present" : "missing: \(missing.joined(separator: ", "))", missing.isEmpty, &steps)
        if !missing.isEmpty { finish(OperationReport(state: .failed, steps: steps, summary: "Start failed: missing files")); return }

        var s = before
        if !s.warpConnected || s.egressIP == nil {
            _ = runner.run(warp, ["mode", "proxy"], 30)
            _ = runner.run(warp, ["proxy", "port", "40000"], 30)
            let c = runner.run(warp, ["connect"], 60)
            step("WARP", "connect exit \(c.exitCode)", c.ok, &steps)
            s = probe()
        } else { step("WARP", "already connected", true, &steps) }

        if !s.proxyListening {
            _ = runner.run(warp, ["mode", "proxy"], 30)
            _ = runner.run(warp, ["proxy", "port", "40000"], 30)
            _ = runner.run(warp, ["connect"], 60)
            s = probe()
        }
        step("Proxy", s.proxyListening ? "127.0.0.1:40000 listening" : "listener not found", s.proxyListening, &steps)

        for job in ["keeper", "guard"] {
            let wasRunning = job == "keeper" ? s.keeperRunning : s.guardRunning
            if !wasRunning {
                let r = runner.run("/bin/launchctl", ["bootstrap", gui, plist(job)], 20)
                step(job, "bootstrap exit \(r.exitCode)", r.ok, &steps)
            } else { step(job, "already running", true, &steps) }
        }

        if !s.laneConfigured { step("Lane", "opencode shim does not reference zen-lane-macos", false, &steps) }
        else { step("Lane", "opencode -> macOS lane", true, &steps) }

        if isCancelled() { finish(OperationReport(state: .stopped, steps: steps, summary: "Start cancelled")); return }
        var quick = quickProbe()
        for _ in 0..<8 {
            if quick.keeperRunning && quick.guardRunning && quick.warpConnected && quick.proxyListening { break }
            Thread.sleep(forTimeInterval: 2)
            quick = quickProbe()
        }
        var final = probe()
        if final.egressIP == nil { final.egressIP = egressIP() }
        let coreOK = { (s: Snapshot) in
            s.keeperRunning && s.guardRunning && s.warpConnected && s.proxyListening && s.egressIP != nil && s.laneConfigured
        }
        // A doctor line can be transient while launchd jobs settle; re-check before failing.
        if !coreOK(final) || !final.doctorBad.isEmpty {
            Thread.sleep(forTimeInterval: 3)
            final = probe()
            if final.egressIP == nil { final.egressIP = egressIP() }
        }
        let healthy = coreOK(final)
        step("Verify", "egress=\(final.egressIP ?? "none") keeper=\(final.keeperRunning) guard=\(final.guardRunning) proxy=\(final.proxyListening)", healthy, &steps)
        finish(report(healthy ? .running : .stopped, steps,
                      healthy ? "Started — all components verified" : "Start incomplete — see failed steps", final))
    }

    // MARK: Stop / stopEverything

    func stop() {
        guard begin(.stopping) else { return }
        DispatchQueue.global(qos: .userInitiated).async { self.performStop() }
    }

    private func performStop() {
        var steps: [OperationStep] = []
        for job in ["keeper", "guard"] {
            let r = runner.run("/bin/launchctl", ["bootout", "\(gui)/com.openhop.\(job)"], 20)
            // "No such process" means the job is already stopped — that is the desired end state.
            let alreadyStopped = r.output.localizedCaseInsensitiveContains("no such process")
            step(job, alreadyStopped ? "already stopped" : "bootout exit \(r.exitCode)", r.ok || alreadyStopped, &steps)
        }
        // Only OpenHop-owned child processes, matched by exact script/module names.
        let owned = ["warp-keeper-macos.sh", "zen_guard.py", "zen-hop-macos.sh"]
        for name in owned {
            let p = runner.run("/usr/bin/pgrep", ["-f", name], 10)
            if p.ok {
                for pidStr in p.output.split(whereSeparator: { $0 == "\n" || $0 == " " }) {
                    let pid = String(pidStr)
                    if !pid.isEmpty, Int(pid) != nil {
                        _ = runner.run("/bin/kill", [pid], 10)
                    }
                }
            }
        }
        step("Managed processes", "OpenHop-owned processes signalled", true, &steps)
        let d = runner.run(warp, ["disconnect"], 30)
        step("WARP", "disconnect exit \(d.exitCode)", d.ok, &steps)
        // WARP's daemon can take a few seconds to report the new state.
        var disconnected = false
        for _ in 0..<6 {
            let s = runner.run(warp, ["status"], 8)
            let line = s.output.split(separator: "\n").first { $0.contains("Status update:") } ?? ""
            if !line.contains("Status update: Connected") { disconnected = true; break }
            Thread.sleep(forTimeInterval: 1)
        }
        step("WARP verify", disconnected ? "disconnected" : "still connected after wait", disconnected, &steps)

        let final = probe()
        let allStopped = !final.keeperRunning && !final.guardRunning && !final.warpConnected
        step("Verify", "keeper=\(final.keeperRunning) guard=\(final.guardRunning) warp=\(final.warpConnected)", allStopped, &steps)
        let state: LifecycleState = allStopped ? .stopped : .running
        finish(report(state, steps,
                      allStopped ? "Stopped — all components verified" : "Stop incomplete — see failed steps", final))
    }

    // MARK: Switch IP / switchIPAndRefresh

    func switchIP() {
        guard begin(.switching) else { return }
        DispatchQueue.global(qos: .userInitiated).async { self.performSwitchIP() }
    }

    private func performSwitchIP() {
        var steps: [OperationStep] = []
        let before = probe()
        let wasRunning = before.lifecycle == .running
        step("Snapshot", "lifecycle=\(before.lifecycle.rawValue) egress=\(before.egressIP ?? "none")", true, &steps)

        guard before.warpConnected, let oldIP = before.egressIP, before.proxyListening else {
            let msg = "Cannot switch IP: WARP/proxy prerequisite missing. Start first. Lifecycle unchanged."
            step("Prerequisite", msg, false, &steps)
            finish(report(before.lifecycle, steps, msg, before))
            return
        }

        let hopResult = runner.run(hop, [], 180)
        step("Hop", "exit \(hopResult.exitCode)", hopResult.ok, &steps)
        if !hopResult.ok {
            let post = probe()
            step("Verify", "egress=\(post.egressIP ?? "none")", false, &steps)
            finish(report(wasRunning ? .running : .stopped, steps, "Switch failed; state preserved", post))
            return
        }

        var newIP: String?
        for _ in 0..<4 {
            if isCancelled() { break }
            let p = probe()
            if let ip = p.egressIP, ip != oldIP { newIP = ip; break }
            Thread.sleep(forTimeInterval: 2)
        }
        let afterHop = probe()
        let changed = newIP != nil
        step("New IP", "\(oldIP) → \(newIP ?? afterHop.egressIP ?? "none")", changed, &steps)

        // zen-hop-macos.sh always kickstarts keeper on EXIT. Undo that when we were stopped.
        if !wasRunning && afterHop.keeperRunning {
            let r = runner.run("/bin/launchctl", ["bootout", "\(gui)/com.openhop.keeper"], 20)
            step("State preserved", "keeper was started by hop EXIT trap; bootout exit \(r.exitCode)", r.ok, &steps)
        }
        if wasRunning {
            // Reload dependants in dependency order after the network change.
            for job in ["keeper", "guard"] {
                let r = runner.run("/bin/launchctl", ["kickstart", "-k", "\(gui)/com.openhop.\(job)"], 20)
                step("Reload \(job)", "kickstart exit \(r.exitCode)", r.ok, &steps)
            }
        }

        let final = probe()
        let ok = changed && final.warpConnected && final.proxyListening && final.doctorBad.isEmpty
        step("Final health", "egress=\(final.egressIP ?? "none") warp=\(final.warpConnected) proxy=\(final.proxyListening)", ok, &steps)
        let state: LifecycleState = ok ? .running : (wasRunning ? .running : .stopped)
        finish(report(state, steps, ok ? "IP switched and dependants refreshed" : "Switch incomplete — see failed steps", final))
    }

    // MARK: Diagnostic-only actions

    func restartJob(_ job: String) {
        let r = runner.run("/bin/launchctl", ["kickstart", "-k", "\(gui)/com.openhop.\(job)"], 20)
        onProgress?(state, "\(job) restart exit \(r.exitCode)", nil)
    }
}

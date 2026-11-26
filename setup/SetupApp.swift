import AppKit
import SwiftUI

private enum SetupBrand {
    static let bg = Color(hex: 0x151619)
    static let surface = Color(hex: 0x1D1F23)
    static let text = Color(hex: 0xECEBE4)
    static let muted = Color(hex: 0xA8A59B)
    static let amber = Color(hex: 0xF2B318)
    static let green = Color(hex: 0x3FB97A)
    static let bad = Color(hex: 0xE5534B)
}
private extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}

private struct CommandResult {
    let ok: Bool
    let output: String
}

final class SetupModel: ObservableObject {
    @Published var opencode = ""
    @Published var warp = ""
    @Published var python = ""
    @Published var app = ""
    @Published var runtime = ""
    @Published var message = "Check each step, then install what is missing."

    private var sourceApp: URL {
        (Bundle.main.resourceURL ?? Bundle.main.bundleURL).appendingPathComponent("OpenHop.app")
    }
    private var targetApp: URL { URL(fileURLWithPath: "/Applications/OpenHop.app") }
    private var home: String { NSHomeDirectory() }

    private func run(_ exe: String, _ args: [String] = [], timeout: TimeInterval = 60) -> CommandResult {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: exe)
        p.arguments = args
        let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
        do {
            try p.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            return CommandResult(ok: p.terminationStatus == 0,
                                 output: String(data: data, encoding: .utf8) ?? "")
        } catch {
            return CommandResult(ok: false, output: error.localizedDescription)
        }
    }

    func check() {
        let oc = ["\(home)/.opencode/bin/opencode", "\(home)/.local/bin/opencode", "/opt/homebrew/bin/opencode"]
        opencode = oc.first { FileManager.default.isExecutableFile(atPath: $0) }.map { "OK — \($0)" } ?? "MISSING"
        let wc = ["/usr/local/bin/warp-cli", "/opt/homebrew/bin/warp-cli"]
        warp = wc.first { FileManager.default.isExecutableFile(atPath: $0) }.map { "OK — \($0)" } ?? "MISSING"
        python = run("/usr/bin/which", ["python3"]).ok ? "OK" : "MISSING"
        app = FileManager.default.fileExists(atPath: targetApp.path) ? "OK — /Applications/OpenHop.app" : "NOT INSTALLED"
        let script = targetApp.appendingPathComponent("Contents/Resources/payload/install-macos.sh").path
        runtime = FileManager.default.fileExists(atPath: script) ? "READY" : "NEEDS APP FIRST"
    }

    func installOpenCode() {
        message = "Running the official OpenCode installer…"
        let r = run("/bin/bash", ["-c", "curl -fsSL https://opencode.ai/install | bash"], timeout: 300)
        message = r.ok ? "OpenCode installed." : "Install failed: \(r.output.prefix(160))"
        check()
    }

    func installWarp() {
        message = "Installing Cloudflare WARP with Homebrew…"
        let brew = "/opt/homebrew/bin/brew"
        guard FileManager.default.isExecutableFile(atPath: brew) else {
            message = "Homebrew not found — install it from https://brew.sh first."
            return
        }
        let r = run(brew, ["install", "--cask", "cloudflare-warp"], timeout: 600)
        message = r.ok ? "WARP installed — open it once to accept its terms." : "Install failed: \(r.output.prefix(160))"
        check()
    }

    func installApp() {
        guard FileManager.default.fileExists(atPath: sourceApp.path) else {
            message = "OpenHop.app is missing from this disk image."; return
        }
        message = "Copying OpenHop.app to Applications…"
        _ = run("/bin/rm", ["-rf", targetApp.path])
        let r = run("/bin/cp", ["-R", sourceApp.path, targetApp.path])
        message = r.ok ? "App installed. Now run the runtime step." : "Copy failed: \(r.output.prefix(160))"
        check()
    }

    func installRuntime() {
        let script = targetApp.appendingPathComponent("Contents/Resources/payload/install-macos.sh").path
        guard FileManager.default.fileExists(atPath: script) else {
            message = "Install the app first."; return
        }
        message = "Installing runtime, CLI and background jobs…"
        let r = run("/bin/bash", [script], timeout: 300)
        message = r.ok ? "Setup complete. You can quit this installer and open OpenHop." : "Setup failed — see the output in Terminal. \(r.output.prefix(160))"
        check()
    }
}

private struct Row: View {
    let number: String
    let title: String
    let subtitle: String
    let status: String
    let button: String
    let action: () -> Void

    private var ok: Bool { status.hasPrefix("OK") || status.hasPrefix("READY") }

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Text(number).font(.system(size: 11, weight: .bold)).foregroundStyle(SetupBrand.amber)
                .frame(width: 18, alignment: .leading)
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.system(size: 14, weight: .semibold)).foregroundStyle(SetupBrand.text)
                Text(subtitle).font(.system(size: 11)).foregroundStyle(SetupBrand.muted)
                Text(status).font(.system(size: 11, weight: .semibold))
                    .foregroundStyle(ok ? SetupBrand.green : SetupBrand.bad)
            }
            Spacer()
            Button(button, action: action).buttonStyle(.borderedProminent).tint(SetupBrand.amber)
        }
        .padding(14)
        .background(SetupBrand.surface, in: RoundedRectangle(cornerRadius: 10))
    }
}

private struct SetupView: View {
    @EnvironmentObject var model: SetupModel

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("OpenHop Setup").font(.system(size: 22, weight: .bold)).foregroundStyle(SetupBrand.text)
            Text("Check each step. Nothing installs until you press its button.")
                .font(.system(size: 12)).foregroundStyle(SetupBrand.muted)
            Row(number: "1", title: "OpenCode CLI", subtitle: "Needed to run OpenCode through the protected lane",
                status: model.opencode, button: "Install OpenCode") { model.installOpenCode() }
            Row(number: "2", title: "Cloudflare WARP", subtitle: "Free 1.1.1.1 app — open it once to accept terms",
                status: model.warp, button: "Install WARP") { model.installWarp() }
            Row(number: "3", title: "Control center app", subtitle: "Copy OpenHop.app into /Applications",
                status: model.app, button: "Install app") { model.installApp() }
            Row(number: "4", title: "Runtime and background jobs", subtitle: "CLI shims, venv, WARP proxy mode, keeper and guard",
                status: model.runtime, button: "Install runtime") { model.installRuntime() }
            Text(model.message).font(.system(size: 11)).foregroundStyle(SetupBrand.amber)
            HStack {
                Button("Re-check") { model.check() }.buttonStyle(.bordered)
                Spacer()
                Button("Quit") { NSApp.terminate(nil) }.buttonStyle(.bordered)
            }
        }
        .padding(24)
        .frame(width: 720)
        .background(SetupBrand.bg)
        .onAppear { model.check() }
    }
}

final class SetupDelegate: NSObject, NSApplicationDelegate {
    private let model = SetupModel()
    private var window: NSWindow?

    func applicationDidFinishLaunching(_ notification: Notification) {
        let hosting = NSHostingController(rootView: SetupView().environmentObject(model))
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 720, height: 620),
                         styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "OpenHop Setup"
        w.contentViewController = hosting
        w.center(); w.makeKeyAndOrderFront(nil)
        window = w
        NSApp.activate(ignoringOtherApps: true)
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }
}

@main
struct SetupApp: App {
    @NSApplicationDelegateAdaptor(SetupDelegate.self) private var delegate
    var body: some Scene { Settings { EmptyView() } }
}

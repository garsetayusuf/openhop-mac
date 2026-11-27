import AppKit
import SwiftUI
import Combine

// MARK: - Official openhop palette

private enum Brand {
    static let bg = Color(hex: 0x151619)
    static let deep = Color(hex: 0x101113)
    static let surface = Color(hex: 0x1D1F23)
    static let text = Color(hex: 0xECEBE4)
    static let muted = Color(hex: 0xA8A59B)
    static let amber = Color(hex: 0xF2B318)
    static let green = Color(hex: 0x3FB97A)
    static let warn = Color(hex: 0xF07D3A)
    static let bad = Color(hex: 0xE5534B)
}

private struct PointingCursor: ViewModifier {
    func body(content: Content) -> some View {
        content.onHover { inside in
            if inside { NSCursor.pointingHand.set() } else { NSCursor.arrow.set() }
        }
    }
}
private extension View {
    func pointingCursor() -> some View { modifier(PointingCursor()) }
}

private extension Color {
    init(hex: UInt32) {
        self.init(red: Double((hex >> 16) & 0xFF) / 255,
                  green: Double((hex >> 8) & 0xFF) / 255,
                  blue: Double(hex & 0xFF) / 255)
    }
}

// MARK: - App shell (unchanged AppKit hosting)

fileprivate final class SidebarStore: ObservableObject {
    @Published var section = 0
}

private func openhopMenuBarImage(dot: NSColor? = nil) -> NSImage {
    let image: NSImage
    if let url = Bundle.main.url(forResource: "openhop", withExtension: "icns"),
       let logo = NSImage(contentsOf: url) {
        image = logo
    } else {
        image = NSImage(systemSymbolName: "network", accessibilityDescription: "OpenHop") ?? NSImage()
    }
    image.size = NSSize(width: 18, height: 18)
    image.isTemplate = false
    guard let dot else { return image }
    let composed = NSImage(size: NSSize(width: 18, height: 18))
    composed.lockFocus()
    image.draw(in: NSRect(x: 0, y: 0, width: 18, height: 18))
    dot.setFill()
    NSBezierPath(ovalIn: NSRect(x: 11, y: 1, width: 7, height: 7)).fill()
    composed.unlockFocus()
    return composed
}

@main
struct OpenHopApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    var body: some Scene { Settings { EmptyView() } }
}

@objc(OpenHopApplication)
final class OpenHopApplication: NSApplication {
    override func terminate(_ sender: Any?) {
        if let d = delegate as? AppDelegate, !d.explicitQuit {
            d.window?.orderOut(nil)
            return
        }
        super.terminate(sender)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate, NSWindowDelegate, NSMenuDelegate, ObservableObject {
    fileprivate var window: NSWindow?
    fileprivate var setupWindow: NSWindow?
    private var statusItem: NSStatusItem?
    private var setupDone = false
    private let setupCompletedKey = "openhop.setupCompleted"
    fileprivate var explicitQuit = false
    let model = DashboardModel.shared
    fileprivate let store = SidebarStore()

    func applicationDidFinishLaunching(_ notification: Notification) {
        buildMainMenu()
        setupStatusItem()
        model.checkSetup()
        if UserDefaults.standard.bool(forKey: setupCompletedKey) {
            setupDone = true
            buildWindow()
        } else {
            buildSetupWindow()
        }
        model.setupNotifications()
        NotificationCenter.default.addObserver(self, selector: #selector(openFromMenu), name: .openhopOpenWindow, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(openSettingsFromUpdateNotification), name: .openhopOpenSettings, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(continueSetup), name: .openhopSetupContinue, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(forceQuit), name: .openhopQuitApp, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(toggleConnection), name: .openhopToggleConnection, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(toggleBackground), name: .openhopToggleBackground, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(rotateIP), name: .openhopRotateIP, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(toggleAutoRotate), name: .openhopToggleAutoRotate, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(restartAfterUpdate), name: .openhopUpdateRestart, object: nil)
        model.scheduleUpdateChecks()
        NSWorkspace.shared.notificationCenter.addObserver(
            self, selector: #selector(systemShutdown), name: NSWorkspace.willPowerOffNotification, object: nil)
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            guard let self, event.modifierFlags.contains(.command), event.keyCode == 12 else { return event }
            if self.model.keepInBackground {
                self.window?.orderOut(nil)
            } else {
                NotificationCenter.default.post(name: .openhopQuitApp, object: nil)
            }
            return nil
        }
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func systemShutdown() { explicitQuit = true }

    private func buildMainMenu() {
        let main = NSMenu()
        let app = NSMenu()
        let quit = NSMenuItem(title: "Quit OpenHop", action: #selector(hideFromCmdQ), keyEquivalent: "q")
        quit.target = self
        app.addItem(quit)
        let appItem = NSMenuItem()
        appItem.submenu = app
        main.addItem(appItem)

        let actions = NSMenu()
        for (title, key, selector) in [
            ("Start", "s", #selector(start)),
            ("Stop", "k", #selector(stop)),
            ("Get New IP", "i", #selector(hop))
        ] {
            let item = NSMenuItem(title: title, action: selector, keyEquivalent: key)
            item.keyEquivalentModifierMask = key == "k" ? [.command] : [.command, .option]
            item.target = self
            actions.addItem(item)
        }
        let actionsItem = NSMenuItem(title: "Actions", action: nil, keyEquivalent: "")
        actionsItem.submenu = actions
        main.addItem(actionsItem)

        let view = NSMenu()
        let fullScreen = NSMenuItem(title: "Enter Full Screen", action: #selector(NSWindow.toggleFullScreen(_:)), keyEquivalent: "f")
        fullScreen.keyEquivalentModifierMask = [.command, .control]
        fullScreen.target = nil
        view.addItem(fullScreen)
        let viewItem = NSMenuItem(title: "View", action: nil, keyEquivalent: "")
        viewItem.submenu = view
        main.addItem(viewItem)

        NSApp.mainMenu = main
    }

    private func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        item.button?.image = openhopMenuBarImage(dot: model.statusNSColor)
        let menu = NSMenu()
        menu.delegate = self
        item.menu = menu
        statusItem = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in
            guard let self, let item = self.statusItem, !item.isVisible else { return }
            NSApp.setActivationPolicy(.regular)
            self.model.notify(title: "OpenHop menu icon hidden", body: "macOS hid the menu bar icon. OpenHop now shows a Dock icon instead; re-enable it in System Settings → Control Center.")
        }
        Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            guard let self else { return }
            self.statusItem?.button?.image = openhopMenuBarImage(dot: self.model.statusNSColor)
        }
    }

    func menuNeedsUpdate(_ menu: NSMenu) {
        menu.removeAllItems()
        let add = { (title: String, action: Selector, enabled: Bool) in
            let i = NSMenuItem(title: title, action: action, keyEquivalent: "")
            i.target = self
            i.isEnabled = enabled
            menu.addItem(i)
        }
        add("Open OpenHop", #selector(openFromMenu), true)
        guard setupDone else { return }
        add("Check for Updates…", #selector(checkUpdates), true)
        menu.addItem(NSMenuItem.separator())
        add(model.liveWarpConnected() || model.liveJobRunning("keeper") ? "Disconnect" : "Connect", #selector(toggleConnection), true)
        add("Rotate IP", #selector(rotateIP), setupDone && model.operationState == .running && !model.isBusy)
        add(model.guardJob == "running" ? "Auto-rotate: On" : "Auto-rotate: Off", #selector(toggleAutoRotate), true)
        add(model.keeper == "running" ? "Background: On" : "Background: Off", #selector(toggleBackground), true)
        menu.addItem(NSMenuItem.separator())
        add("Quit OpenHop", #selector(forceQuit), true)
    }

    private func buildSetupWindow() {
        let hosting = NSHostingController(rootView: SetupGateView().environmentObject(model))
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 620, height: 460),
                         styleMask: [.titled, .closable], backing: .buffered, defer: false)
        w.title = "OpenHop Setup"
        w.delegate = self
        w.contentViewController = hosting
        if let screen = NSScreen.main {
            let f = screen.visibleFrame
            w.setFrame(NSRect(x: f.midX - 310, y: f.midY - 206, width: 620, height: 412), display: true)
        } else { w.center() }
        w.makeKeyAndOrderFront(nil)
        setupWindow = w
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func continueSetup() {
        guard model.setupComplete else { return }
        setupDone = true
        UserDefaults.standard.set(true, forKey: setupCompletedKey)
        setupWindow?.orderOut(nil)
        setupWindow = nil
        if window == nil { buildWindow() }
        show()
    }

    private func buildWindow() {
        let hosting = NSHostingController(rootView: DashboardView(store: store)
            .environmentObject(model))
        let w = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 1180, height: 700),
                         styleMask: [.titled, .closable, .miniaturizable, .resizable],
                         backing: .buffered, defer: false)
        w.title = "OpenHop — Control Center"
        w.delegate = self
        if let screen = NSScreen.main {
            w.setFrame(NSRect(x: screen.frame.midX - 590, y: screen.frame.midY - 350,
                              width: 1180, height: 700), display: false)
        } else { w.center() }
        w.minSize = NSSize(width: 940, height: 560)
        w.contentViewController = hosting
        w.setContentSize(NSSize(width: 1180, height: 700))
        w.appearance = NSAppearance(named: .darkAqua)
        w.collectionBehavior = [.fullScreenPrimary]
        w.makeKeyAndOrderFront(nil)
        w.orderFrontRegardless()
        window = w
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        guard setupDone else {
            if setupWindow == nil { buildSetupWindow() }
            setupWindow?.makeKeyAndOrderFront(nil)
            return true
        }
        if !flag { show() }
        return true
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }

    @objc private func openFromMenu() {
        guard setupDone else {
            if setupWindow == nil { buildSetupWindow() }
            setupWindow?.makeKeyAndOrderFront(nil)
            NSApp.activate(ignoringOtherApps: true)
            return
        }
        show()
    }

    @objc private func openSettingsFromUpdateNotification() {
        guard setupDone else { openFromMenu(); return }
        store.section = 5
        show()
    }

    @objc private func show() {
        let w: NSWindow
        if let existing = window {
            w = existing
        } else {
            buildWindow()
            guard let created = window else { return }
            w = created
        }
        NSApp.setActivationPolicy(.regular)
        w.makeKeyAndOrderFront(nil)
        w.orderFrontRegardless()
        NSApp.activate(ignoringOtherApps: true)
    }

    @objc private func hideFromCmdQ() {
        if model.keepInBackground {
            window?.orderOut(nil)
        } else {
            model.shutdownForQuit()
            explicitQuit = true
            NSApp.terminate(nil)
        }
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        if !explicitQuit {
            if model.keepInBackground {
                window?.orderOut(nil)
                return .terminateCancel
            }
            model.shutdownForQuit()
            return .terminateNow
        }
        return .terminateNow
    }

    func windowShouldClose(_ window: NSWindow) -> Bool {
        if window === setupWindow {
            NotificationCenter.default.post(name: .openhopQuitApp, object: nil)
            return false
        }
        if model.keepInBackground {
            self.window?.orderOut(nil)
            return false
        }
        model.shutdownForQuit()
        explicitQuit = true
        NSApp.terminate(nil)
        return false
    }

    func windowWillClose(_ notification: Notification) {
        // A closed NSWindow cannot be reshown; drop it so show() rebuilds it.
        if notification.object as? NSWindow === window { window = nil }
    }
    @objc private func start() { guard setupDone else { return }; NotificationCenter.default.post(name: .openhopStart, object: nil) }
    @objc private func stop() { guard setupDone else { return }; NotificationCenter.default.post(name: .openhopStop, object: nil) }
    @objc private func hop() { guard setupDone else { return }; NotificationCenter.default.post(name: .openhopSwitchIP, object: nil) }
    private var lastConnectionToggle = Date.distantPast

    @objc private func toggleConnection() {
        guard setupDone else { return }
        let now = Date()
        guard now.timeIntervalSince(lastConnectionToggle) > 2 else { return }
        lastConnectionToggle = now
        if model.liveJobRunning("keeper") || model.liveWarpConnected() { model.stop() }
        else { model.start() }
    }
    @objc private func toggleBackground() { guard setupDone else { return }; model.toggleService("keeper") }
    @objc private func rotateIP() { guard setupDone else { return }; model.switchIP() }
    @objc private func toggleAutoRotate() { guard setupDone else { return }; model.toggleService("guard") }
    @objc private func forceQuit() {
        explicitQuit = true
        model.shutdownForQuit()
        NSApp.terminate(nil)
    }
    @objc private func checkUpdates() { model.checkForUpdates() }
    @objc private func restartAfterUpdate() {
        explicitQuit = true
        NSApp.terminate(nil)
    }
}

extension Notification.Name {
    static let openhopStart = Notification.Name("openhop.start")
    static let openhopStop = Notification.Name("openhop.stop")
    static let openhopSwitchIP = Notification.Name("openhop.switchIP")
    static let openhopOpenWindow = Notification.Name("openhop.openWindow")
    static let openhopSetupContinue = Notification.Name("openhop.setupContinue")
    static let openhopQuitApp = Notification.Name("openhop.quitApp")
    static let openhopToggleConnection = Notification.Name("openhop.toggleConnection")
    static let openhopToggleBackground = Notification.Name("openhop.toggleBackground")
    static let openhopRotateIP = Notification.Name("openhop.rotateIP")
    static let openhopToggleAutoRotate = Notification.Name("openhop.toggleAutoRotate")
    static let openhopUpdateRestart = Notification.Name("openhop.updateRestart")
    static let openhopOpenSettings = Notification.Name("openhop.openSettings")
}

private struct SetupGateView: View {
    @EnvironmentObject var model: DashboardModel
    private let steps = ["OpenCode", "WARP", "App", "Runtime"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("OpenHop Setup").font(.system(size: 21, weight: .bold)).foregroundStyle(Brand.text)
            Text("OpenHop cannot run until every check passes. Install anything missing here, then press Continue.")
                .font(.system(size: 11)).foregroundStyle(Brand.muted)
            ForEach(steps, id: \.self) { step in
                HStack(alignment: .top, spacing: 10) {
                    let status = model.setupChecks[step] ?? ""
                    if status.hasPrefix("Checking") || model.setupCurrent == step {
                        ProgressView().controlSize(.small).frame(width: 18, alignment: .leading)
                    } else {
                        Image(systemName: status.hasPrefix("OK") ? "checkmark.circle.fill" : "xmark.circle.fill")
                            .foregroundStyle(status.hasPrefix("OK") ? Brand.green : Brand.bad)
                            .frame(width: 18, alignment: .leading)
                    }
                    VStack(alignment: .leading, spacing: 2) {
                        Text(step).font(.system(size: 13, weight: .semibold)).foregroundStyle(Brand.text)
                        Text(status.isEmpty ? "Not checked" : status)
                            .font(.system(size: 11))
                            .foregroundStyle(status.hasPrefix("OK") ? Brand.green
                                             : status.hasPrefix("Checking") ? Brand.muted : Brand.bad)
                    }
                    Spacer()
                    if model.setupInstalling == step {
                        ProgressView().controlSize(.small)
                    } else if !status.hasPrefix("OK") && !status.hasPrefix("Checking") {
                        Button("Install") { model.installStep(step) }
                            .buttonStyle(.bordered).controlSize(.small)
                            .disabled(!model.setupInstalling.isEmpty)
                    }
                }
            }
            if !model.setupMessage.isEmpty {
                Text(model.setupMessage).font(.system(size: 11)).foregroundStyle(Brand.amber)
            }
            Spacer(minLength: 4)
            HStack {
                Button("Re-check") { model.checkSetupAsync() }.buttonStyle(.bordered).disabled(model.setupChecking)
                Spacer()
                Button("Exit") {
                    NotificationCenter.default.post(name: .openhopQuitApp, object: nil)
                }.buttonStyle(.bordered)
                Button("Continue") {
                    NotificationCenter.default.post(name: .openhopSetupContinue, object: nil)
                }
                .buttonStyle(.borderedProminent).tint(Brand.green)
                .keyboardShortcut(.defaultAction)
                .disabled(!model.setupComplete)
            }
        }
        .padding(22)
        .frame(width: 620, height: 380)
        .animation(.easeInOut(duration: 0.2), value: model.setupChecks)
    }
}

// MARK: - Sections

private enum Section: Int, CaseIterable, Identifiable {
    case dashboard, activity, diagnostics, services, usage, settings
    var id: Int { rawValue }
    var title: String {
        switch self {
        case .dashboard: return "Dashboard"
        case .activity: return "Activity"
        case .diagnostics: return "Diagnostics"
        case .services: return "Services"
        case .usage: return "Usage"
        case .settings: return "Settings"
        }
    }
    var subtitle: String {
        switch self {
        case .dashboard: return "Overview and primary actions"
        case .activity: return "A live timeline of what OpenHop is doing"
        case .diagnostics: return "Structured health checks"
        case .services: return "Background jobs and the OpenCode lane"
        case .usage: return "Local tokens, rotations and limit counts"
        case .settings: return "Background behaviour and app options"
        }
    }
    var symbol: String {
        switch self {
        case .dashboard: return "square.grid.2x2"
        case .activity: return "waveform.path.ecg"
        case .diagnostics: return "stethoscope"
        case .services: return "server.rack"
        case .usage: return "chart.bar.fill"
        case .settings: return "gearshape"
        }
    }
}

// MARK: - Shared building blocks

private struct GlassCard<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 16) { content }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(20)
            .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Color.white.opacity(0.09), lineWidth: 1))
            .shadow(color: .black.opacity(0.22), radius: 8, y: 4)
    }
}

private struct SectionTitle: View {
    let text: String
    var body: some View {
        Text(text.uppercased())
            .font(.system(size: 10, weight: .semibold))
            .kerning(1)
            .foregroundStyle(Brand.muted)
    }
}

// Explicit fills: the prominent style picks a dark fill for custom tints on this
// window's dark appearance, which made light Brand accents unreadable.
private struct AccentButtonStyle: ButtonStyle {
    let accent: Color
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .foregroundStyle(.black)
            .frame(maxWidth: .infinity, minHeight: 40)
            .background(accent, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .opacity(isEnabled ? (configuration.isPressed ? 0.82 : 1) : 0.45)
    }
}

private struct OutlineButtonStyle: ButtonStyle {
    let accent: Color
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(.system(size: 13, weight: .semibold))
            .foregroundStyle(accent)
            .frame(minHeight: 40)
            .padding(.horizontal, 14)
            .background(accent.opacity(configuration.isPressed ? 0.22 : 0.10),
                        in: RoundedRectangle(cornerRadius: 10, style: .continuous))
            .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                .stroke(accent.opacity(0.5), lineWidth: 1))
            .opacity(isEnabled ? 1 : 0.45)
    }
}

private struct PrimaryAction: View {
    let title: String
    let symbol: String
    var accent: Color = Brand.surface
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            Label(title, systemImage: symbol)
                .font(.system(size: 13, weight: .semibold))
        }
        .buttonStyle(AccentButtonStyle(accent: accent))
        .pointingCursor()
    }
}

private struct StatBox: View {
    let title: String
    let value: String
    var mono = false
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            SectionTitle(text: title)
            Text(value)
                .font(.system(size: 13, weight: .semibold, design: mono ? .monospaced : .default))
                .lineLimit(1)
                .foregroundStyle(Brand.text)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(12)
        .background(Brand.surface.opacity(0.7), in: RoundedRectangle(cornerRadius: 8))
        .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.white.opacity(0.07), lineWidth: 1))
    }
}

private struct StatusChip: View {
    let text: String
    let color: Color
    var body: some View {
        Text(text)
            .font(.system(size: 9, weight: .bold))
            .kerning(0.8)
            .foregroundStyle(color)
            .padding(.horizontal, 10).padding(.vertical, 6)
            .background(color.opacity(0.12), in: Capsule())
    }
}

private struct Hairline: View {
    var body: some View {
        Rectangle().fill(Color.white.opacity(0.06)).frame(height: 1)
    }
}

private func toneColor(_ tone: EventTone) -> Color {
    switch tone {
    case .success: return Brand.green
    case .info: return Brand.amber
    case .warn: return Brand.warn
    case .error: return Brand.bad
    }
}

// MARK: - Sidebar

private struct SidebarView: View {
    @ObservedObject var store: SidebarStore
    private var current: Section { Section(rawValue: store.section) ?? .dashboard }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            brandHeader
            VStack(spacing: 6) {
                ForEach(Section.allCases) { section in
                    sidebarRow(section)
                }
            }
            .padding(.horizontal, 16)
            .padding(.top, 16)
            Spacer()
            Text("OPENHOP · MACOS")
                .font(.system(size: 9, weight: .semibold))
                .kerning(1)
                .foregroundStyle(Brand.muted.opacity(0.7))
                .padding(.horizontal, 18).padding(.bottom, 18)
        }
        .frame(width: 200)
        .background(Brand.deep)
    }

    private var brandHeader: some View {
        HStack(spacing: 10) {
            Image(systemName: "shield.checkered")
                .font(.system(size: 16, weight: .bold))
                .foregroundStyle(.black)
                .frame(width: 32, height: 32)
                .background(Brand.amber, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            VStack(alignment: .leading, spacing: 2) {
                Text("openhop")
                    .font(.system(size: 15, weight: .bold))
                    .foregroundStyle(Brand.text)
                Text("CONTROL CENTER")
                    .font(.system(size: 8, weight: .semibold))
                    .kerning(1)
                    .foregroundStyle(Brand.muted)
            }
        }
        .padding(.horizontal, 16).padding(.top, 24).padding(.bottom, 4)
    }

    private func sidebarRow(_ section: Section) -> some View {
        let selected = current == section
        return Button {
            store.section = section.rawValue
        } label: {
            HStack(spacing: 10) {
                Image(systemName: section.symbol)
                    .font(.system(size: 14, weight: .semibold))
                    .frame(width: 18)
                Text(section.title)
                    .font(.system(size: 13, weight: selected ? .semibold : .regular))
                Spacer(minLength: 0)
            }
            .foregroundStyle(selected ? Brand.amber : Brand.muted)
            .padding(.horizontal, 12).padding(.vertical, 10)
            .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 9, style: .continuous)
                    .fill(selected ? Brand.amber.opacity(0.10) : Color.clear)
            )
            .overlay(alignment: .leading) {
                if selected {
                    RoundedRectangle(cornerRadius: 2)
                        .fill(Brand.amber)
                        .frame(width: 3, height: 20)
                }
            }
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .contentShape(Rectangle())
        .pointingCursor()
    }
}

// MARK: - Root and main pane

private struct DashboardView: View {
    @ObservedObject var store: SidebarStore
    @EnvironmentObject var model: DashboardModel
    @Environment(\.accessibilityReduceMotion) var reduceMotion

    var body: some View {
        HStack(spacing: 0) {
            SidebarView(store: store)
            MainPane(store: store)
        }
        .overlay(alignment: .topTrailing) {
            if let toast = model.toast {
                ToastView(toast: toast)
                    .padding(.trailing, 24).padding(.top, 16)
                    .transition(.move(edge: .top).combined(with: .opacity))
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: model.toast?.id)
        .onAppear { model.reconcile(); model.startGuardWatch() }
        .onReceive(NotificationCenter.default.publisher(for: .openhopStart)) { _ in model.start() }
        .onReceive(NotificationCenter.default.publisher(for: .openhopStop)) { _ in model.stop() }
        .onReceive(NotificationCenter.default.publisher(for: .openhopSwitchIP)) { _ in model.switchIP() }
    }
}

private struct MainPane: View {
    @ObservedObject var store: SidebarStore
    @EnvironmentObject var model: DashboardModel
    private var current: Section { Section(rawValue: store.section) ?? .dashboard }

    var body: some View {
        VStack(spacing: 0) {
            Toolbar(section: current)
            Hairline()
            GeometryReader { geo in
                ScrollView {
                    VStack(alignment: .leading, spacing: 20) {
                        panel(for: current)
                    }
                    .padding(.horizontal, 28)
                    .padding(.vertical, 24)
                    .frame(maxWidth: min(geo.size.width, 1500), alignment: .leading)
                    .frame(maxWidth: .infinity, alignment: .center)
                }
                .background(Brand.bg)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onChange(of: store.section) { _ in
            if current == .diagnostics { model.doctor() }
        }
    }

    private func panel(for section: Section) -> AnyView {
        switch section {
        case .dashboard: return AnyView(DashboardPanel())
        case .activity: return AnyView(ActivityPanel())
        case .diagnostics: return AnyView(DiagnosticsPanel())
        case .services: return AnyView(ServicesPanel())
        case .usage: return AnyView(UsagePanel())
        case .settings: return AnyView(SettingsPanel())
        }
    }
}

private struct Toolbar: View {
    let section: Section
    @EnvironmentObject var model: DashboardModel

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Image(systemName: section.symbol)
                    .font(.system(size: 15, weight: .semibold))
                    .foregroundStyle(Brand.amber)
                VStack(alignment: .leading, spacing: 2) {
                    Text(section.title)
                        .font(.system(size: 15, weight: .bold))
                        .foregroundStyle(Brand.text)
                    Text(section.subtitle)
                        .font(.system(size: 10))
                        .foregroundStyle(Brand.muted)
                }
                Spacer()
                StatusChip(text: displayStatus, color: statusColor)
            }
            if model.isBusy {
                VStack(alignment: .leading, spacing: 6) {
                    HStack(spacing: 8) {
                        Image(systemName: "gearshape.2")
                            .foregroundStyle(Brand.amber)
                        Text(model.progressMessage)
                            .font(.system(size: 11, weight: .medium))
                            .foregroundStyle(Brand.text)
                            .lineLimit(1)
                        Spacer()
                        if lifecycleBusy {
                            Button("Cancel") { model.stopCurrent() }
                                .buttonStyle(.borderless).controlSize(.small).pointingCursor()
                        }
                    }
                    ProgressView()
                        .progressViewStyle(.linear)
                        .tint(Brand.amber)
                        .frame(height: 4)
                }
                .padding(.top, 4)
            }
        }
        .padding(.horizontal, 28).padding(.vertical, 14)
        .background(Brand.deep.opacity(0.94))
    }

    private var lifecycleBusy: Bool {
        model.operationState == .starting || model.operationState == .stopping || model.operationState == .switching
    }
    private var isRunning: Bool { model.operationState == .running }
    private var isStopped: Bool {
        model.operationState == .stopped || model.operationState == .unknown || model.operationState == .failed
    }
    private var checksHealthy: Bool {
        !model.healthChecks.isEmpty && model.healthChecks.allSatisfy { $0.ok && !$0.warning }
    }
    private var displayStatus: String {
        switch model.operationState {
        case .stopped: return "STOPPED"
        case .failed: return "NEEDS ATTENTION"
        case .starting: return "STARTING"
        case .stopping: return "STOPPING"
        case .switching: return "SWITCHING IP"
        case .running, .unknown:
            return checksHealthy ? "RUNNING · WARP PROTECTED" : "RUNNING · CHECKS NEEDED"
        }
    }
    private var statusColor: Color {
        switch model.operationState {
        case .starting, .switching, .stopping: return Brand.amber
        case .running, .unknown: return checksHealthy ? Brand.green : Brand.warn
        default: return Brand.bad
        }
    }
}

// MARK: - Dashboard

private struct DashboardPanel: View {
    @EnvironmentObject var model: DashboardModel

    private var isRunning: Bool { model.operationState == .running }
    private var isStopped: Bool {
        model.operationState == .stopped || model.operationState == .unknown || model.operationState == .failed
    }

    var body: some View {
        Group {
            GlassCard {
                HStack(alignment: .top, spacing: 16) {
                    Image(systemName: icon)
                        .font(.system(size: 22, weight: .bold))
                        .foregroundStyle(.black)
                        .frame(width: 46, height: 46)
                        .background(statusColor, in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                    VStack(alignment: .leading, spacing: 5) {
                        Text(headline)
                            .font(.system(size: 23, weight: .bold))
                            .foregroundStyle(statusColor)
                        Text(subline)
                            .font(.system(size: 12))
                            .foregroundStyle(Brand.muted)
                    }
                    Spacer()
                }
                Hairline().opacity(0.5)
                LazyVGrid(columns: [GridItem(.flexible()), GridItem(.flexible()), GridItem(.flexible())], spacing: 10) {
                    StatBox(title: "Current IP", value: model.ip, mono: true)
                    StatBox(title: "Connection", value: model.warp == "connected" ? "WARP · OpenCode only" : "Disconnected")
                    StatBox(title: "Proxy port", value: "127.0.0.1:40000", mono: true)
                }
            }

            GlassCard {
                SectionTitle(text: "Primary actions")
                HStack(spacing: 10) {
                    PrimaryAction(title: "Start", symbol: "play.fill", accent: Brand.green) { model.start() }
                        .disabled(isRunning)
                    PrimaryAction(title: "Stop", symbol: "stop.fill", accent: Brand.bad) { model.stop() }
                        .disabled(isStopped)
                    PrimaryAction(title: "Get New IP", symbol: "arrow.triangle.2.circlepath", accent: Brand.amber) { model.switchIP() }
                }
                .disabled(model.isBusy)
                if let summary = model.lastReport?.summary {
                    Text(summary)
                        .font(.system(size: 11))
                        .foregroundStyle(Brand.muted)
                }
            }

            GlassCard {
                SectionTitle(text: "At a glance")
                HStack(spacing: 18) {
                    StatusPill(title: "Keeper", value: model.keeper)
                    StatusPill(title: "Guard", value: model.guardJob)
                    StatusPill(title: "WARP", value: model.warp)
                    StatusPill(title: "OpenCode lane", value: model.laneStatus)
                }
            }

            GlassCard {
                SectionTitle(text: "Protection")
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 8) {
                            Text("Background protection").font(.system(size: 13, weight: .semibold))
                            StatusChip(text: model.keeper == "running" ? "ON" : "OFF",
                                       color: model.keeper == "running" ? Brand.green : Brand.bad)
                        }
                        Text("Keeps OpenCode connected, even with this window closed.")
                            .font(.system(size: 11)).foregroundStyle(Brand.muted)
                        Text("launchd: com.openhop.keeper").font(.system(size: 10)).foregroundStyle(Brand.muted)
                    }
                    Spacer()
                    Toggle("", isOn: Binding(get: { model.keeper == "running" },
                                             set: { _ in model.toggleService("keeper") }))
                        .labelsHidden().tint(Brand.green)
                        .disabled(model.isBusy || model.auxiliaryBusy)
                }
                HStack {
                    VStack(alignment: .leading, spacing: 2) {
                        HStack(spacing: 8) {
                            Text("Auto-rotate on limit").font(.system(size: 13, weight: .semibold))
                            StatusChip(text: model.guardJob == "running" ? "ON" : "OFF",
                                       color: model.guardJob == "running" ? Brand.green : Brand.bad)
                        }
                        Text("Gets a new IP by itself when you hit the free limit.")
                            .font(.system(size: 11)).foregroundStyle(Brand.muted)
                        Text("launchd: com.openhop.guard").font(.system(size: 10)).foregroundStyle(Brand.muted)
                    }
                    Spacer()
                    Toggle("", isOn: Binding(get: { model.guardJob == "running" },
                                             set: { _ in model.toggleService("guard") }))
                        .labelsHidden().tint(Brand.green)
                        .disabled(model.isBusy || model.auxiliaryBusy)
                }
            }
        }
    }

    private var headline: String {
        switch model.operationState {
        case .starting: return "Starting OpenHop"
        case .stopping: return "Stopping OpenHop"
        case .switching: return "Getting a new IP"
        case .stopped: return "OpenHop is stopped"
        case .failed: return "Needs attention"
        case .running, .unknown:
            return model.health == "EVERYTHING IS WORKING" ? "Everything is working"
                 : model.health.replacingOccurrences(of: "EVERYTHING IS WORKING", with: "").lowercased().capitalized
        }
    }
    private var subline: String {
        if model.operationState == .stopped {
            return "Keeper, Guard and WARP are stopped. Press Start to restore and verify the full system."
        }
        return "OpenCode is connected through Cloudflare WARP. IP switching refreshes dependant components."
    }
    private var icon: String {
        switch model.operationState {
        case .stopped, .failed: return "xmark.circle.fill"
        case .starting, .switching: return "arrow.triangle.2.circlepath"
        default: return "checkmark.circle.fill"
        }
    }
    private var statusColor: Color {
        switch model.operationState {
        case .starting, .switching: return Brand.amber
        case .running: return model.health == "EVERYTHING IS WORKING" ? Brand.green : Brand.warn
        default: return Brand.bad
        }
    }
}

private struct StatusPill: View {
    let title: String
    let value: String
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            SectionTitle(text: title)
            HStack(spacing: 6) {
                Circle().frame(width: 7, height: 7)
                    .foregroundStyle(good ? Brand.green : Brand.bad)
                Text(value)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Brand.text)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
    private var good: Bool {
        value == "running" || value == "connected" || value == "ready"
    }
}

// MARK: - Activity (replaces the old console)

private struct ActivityPanel: View {
    @EnvironmentObject var model: DashboardModel
    @Environment(\.accessibilityReduceMotion) var reduceMotion

    var body: some View {
        GlassCard {
            HStack(spacing: 10) {
                SectionTitle(text: "Live timeline")
                if model.isBusy {
                    HStack(spacing: 5) {
                        Circle().frame(width: 6, height: 6).foregroundStyle(Brand.green)
                        Text("UPDATING")
                            .font(.system(size: 9, weight: .bold)).kerning(0.8)
                            .foregroundStyle(Brand.green)
                    }
                }
                Spacer()
                Text("Newest first")
                    .font(.system(size: 10))
                    .foregroundStyle(Brand.muted)
            }
            if model.events.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "waveform.path.ecg")
                        .font(.system(size: 28))
                        .foregroundStyle(Brand.muted)
                    Text("Nothing here yet")
                        .font(.system(size: 14, weight: .semibold))
                        .foregroundStyle(Brand.text)
                    Text("Start, Stop, Get New IP and health checks will appear here as they happen.")
                        .font(.system(size: 11))
                        .foregroundStyle(Brand.muted)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity)
                .padding(.vertical, 44)
            } else {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(model.events) { event in
                        ActivityRow(event: event)
                    }
                }
            }
        }
        .animation(reduceMotion ? nil : .easeOut(duration: 0.18), value: model.events.count)
    }
}

private struct ActivityRow: View {
    let event: ActivityEvent

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: event.symbol)
                .font(.system(size: 12, weight: .bold))
                .foregroundStyle(toneColor(event.tone))
                .frame(width: 28, height: 28)
                .background(toneColor(event.tone).opacity(0.14), in: Circle())
            VStack(alignment: .leading, spacing: 3) {
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Text(event.title)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Brand.text)
                    Spacer(minLength: 0)
                    Text(time)
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundStyle(Brand.muted)
                }
                Text(event.detail)
                    .font(.system(size: 11))
                    .foregroundStyle(Brand.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.vertical, 10)
        .overlay(alignment: .bottom) { Hairline().padding(.leading, 40) }
    }

    private var time: String {
        let f = DateFormatter()
        f.dateFormat = "HH:mm:ss"
        return f.string(from: event.at)
    }
}

// MARK: - Diagnostics (no raw doctor output)

private struct DiagnosticsPanel: View {
    @EnvironmentObject var model: DashboardModel

    var body: some View {
        Group {
            GlassCard {
                HStack(spacing: 10) {
                    SectionTitle(text: "System checks")
                    if model.doctorRunning {
                        HStack(spacing: 6) {
                            ProgressView().controlSize(.small)
                            Text("CHECKING…")
                                .font(.system(size: 9, weight: .bold))
                                .kerning(0.8)
                                .foregroundStyle(Brand.amber)
                        }
                    }
                    Spacer()
                }
                VStack(spacing: 0) {
                    ForEach(model.healthChecks) { check in
                        CheckRow(check: check)
                    }
                }
                HStack(spacing: 10) {
                    PrimaryAction(title: model.doctorRunning ? "Running Doctor…" : "Run Doctor",
                                  symbol: "stethoscope", accent: Brand.amber) { model.doctor() }
                    PrimaryAction(title: "Fix", symbol: "wrench.and.screwdriver", accent: Brand.green) { model.repair() }
                }
                .disabled(model.isBusy)
            }
            if let summary = model.lastReport?.summary {
                GlassCard {
                    SectionTitle(text: "Last action")
                    Text(summary)
                        .font(.system(size: 12))
                        .foregroundStyle(Brand.text)
                }
            }
        }
        .onAppear {
            model.refreshStates()
            if !model.doctorRunning && !model.isBusy { model.doctor() }
        }
    }
}

private struct CheckRow: View {
    let check: HealthCheck

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(color)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 3) {
                Text(check.label)
                    .font(.system(size: 13, weight: .semibold))
                    .foregroundStyle(Brand.text)
                Text(check.detail)
                    .font(.system(size: 11))
                    .foregroundStyle(Brand.muted)
            }
            Spacer(minLength: 0)
            StatusChip(text: check.ok ? "OK" : (check.warning ? "ATTENTION" : "ISSUE"), color: color)
        }
        .padding(.vertical, 10)
        .overlay(alignment: .bottom) { Hairline() }
    }

    private var color: Color {
        check.ok ? Brand.green : (check.warning ? Brand.warn : Brand.bad)
    }
    private var symbol: String {
        check.ok ? "checkmark.circle.fill" : (check.warning ? "exclamationmark.triangle.fill" : "xmark.circle.fill")
    }
}

// MARK: - Services

private struct ServicesPanel: View {
    @EnvironmentObject var model: DashboardModel

    var body: some View {
        Group {
            GlassCard {
                SectionTitle(text: "Background services")
                ServiceCard(symbol: "gearshape.fill", title: "Keeper",
                            description: "Keeps the WARP tunnel and background jobs alive.",
                            status: model.keeper, actionTitle: "Restart") { model.restartJob("keeper") }
                Hairline()
                ServiceCard(symbol: "shield.checkered", title: "Guard",
                            description: "Watches the connection and refreshes it when needed.",
                            status: model.guardJob, actionTitle: "Restart") { model.restartJob("guard") }
            }
            GlassCard {
                SectionTitle(text: "Application lane")
                ServiceCard(symbol: "desktopcomputer", title: "OpenCode lane",
                            description: "Runs OpenCode through the WARP proxy.",
                            status: model.laneStatus, actionTitle: "Launch OpenCode") { model.launchOpenCode() }
            }
        }
    }
}

private struct ServiceCard: View {
    @EnvironmentObject var model: DashboardModel
    let symbol: String
    let title: String
    let description: String
    let status: String
    let actionTitle: String
    let action: () -> Void

    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: symbol)
                .font(.system(size: 16, weight: .semibold))
                .foregroundStyle(Brand.amber)
                .frame(width: 36, height: 36)
                .background(Brand.amber.opacity(0.12), in: RoundedRectangle(cornerRadius: 9, style: .continuous))
            VStack(alignment: .leading, spacing: 4) {
                HStack(spacing: 8) {
                    Text(title)
                        .font(.system(size: 13, weight: .semibold))
                        .foregroundStyle(Brand.text)
                    StatusChip(text: chipText, color: chipColor)
                }
                Text(description)
                    .font(.system(size: 11))
                    .foregroundStyle(Brand.muted)
            }
            Spacer(minLength: 0)
            Button(action: action) {
                Text(actionTitle)
                    .font(.system(size: 11, weight: .semibold))
                    .padding(.horizontal, 12).padding(.vertical, 7)
            }
            .buttonStyle(.bordered)
            .controlSize(.small)
            .tint(Brand.amber)
            .buttonBorderShape(.roundedRectangle)
            .disabled(model.isBusy)
            .pointingCursor()
        }
    }

    private var chipText: String {
        switch status {
        case "running": return "RUNNING"
        case "connected": return "CONNECTED"
        case "ready": return "READY"
        case "stopped": return "STOPPED"
        case "not ready": return "NOT READY"
        default: return "UNKNOWN"
        }
    }
    private var chipColor: Color {
        switch status {
        case "running", "connected", "ready": return Brand.green
        case "stopped", "not ready": return Brand.bad
        default: return Brand.warn
        }
    }
}

// MARK: - In-window notification

private struct UsagePanel: View {
    @EnvironmentObject var model: DashboardModel

    private func tile(_ title: String, _ value: String, _ note: String) -> some View {
        VStack(alignment: .leading, spacing: 5) {
            Text(title.uppercased()).font(.system(size: 9, weight: .bold)).kerning(0.8).foregroundStyle(Brand.muted)
            Text(value).font(.system(size: 22, weight: .bold)).foregroundStyle(Brand.text)
            Text(note).font(.system(size: 10)).foregroundStyle(Brand.muted)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(14)
        .background(Brand.surface.opacity(0.55), in: RoundedRectangle(cornerRadius: 10))
    }

    private func usageNumber(_ n: Int) -> String {
        let s = String(n)
        var out = ""
        for (i, ch) in s.enumerated() {
            if i > 0 && (s.count - i) % 3 == 0 { out.append(".") }
            out.append(ch)
        }
        return out
    }

    var body: some View {
        Group {
            GlassCard {
                SectionTitle(text: "OpenCode usage · last 24 hours")
                HStack(alignment: .top, spacing: 12) {
                    tile("Tokens", usageNumber(model.usage.tokens), "input + output")
                    tile("Messages", usageNumber(model.usage.messages), "answers counted")
                    tile("Chats", usageNumber(model.usage.chats), "sessions seen")
                }
            }
            GlassCard {
                SectionTitle(text: "Rotations")
                HStack(alignment: .top, spacing: 12) {
                    tile("Today", usageNumber(model.usage.rotationsToday), "since daily reset")
                    tile("All time", usageNumber(model.usage.rotationsAll), "every address tried")
                    tile("Worked", usageNumber(model.usage.goodIPs), "verified new IPs")
                }
                HStack(alignment: .top, spacing: 12) {
                    tile("Used up", usageNumber(model.usage.usedUp), "blacklisted /64s")
                    tile("Limits", usageNumber(model.usage.limits), "limit lines in guard log")
                    tile("Continues", usageNumber(model.usage.continues), "sent to chats")
                }
            }
            GlassCard {
                SectionTitle(text: "Auto-rotate watch")
                Text("The guard watches OpenCode's log. On a free-limit message it gets a new IP, clears the limit banner and sends “continue” — about 10 seconds, chats and history untouched.")
                    .font(.system(size: 12)).foregroundStyle(Brand.text)
                PrimaryAction(title: "Refresh counts", symbol: "arrow.triangle.2.circlepath", accent: Brand.amber) {
                    model.loadUsage()
                }
            }
        }
        .onAppear { model.loadUsage() }
    }
}

private struct UpdateStatusLine: View {
    let state: UpdateState
    let message: String
    let badge: Bool

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: symbol)
                .font(.system(size: 14, weight: .bold))
                .foregroundStyle(color)
                .frame(width: 18)
            VStack(alignment: .leading, spacing: 3) {
                HStack(spacing: 6) {
                    Text(title)
                        .font(.system(size: 12, weight: .semibold))
                        .foregroundStyle(color)
                    if badge, case .available = state {
                        Circle().frame(width: 6, height: 6).foregroundStyle(Brand.amber)
                    }
                }
                if !detail.isEmpty {
                    Text(detail)
                        .font(.system(size: 11))
                        .foregroundStyle(Brand.muted)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
            Spacer(minLength: 0)
        }
        .padding(12)
        .background(color.opacity(0.10), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
            .stroke(color.opacity(0.22), lineWidth: 1))
    }

    private var title: String {
        switch state {
        case .idle: return "Ready to check"
        case .checking: return "Checking for updates"
        case .upToDate: return "Up to date"
        case .available(let v): return "Version \(v) is available"
        case .downloading: return "Downloading update"
        case .ready: return "Ready to install"
        case .failed: return "Update failed"
        }
    }
    private var detail: String {
        switch state {
        case .idle: return message.isEmpty ? "Check to see whether a newer version is available." : message
        case .checking: return message.isEmpty ? "Looking for the latest release." : message
        case .upToDate: return message.isEmpty ? "You are running the latest version." : message
        case .available: return "Download and install when you are ready."
        case .downloading: return message.isEmpty ? "Preparing the verified update." : message
        case .ready: return message.isEmpty ? "Verified and ready to install." : message
        case .failed(let error): return error.isEmpty ? message : error
        }
    }
    private var symbol: String {
        switch state {
        case .idle: return "clock"
        case .checking: return "arrow.triangle.2.circlepath"
        case .upToDate: return "checkmark.circle.fill"
        case .available: return "arrow.down.circle.fill"
        case .downloading: return "arrow.down.circle"
        case .ready: return "checkmark.shield.fill"
        case .failed: return "xmark.circle.fill"
        }
    }
    private var color: Color {
        switch state {
        case .idle: return Brand.muted
        case .checking, .downloading, .available: return Brand.amber
        case .upToDate, .ready: return Brand.green
        case .failed: return Brand.bad
        }
    }
}

private struct SettingsPanel: View {
    @EnvironmentObject var model: DashboardModel
    private let endpoints = ["automatic", "162.159.192.1:2408", "162.159.193.1:500",
                             "188.114.96.1:4500", "162.159.192.1:854"]

    private var routeApplyButton: some View {
        Button("Apply") { model.applyRouteDraft() }
            .buttonStyle(.borderedProminent).tint(Brand.amber)
            .disabled(!model.routeDirty || model.isBusy)
    }

    var body: some View {
        Group {
            GlassCard {
                SectionTitle(text: "Quit behaviour")
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Stay active after Cmd+Q").font(.system(size: 14, weight: .semibold))
                        Text("This switch controls Cmd+Q only.")
                            .font(.system(size: 11)).foregroundStyle(Brand.muted)
                    }
                    Spacer()
                    Toggle("", isOn: Binding(get: { model.keepInBackground },
                                             set: { model.setKeepInBackground($0) }))
                        .labelsHidden().tint(Brand.green)
                }
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: model.keepInBackground ? "checkmark.circle.fill" : "xmark.circle.fill")
                        .foregroundStyle(model.keepInBackground ? Brand.green : Brand.amber)
                    Text(model.keepInBackground
                         ? "ON — Cmd+Q hides the window. Keeper, Guard and WARP keep running in the background."
                         : "OFF — Cmd+Q stops Keeper, Guard and WARP, then quits OpenHop.")
                        .font(.system(size: 11)).foregroundStyle(Brand.muted)
                }
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "power.circle.fill").foregroundStyle(Brand.bad)
                    Text("Menu-bar Quit OpenHop always stops everything and exits — with this switch ON or OFF.")
                        .font(.system(size: 11)).foregroundStyle(Brand.muted)
                }
            }
            GlassCard {
                HStack {
                    SectionTitle(text: "Connection route")
                    Spacer()
                    routeApplyButton
                }
                Text("Only OpenCode is routed. If the chosen proxy is down, OpenCode fails closed — it never leaks your real IP.")
                    .font(.system(size: 11)).foregroundStyle(Brand.muted)
                Text("Active route: \(model.egressMode == "custom" ? "custom proxy (\(model.customProxy))" : "WARP (only OpenCode)")")
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(model.egressMode == "custom" ? Brand.amber : Brand.green)
                Picker("Route", selection: Binding(get: { model.draftEgressMode },
                                                   set: { model.draftEgressMode = $0 })) {
                    Text("WARP (only OpenCode)").tag("warp")
                    Text("Custom proxy").tag("custom")
                }.pickerStyle(.menu).frame(width: 260)
                if model.draftEgressMode == "custom" {
                    HStack {
                        TextField("host:port", text: Binding(get: { model.draftCustomProxy },
                                                             set: { model.draftCustomProxy = $0 }))
                            .textFieldStyle(.roundedBorder)
                        Picker("", selection: Binding(get: { model.draftCustomProxyType },
                                                      set: { model.draftCustomProxyType = $0 })) {
                            Text("socks5").tag("socks5")
                            Text("http").tag("http")
                        }.pickerStyle(.segmented).frame(width: 150)
                    }
                    Text("OpenCode uses this proxy after Apply. If it is down, OpenCode fails closed.")
                        .font(.system(size: 10)).foregroundStyle(Brand.muted)
                }
                HStack {
                    Picker("WARP endpoint", selection: Binding(get: { model.draftCleanEndpoint },
                                                               set: { model.draftCleanEndpoint = $0 })) {
                        ForEach(endpoints, id: \.self) { Text($0).tag($0) }
                    }
                }
                Text("Clean endpoints pin a specific Cloudflare entry point for restricted networks. 'automatic' restores the default.")
                    .font(.system(size: 10)).foregroundStyle(Brand.muted)
            }
            .onAppear {
                model.loadEgressConfig()
                model.loadRouteDraft()
            }
            GlassCard {
                SectionTitle(text: "Startup")
                HStack {
                    VStack(alignment: .leading, spacing: 3) {
                        Text("Start OpenHop at login").font(.system(size: 14, weight: .semibold))
                        Text("Adds a user launch agent — no sudo, no system-wide changes.")
                            .font(.system(size: 11)).foregroundStyle(Brand.muted)
                    }
                    Spacer()
                    Toggle("", isOn: Binding(get: { model.launchAtLogin },
                                             set: { model.setLaunchAtLogin($0) }))
                        .labelsHidden().tint(Brand.green)
                }
            }
            GlassCard {
                SectionTitle(text: "About")
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "shield.checkered")
                        .font(.system(size: 16, weight: .bold))
                        .foregroundStyle(.black)
                        .frame(width: 36, height: 36)
                        .background(Brand.amber, in: RoundedRectangle(cornerRadius: 9, style: .continuous))
                    VStack(alignment: .leading, spacing: 3) {
                        Text("OpenHop Control Center").font(.system(size: 14, weight: .semibold))
                        Text("Mac port · native dashboard · local-only controls")
                            .font(.system(size: 11)).foregroundStyle(Brand.muted)
                    }
                    Spacer(minLength: 0)
                }
                Hairline()
                HStack(alignment: .firstTextBaseline) {
                    SectionTitle(text: "Installed version")
                    Spacer(minLength: 12)
                    Text("v\(UpdateManager.appVersion)")
                        .font(.system(size: 12, weight: .semibold, design: .monospaced))
                        .foregroundStyle(Brand.text)
                }
            }
            GlassCard {
                SectionTitle(text: "Updates")
                UpdateStatusLine(state: model.updateState, message: model.updateMessage, badge: model.updateBadge)
                switch model.updateState {
                case .available(let v):
                    VStack(alignment: .leading, spacing: 10) {
                        HStack(alignment: .firstTextBaseline) {
                            SectionTitle(text: "Latest version")
                            Spacer(minLength: 12)
                            Text(v)
                                .font(.system(size: 18, weight: .bold, design: .monospaced))
                                .foregroundStyle(Brand.amber)
                        }
                        .padding(12)
                        .background(Brand.surface.opacity(0.7), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                        .overlay(RoundedRectangle(cornerRadius: 10, style: .continuous)
                            .stroke(Color.white.opacity(0.07), lineWidth: 1))
                        HStack(spacing: 10) {
                            PrimaryAction(title: "Update to \(v)", symbol: "arrow.down.circle",
                                          accent: Brand.green) { model.downloadUpdate() }
                            if !model.updateNotes.isEmpty {
                                Button { model.showReleaseNotes = true } label: {
                                    Label("What's new", systemImage: "doc.text")
                                }
                                .buttonStyle(OutlineButtonStyle(accent: Brand.amber))
                                .pointingCursor()
                            }
                        }
                    }
                case .downloading:
                    VStack(alignment: .leading, spacing: 10) {
                        ProgressView(value: model.updateProgress)
                            .progressViewStyle(.linear).tint(Brand.amber).frame(height: 6)
                        HStack {
                            Spacer()
                            Button("Cancel") { model.cancelUpdate() }
                                .buttonStyle(OutlineButtonStyle(accent: Brand.amber))
                                .pointingCursor()
                        }
                    }
                case .ready:
                    HStack(spacing: 10) {
                        PrimaryAction(title: "Install & Restart", symbol: "arrow.triangle.2.circlepath",
                                      accent: Brand.amber) { model.installUpdate() }
                        Button("Cancel") { model.cancelUpdate() }
                            .buttonStyle(OutlineButtonStyle(accent: Brand.muted))
                            .pointingCursor()
                    }
                default:
                    VStack(alignment: .leading, spacing: 10) {
                        PrimaryAction(title: model.updateState == .checking ? "Checking…" : "Check for updates",
                                      symbol: "arrow.triangle.2.circlepath", accent: Brand.amber) {
                            model.checkForUpdates()
                        }
                        .disabled(model.updateState == .checking)
                        if !model.updateNotes.isEmpty {
                            HStack {
                                Spacer()
                                Button { model.showReleaseNotes = true } label: {
                                    Label("What's new", systemImage: "doc.text")
                                }
                                .buttonStyle(OutlineButtonStyle(accent: Brand.amber))
                                .pointingCursor()
                            }
                        }
                    }
                }
            }
            GlassCard {
                SectionTitle(text: "Notifications")
                Text("OpenHop uses macOS notifications for free-limit hits, new IPs and lifecycle results. Clicking a notification reopens the dashboard.")
                    .font(.system(size: 11)).foregroundStyle(Brand.muted)
                if model.notificationPermissionDenied {
                    Text("Notifications are off. Enable them in System Settings → Notifications → OpenHop.")
                        .font(.system(size: 11)).foregroundStyle(Brand.warn)
                }
                PrimaryAction(title: "Send test notification", symbol: "bell.badge", accent: Brand.amber) {
                    model.sendTestNotification()
                }
            }
        }
        .sheet(isPresented: Binding(get: { model.showReleaseNotes },
                                    set: { model.showReleaseNotes = $0 })) {
            ReleaseNotesView(notes: model.updateNotes)
        }
    }
}

private struct ReleaseNotesView: View {
    let notes: String
    @Environment(\.dismiss) private var dismiss

    var body: some View {
        VStack(alignment: .leading, spacing: 16) {
            HStack {
                Label("What's new", systemImage: "doc.text")
                    .font(.system(size: 16, weight: .bold))
                    .foregroundStyle(Brand.text)
                Spacer()
                Button { dismiss() } label: {
                    Image(systemName: "xmark")
                        .font(.system(size: 11, weight: .bold))
                        .foregroundStyle(Brand.muted)
                }
                .buttonStyle(.plain).pointingCursor()
            }
            ScrollView {
                Text(notes)
                    .font(.system(size: 12))
                    .foregroundStyle(Brand.muted)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .textSelection(.enabled)
            }
            .frame(minHeight: 140, maxHeight: 360)
            PrimaryAction(title: "Done", symbol: "checkmark.circle", accent: Brand.green) { dismiss() }
        }
        .padding(20)
        .frame(width: 540)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Color.white.opacity(0.09), lineWidth: 1))
        .shadow(color: .black.opacity(0.22), radius: 8, y: 4)
        .padding(20)
        .background(Brand.bg)
    }
}

private struct ToastView: View {
    let toast: AppToast
    @EnvironmentObject var model: DashboardModel

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: icon)
                .font(.system(size: 15, weight: .bold))
                .foregroundStyle(toneColor(toast.tone))
            VStack(alignment: .leading, spacing: 3) {
                Text(toast.title)
                    .font(.system(size: 12, weight: .semibold))
                    .foregroundStyle(Brand.text)
                Text(toast.detail)
                    .font(.system(size: 11))
                    .foregroundStyle(Brand.muted)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            Button { model.dismissToast() } label: {
                Image(systemName: "xmark")
                    .font(.system(size: 10, weight: .bold))
                    .foregroundStyle(Brand.muted)
            }
            .buttonStyle(.plain)
            .pointingCursor()
        }
        .padding(14)
        .frame(width: 330, alignment: .leading)
        .background(.ultraThinMaterial, in: RoundedRectangle(cornerRadius: 12, style: .continuous))
        .overlay(RoundedRectangle(cornerRadius: 12, style: .continuous).stroke(Color.white.opacity(0.10), lineWidth: 1))
        .overlay(alignment: .leading) {
            RoundedRectangle(cornerRadius: 2)
                .fill(toneColor(toast.tone))
                .frame(width: 3)
                .padding(.vertical, 12)
        }
        .shadow(color: .black.opacity(0.35), radius: 12, y: 6)
    }

    private var icon: String {
        switch toast.tone {
        case .success: return "checkmark.circle.fill"
        case .info: return "info.circle.fill"
        case .warn: return "exclamationmark.triangle.fill"
        case .error: return "xmark.circle.fill"
        }
    }
}

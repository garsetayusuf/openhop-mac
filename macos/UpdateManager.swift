import Foundation

enum UpdateState: Equatable {
    case idle, checking, upToDate
    case available(String)
    case downloading, ready
    case failed(String)
}

private struct GitHubRelease: Decodable {
    let tagName: String
    let body: String?
    let assets: [GitHubAsset]
    enum CodingKeys: String, CodingKey {
        case tagName = "tag_name", body, assets
    }
}

private struct GitHubAsset: Decodable {
    let name: String
    let browserDownloadURL: String
    enum CodingKeys: String, CodingKey {
        case name, browserDownloadURL = "browser_download_url"
    }
}

final class UpdateManager {
    static let appVersion = Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "0"
    private static let api = "https://api.github.com/repos/garsetayusuf/openhop-mac/releases/latest"

    var onState: ((UpdateState, String, Double) -> Void)?
    private var dataTask: URLSessionDataTask?
    private var downloadTask: URLSessionDownloadTask?
    private(set) var latestVersion: String?
    private(set) var dmgURL: URL?
    private(set) var notes = ""
    private var dmgPath: String {
        let dir = NSHomeDirectory() + "/Library/Caches/openhop/updates"
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        return dir + "/OpenHop.dmg"
    }

    static func compareVersions(_ a: String, _ b: String) -> Int {
        let ax = a.trimmingCharacters(in: .init(charactersIn: "v")).split(separator: ".").map { Int($0) ?? 0 }
        let bx = b.trimmingCharacters(in: .init(charactersIn: "v")).split(separator: ".").map { Int($0) ?? 0 }
        for i in 0..<max(ax.count, bx.count) {
            let l = i < ax.count ? ax[i] : 0
            let r = i < bx.count ? bx[i] : 0
            if l != r { return l > r ? 1 : -1 }
        }
        return 0
    }

    func check() {
        guard dataTask == nil, downloadTask == nil else { return }
        emit(.checking, "Checking for updates…", 0)
        var req = URLRequest(url: URL(string: Self.api)!)
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        dataTask = URLSession.shared.dataTask(with: req) { [weak self] data, _, error in
            guard let self else { return }
            DispatchQueue.main.async { self.dataTask = nil }
            guard let data, error == nil,
                  let release = try? JSONDecoder().decode(GitHubRelease.self, from: data) else {
                self.emit(.failed("Could not reach GitHub Releases."), "", 0)
                return
            }
            let version = release.tagName
            self.latestVersion = version
            self.notes = release.body ?? ""
            self.dmgURL = release.assets.first { $0.name == "OpenHop.dmg" }
                .flatMap { URL(string: $0.browserDownloadURL) }
            if Self.compareVersions(version, Self.appVersion) > 0 {
                guard self.dmgURL != nil else {
                    self.emit(.failed("Release \(version) has no OpenHop.dmg asset."), "", 0)
                    return
                }
                self.emit(.available(version), "Version \(version) is available.", 0)
            } else {
                self.emit(.upToDate, "You are up to date (v\(Self.appVersion)).", 0)
            }
        }
        dataTask?.resume()
    }

    func download() {
        guard let url = dmgURL, downloadTask == nil else { return }
        emit(.downloading, "Downloading \(url.lastPathComponent)…", 0)
        downloadTask = URLSession.shared.downloadTask(with: url) { [weak self] temp, _, error in
            guard let self else { return }
            DispatchQueue.main.async { self.downloadTask = nil }
            if let cancelled = error as? URLError, cancelled.code == .cancelled { return }
            guard let temp, error == nil else {
                self.emit(.failed("Download failed."), "", 0)
                return
            }
            do {
                try? FileManager.default.removeItem(atPath: self.dmgPath)
                try FileManager.default.moveItem(at: temp, to: URL(fileURLWithPath: self.dmgPath))
                self.emit(.ready, "Update downloaded. Ready to install.", 1)
            } catch {
                self.emit(.failed("Could not save the update: \(error.localizedDescription)"), "", 0)
            }
        }
        downloadTask?.resume()
    }

    func cancel() {
        downloadTask?.cancel()
        downloadTask = nil
        try? FileManager.default.removeItem(atPath: dmgPath)
        emit(checkResultState(), "Download cancelled.", 0)
    }

    private func checkResultState() -> UpdateState {
        guard let latestVersion, Self.compareVersions(latestVersion, Self.appVersion) > 0 else { return .upToDate }
        return .available(latestVersion)
    }

    func install(completion: @escaping (Bool, String) -> Void) {
        let dmg = dmgPath
        let mount = FileManager.default.temporaryDirectory.appendingPathComponent("openhop-update-mount").path
        let stage = FileManager.default.temporaryDirectory.appendingPathComponent("openhop-update-stage").path
        DispatchQueue.global(qos: .userInitiated).async {
            func run(_ exe: String, _ args: [String]) -> (Int32, String) {
                let p = Process(); p.executableURL = URL(fileURLWithPath: exe); p.arguments = args
                let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
                do { try p.run() } catch { return (-1, error.localizedDescription) }
                let data = pipe.fileHandleForReading.readDataToEndOfFile(); p.waitUntilExit()
                return (p.terminationStatus, String(data: data, encoding: .utf8) ?? "")
            }
            func cleanup(keepBackup: Bool = false) {
                try? FileManager.default.removeItem(atPath: mount)
                if !keepBackup { try? FileManager.default.removeItem(atPath: stage) }
            }
            try? FileManager.default.removeItem(atPath: mount)
            try? FileManager.default.removeItem(atPath: stage)
            try? FileManager.default.createDirectory(atPath: mount, withIntermediateDirectories: true)
            try? FileManager.default.createDirectory(atPath: stage, withIntermediateDirectories: true)
            let attach = run("/usr/bin/hdiutil", ["attach", dmg, "-nobrowse", "-quiet", "-mountpoint", mount])
            guard attach.0 == 0 else {
                cleanup()
                DispatchQueue.main.async { completion(false, "Could not mount the update (exit \(attach.0)).") }
                return
            }
            let source = mount + "/OpenHop.app"
            let staged = stage + "/OpenHop.app"
            guard FileManager.default.fileExists(atPath: source) else {
                _ = run("/usr/bin/hdiutil", ["detach", mount, "-quiet"])
                cleanup()
                DispatchQueue.main.async { completion(false, "The DMG has no OpenHop.app.") }
                return
            }
            let copy = run("/bin/cp", ["-R", source, staged])
            _ = run("/usr/bin/hdiutil", ["detach", mount, "-quiet"])
            guard copy.0 == 0 else {
                cleanup()
                DispatchQueue.main.async { completion(false, "Could not stage the update (exit \(copy.0)).") }
                return
            }
            let target = "/Applications/OpenHop.app"
            let backup = stage + "/OpenHop-old.app"
            let moveOld = run("/bin/mv", [target, backup])
            let install = run("/bin/mv", [staged, target])
            if install.0 != 0 {
                var message = "Could not replace the app (exit \(install.0))."
                var keepBackup = false
                if moveOld.0 == 0 {
                    let rollback = run("/bin/mv", [backup, target])
                    if rollback.0 != 0 {
                        message += " Could not restore the previous app (exit \(rollback.0)); backup kept at \(backup)."
                        keepBackup = true
                    }
                }
                cleanup(keepBackup: keepBackup)
                DispatchQueue.main.async { completion(false, message) }
                return
            }
            cleanup()
            let reopen = run("/usr/bin/open", ["-n", target])
            guard reopen.0 == 0 else {
                DispatchQueue.main.async { completion(false, "Installed but could not relaunch (exit \(reopen.0)).") }
                return
            }
            DispatchQueue.main.async { completion(true, "Installed. Restarting…") }
        }
    }

    func emit(_ state: UpdateState, _ message: String, _ progress: Double) {
        DispatchQueue.main.async { self.onState?(state, message, progress) }
    }
}

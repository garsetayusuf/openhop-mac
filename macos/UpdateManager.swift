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
    static let publicKeyBase64 = "Tyq6eOuHXEb4psJHQmwbjfrxT7IvCTpzdxtYjgNMXb8="

    var onState: ((UpdateState, String, Double) -> Void)?
    private var dataTask: URLSessionDataTask?
    private var downloadTask: URLSessionDownloadTask?
    private var downloadGeneration = 0
    private var checkGeneration = 0
    private var progressTimer: Timer?

    private func stopProgressTimer() {
        progressTimer?.invalidate()
        progressTimer = nil
    }
    private(set) var latestVersion: String?
    private(set) var dmgURL: URL?
    private(set) var manifestURL: URL?
    private(set) var notes = ""
    private static let isTestBuild = Bundle.main.bundleIdentifier?.hasSuffix(".test") == true
    private var dmgPath: String {
        let dir = NSHomeDirectory() + (Self.isTestBuild
            ? "/Library/Caches/openhop-test/updates" : "/Library/Caches/openhop/updates")
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        return dir + "/OpenHop.dmg"
    }
    private var installTarget: String {
        Self.isTestBuild
            ? NSHomeDirectory() + "/Library/Application Support/openhop-test/OpenHop.app"
            : "/Applications/OpenHop.app"
    }
    private var manifestPath: String { dmgPath + ".sha256" }

    static let setupIdentityKey = "openhop.setupIdentity"

    static func appIdentity(_ bundle: Bundle = .main) -> String {
        let path = bundle.executableURL?.path ?? bundle.bundlePath
        let attrs = try? FileManager.default.attributesOfItem(atPath: path)
        let mod = (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
        let size = (attrs?[.size] as? NSNumber)?.intValue ?? 0
        return "\(path)|\(Int(mod))|\(size)"
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
        guard downloadTask == nil else { return }
        checkGeneration += 1
        let generation = checkGeneration
        dataTask?.cancel()
        dataTask = nil
        emit(.checking, "Checking for updates…", 0)
        var req = URLRequest(url: URL(string: Self.api)!)
        req.setValue("application/vnd.github+json", forHTTPHeaderField: "Accept")
        dataTask = URLSession.shared.dataTask(with: req) { [weak self] data, response, error in
            guard let self else { return }
            guard self.checkGeneration == generation else { return }
            self.dataTask = nil
            if (response as? HTTPURLResponse)?.statusCode == 404 {
                self.emit(.upToDate, "No releases published yet.", 0)
                return
            }
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
            self.manifestURL = release.assets.first { $0.name == "OpenHop.dmg.sha256" }
                .flatMap { URL(string: $0.browserDownloadURL) }
            if Self.compareVersions(version, Self.appVersion) > 0 {
                guard self.dmgURL != nil else {
                    self.emit(.failed("Release \(version) has no OpenHop.dmg asset."), "", 0)
                    return
                }
                guard self.manifestURL != nil else {
                    self.emit(.failed("Release \(version) has no OpenHop.dmg.sha256 asset."), "", 0)
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
        guard let url = dmgURL, let manifest = manifestURL, downloadTask == nil else { return }
        downloadGeneration += 1
        let generation = downloadGeneration
        emit(.downloading, "Downloading update…", 0)
        downloadTask = URLSession.shared.downloadTask(with: url) { [weak self] temp, _, error in
            guard let self else { return }
            self.stopProgressTimer()
            if let cancelled = error as? URLError, cancelled.code == .cancelled {
                DispatchQueue.main.async { self.downloadTask = nil }
                return
            }
            guard let temp, error == nil else {
                DispatchQueue.main.async { self.downloadTask = nil }
                self.emit(.failed("Download failed."), "", 0)
                return
            }
            URLSession.shared.dataTask(with: manifest) { [weak self] data, _, merr in
                guard let self else { return }
                guard self.downloadGeneration == generation else {
                    try? FileManager.default.removeItem(at: temp)
                    return
                }
                guard let data, merr == nil else {
                    try? FileManager.default.removeItem(at: temp)
                    DispatchQueue.main.async { self.downloadTask = nil }
                    self.emit(.failed("Could not download the update manifest."), "", 0)
                    return
                }
                self.verifyAndFinish(temp: temp, manifestData: data, generation: generation)
            }.resume()
        }
        downloadTask?.resume()
        DispatchQueue.main.async {
            self.progressTimer?.invalidate()
            self.progressTimer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in
                guard let self, let task = self.downloadTask else { return }
                let fraction = min(0.99, max(0, task.progress.fractionCompleted))
                self.emit(.downloading, "Downloading update… \(Int(fraction * 100))%", fraction)
            }
        }
    }

    private func verifyAndFinish(temp: URL, manifestData: Data, generation: Int) {
        let home = NSHomeDirectory()
        let py = home + "/Library/Application Support/openhop/venv/bin/python"
        let installedScript = home + "/Library/Application Support/openhop/src/verify_update.py"
        let script = FileManager.default.fileExists(atPath: installedScript)
            ? installedScript
            : Bundle.main.resourceURL?.appendingPathComponent("payload/src/verify_update.py").path
        let expected = (latestVersion ?? "").trimmingCharacters(in: CharacterSet(charactersIn: "vV"))
        let manifest = manifestPath
        let dmg = dmgPath
        DispatchQueue.global(qos: .userInitiated).async {
            guard self.downloadGeneration == generation else {
                try? FileManager.default.removeItem(at: temp)
                return
            }
            guard let script else {
                try? FileManager.default.removeItem(at: temp)
                try? FileManager.default.removeItem(atPath: manifest)
                guard self.downloadGeneration == generation else { return }
                DispatchQueue.main.async { self.downloadTask = nil }
                self.emit(.failed("Update verifier is missing — reinstall OpenHop."), "", 0)
                return
            }
            do {
                try manifestData.write(to: URL(fileURLWithPath: manifest))
            } catch {
                try? FileManager.default.removeItem(at: temp)
                guard self.downloadGeneration == generation else { return }
                DispatchQueue.main.async { self.downloadTask = nil }
                self.emit(.failed("Could not save the update manifest: \(error.localizedDescription)"), "", 0)
                return
            }
            let p = Process()
            p.executableURL = URL(fileURLWithPath: py)
            p.arguments = [script, temp.path, manifest, Self.publicKeyBase64, expected]
            let pipe = Pipe(); p.standardOutput = pipe; p.standardError = pipe
            do { try p.run() } catch {
                try? FileManager.default.removeItem(at: temp)
                try? FileManager.default.removeItem(atPath: manifest)
                guard self.downloadGeneration == generation else { return }
                DispatchQueue.main.async { self.downloadTask = nil }
                self.emit(.failed("Could not run update verification."), "", 0)
                return
            }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            let out = String(data: data, encoding: .utf8)?
                .trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            guard p.terminationStatus == 0 else {
                try? FileManager.default.removeItem(at: temp)
                try? FileManager.default.removeItem(atPath: manifest)
                guard self.downloadGeneration == generation else { return }
                DispatchQueue.main.async { self.downloadTask = nil }
                let reason = out.isEmpty ? "unknown reason" : out
                self.emit(.failed("Update verification failed: \(reason)"), "", 0)
                return
            }
            guard self.downloadGeneration == generation else {
                try? FileManager.default.removeItem(at: temp)
                return
            }
            do {
                try? FileManager.default.removeItem(atPath: dmg)
                try FileManager.default.moveItem(at: temp, to: URL(fileURLWithPath: dmg))
                DispatchQueue.main.async { self.downloadTask = nil }
                self.emit(.ready, "Update verified and ready to install.", 1)
            } catch {
                try? FileManager.default.removeItem(at: temp)
                try? FileManager.default.removeItem(atPath: manifest)
                guard self.downloadGeneration == generation else { return }
                DispatchQueue.main.async { self.downloadTask = nil }
                self.emit(.failed("Could not save the update: \(error.localizedDescription)"), "", 0)
            }
        }
    }

    func cancel() {
        downloadGeneration += 1
        downloadTask?.cancel()
        downloadTask = nil
        try? FileManager.default.removeItem(atPath: dmgPath)
        try? FileManager.default.removeItem(atPath: manifestPath)
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
            func reason(_ result: (Int32, String)) -> String {
                let out = result.1.trimmingCharacters(in: .whitespacesAndNewlines)
                return "exit \(result.0)" + (out.isEmpty ? "" : ": \(out)")
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
                DispatchQueue.main.async { completion(false, "Could not mount the update (\(reason(attach))).") }
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
            try? FileManager.default.removeItem(atPath: staged)
            let stageError: Error?
            do {
                try FileManager.default.copyItem(atPath: source, toPath: staged)
                stageError = nil
            } catch {
                stageError = error
            }
            _ = run("/usr/bin/hdiutil", ["detach", mount, "-quiet"])
            if let stageError {
                cleanup()
                DispatchQueue.main.async { completion(false, "Could not stage the update: \(stageError.localizedDescription)") }
                return
            }
            let target = self.installTarget
            let exeName = (NSDictionary(contentsOfFile: staged + "/Contents/Info.plist")?["CFBundleExecutable"] as? String) ?? "OpenHop"
            let newExe = staged + "/Contents/MacOS/" + exeName
            let attrs = try? FileManager.default.attributesOfItem(atPath: newExe)
            let mod = (attrs?[.modificationDate] as? Date)?.timeIntervalSince1970 ?? 0
            let size = (attrs?[.size] as? NSNumber)?.intValue ?? 0
            let backup = stage + "/OpenHop-old.app"
            // Back up the installed app before touching it; without a copy there is no rollback.
            var backupResult: (Int32, String)?
            if FileManager.default.fileExists(atPath: target) {
                backupResult = run("/bin/cp", ["-Rp", target, backup])
                if backupResult?.0 != 0 { try? FileManager.default.removeItem(atPath: backup) }
            }
            let backupOK = backupResult?.0 == 0

            // Remove the old app first so mv cannot land inside an existing directory.
            let remove = run("/bin/rm", ["-rf", target])
            if remove.0 != 0 {
                // Escape for a shell single-quoted path, then for the AppleScript string literal.
                let quoted = target.replacingOccurrences(of: "'", with: "'\\''")
                let shell = "rm -rf '\(quoted)'"
                let script = shell.replacingOccurrences(of: "\\", with: "\\\\")
                                  .replacingOccurrences(of: "\"", with: "\\\"")
                let admin = run("/usr/bin/osascript", ["-e", "do shell script \"\(script)\" with administrator privileges"])
                if admin.0 != 0 {
                    var message = "Could not remove the installed app (\(reason(remove)); admin retry: \(reason(admin)))."
                    if let backupResult, backupResult.0 != 0 {
                        message += " Backup failed: \(reason(backupResult))."
                    }
                    cleanup(keepBackup: backupOK)
                    DispatchQueue.main.async { completion(false, message) }
                    return
                }
            }

            let install = run("/bin/mv", [staged, target])
            if install.0 != 0 {
                var message = "Could not replace the app (\(reason(install)))."
                var keepBackup = false
                if backupOK {
                    let restore = run("/bin/cp", ["-Rp", backup, target])
                    if restore.0 != 0 {
                        message += " Could not restore the previous app (\(reason(restore))); backup kept at \(backup)."
                        keepBackup = true
                    }
                } else if let backupResult {
                    message += " No usable backup: \(reason(backupResult))."
                }
                cleanup(keepBackup: keepBackup)
                DispatchQueue.main.async { completion(false, message) }
                return
            }
            UserDefaults.standard.set("\(target)/Contents/MacOS/\(exeName)|\(Int(mod))|\(size)",
                                      forKey: UpdateManager.setupIdentityKey)
            cleanup()
            let reopen = run("/usr/bin/open", ["-n", target])
            guard reopen.0 == 0 else {
                DispatchQueue.main.async { completion(false, "Installed but could not relaunch (\(reason(reopen))).") }
                return
            }
            DispatchQueue.main.async { completion(true, "Installed. Restarting…") }
        }
    }

    func emit(_ state: UpdateState, _ message: String, _ progress: Double) {
        DispatchQueue.main.async { self.onState?(state, message, progress) }
    }
}

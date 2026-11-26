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
                self.emit(.available(version), "Version \(version) is available.", 0)
            } else {
                self.emit(.upToDate, "You are up to date (v\(Self.appVersion)).", 0)
            }
        }
        dataTask?.resume()
    }

    func emit(_ state: UpdateState, _ message: String, _ progress: Double) {
        DispatchQueue.main.async { self.onState?(state, message, progress) }
    }
}

import Foundation

/// The ordered `pal` work needed to turn a folder into a watched silo.
/// Keeping the plan separate from AppKit makes the command contract easy to
/// test without opening a folder picker or launching a subprocess.
struct PalJobSpec: Equatable, Sendable {
    let title: String
    let siloSlug: String?
    let arguments: [String]
}

enum SiloAddWorkflow {
    static func normalizedPath(for folderURL: URL) -> String {
        folderURL.resolvingSymlinksInPath().standardizedFileURL.path
    }

    static func jobs(for folderURL: URL, imageVision: Bool = false) -> [PalJobSpec] {
        jobs(for: [folderURL], imageVision: imageVision)
    }

    /// Index every selected folder in order, then reconcile watcher services
    /// once. This avoids launching several embedding jobs in parallel when the
    /// Finder Service is used on a multi-selection.
    static func jobs(for folderURLs: [URL], imageVision: Bool = false) -> [PalJobSpec] {
        guard !folderURLs.isEmpty else { return [] }
        let folders = folderURLs.map { url -> (path: String, name: String) in
            let path = normalizedPath(for: url)
            return (path, URL(fileURLWithPath: path).lastPathComponent)
        }
        let pulls = folders.map {
            var arguments = ["pull", $0.path]
            if imageVision { arguments.append("--image-vision") }
            return PalJobSpec(title: "Index \($0.name)", siloSlug: nil, arguments: arguments)
        }
        let watcherTitle = folders.count == 1
            ? "Start watcher for \(folders[0].name)"
            : "Start watchers for \(folders.count) silos"
        return pulls + [PalJobSpec(title: watcherTitle, siloSlug: nil, arguments: ["daemon", "sync"])]
    }
}

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
    /// `pal pull` exits with this (EX_TEMPFAIL) when another pull already holds
    /// the folder's lock; see pal._acquire_pull_lock.
    static let alreadyIndexingExit: Int32 = 75

    static func normalizedPath(for folderURL: URL) -> String {
        folderURL.resolvingSymlinksInPath().standardizedFileURL.path
    }

    /// The folder a one-shot `pal pull <folder>` indexes, normalized so two
    /// jobs for the same folder compare equal; nil for any other command.
    static func pullTarget(of arguments: [String]) -> String? {
        guard arguments.count >= 2, arguments[0] == "pull", !arguments.contains("--watch"),
              !arguments[1].hasPrefix("-") else { return nil }
        return normalizedPath(for: URL(fileURLWithPath: arguments[1]))
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

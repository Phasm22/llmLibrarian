import Foundation

struct AppBuildInfo {
    let version: String
    let build: String
    let date: Date?
    let commit: String
    let branch: String
    let state: String
    let source: String?

    init(info: [String: Any] = Bundle.main.infoDictionary ?? [:]) {
        version = info["CFBundleShortVersionString"] as? String ?? "Unknown"
        build = info["CFBundleVersion"] as? String ?? "Unknown"
        date = (info["LibrarianBuildDate"] as? String).flatMap { ISO8601DateFormatter().date(from: $0) }
        commit = info["LibrarianBuildCommit"] as? String ?? "Unknown"
        branch = info["LibrarianBuildBranch"] as? String ?? "Unknown"
        state = info["LibrarianBuildState"] as? String ?? "unknown"
        source = info["LibrarianBuildSource"] as? String
    }

    var sourceState: String {
        switch state {
        case "modified": return "Includes uncommitted local changes"
        case "clean": return "Clean checkout at build time"
        default: return "Source state not recorded"
        }
    }

    var report: String {
        """
        llmLibrarian \(version) (\(build))
        Built: \(date.map { ISO8601DateFormatter().string(from: $0) } ?? "Not recorded")
        Commit: \(commit)
        Branch: \(branch)
        \(sourceState)
        Source: \(source ?? "Not recorded")
        """
    }
}

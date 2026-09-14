import Foundation

enum HealthState: Sendable, Hashable {
    case running, attention, stopped, unknown

    var label: String {
        switch self {
        case .running: return "Running"
        case .attention: return "Needs attention"
        case .stopped: return "Stopped"
        case .unknown: return "Unknown"
        }
    }
}

struct Silo: Identifiable, Hashable, Sendable {
    let slug: String
    let displayName: String
    let path: String
    let filesIndexed: Int
    let chunks: Int
    let updated: Date?
    let imageVision: Bool
    /// Excluded from unscoped retrieval; reachable only via an explicit `--in <slug>`.
    let isPrivate: Bool
    /// Machine that indexed this silo. Silos are machine-local, so a roster from
    /// the Linux box and one from this Mac are different sets, not a sync gap.
    let host: String

    var id: String { slug }
    var pathExists: Bool { FileManager.default.fileExists(atPath: path) }
    var tildePath: String { (path as NSString).abbreviatingWithTildeInPath }
}

struct ServiceStatus: Identifiable, Hashable, Sendable {
    enum Kind: Hashable, Sendable { case chroma, mcp, watcher }

    let kind: Kind
    let label: String
    let title: String
    var subtitle: String
    var installed: Bool
    var loaded: Bool
    var pid: Int?
    var lastExit: Int?
    var uptime: String?
    var memoryMB: Double?
    var probeOK: Bool?
    var probeDetail: String?
    var siloSlug: String?
    var lockStale = false
    var logPath: String
    var plistPath: String

    var id: String { label }

    var state: HealthState {
        if !installed { return .unknown }
        guard loaded, pid != nil else { return .stopped }
        if let ok = probeOK, !ok { return .attention }
        if lockStale { return .attention }
        return .running
    }

    var stateText: String {
        switch state {
        case .running: return "Running"
        case .attention:
            if let ok = probeOK, !ok { return "Process up, endpoint not responding" }
            return "Running, lock file stale"
        case .stopped: return loaded ? "Loaded, not running" : "Not loaded"
        case .unknown: return "Not installed"
        }
    }
}

struct QuerySource: Hashable, Sendable {
    let file: String
    let path: String
    let chunks: Int
}

struct QueryRecord: Identifiable, Hashable, Sendable {
    let id: Int
    let timestamp: Date?
    let tool: String
    let silo: String?
    let queries: [String]
    let chunks: Int
    let sources: [QuerySource]
    let truncated: Bool
    let errored: Bool
}

struct MCPHealth: Hashable, Sendable {
    let ok: Bool
    let version: String?
    let startedAt: Date?
    let dbPath: String?
}

struct Snapshot: Sendable {
    var silos: [Silo] = []
    var services: [ServiceStatus] = []
    var watchers: [ServiceStatus] = []
    var queries: [QueryRecord] = []
    var mcpHealth: MCPHealth?
    var chromaReachable: Bool?
    var unindexedBookmarks: [String] = []
    var registryError: String?
}

enum Dates {
    static let isoFractional: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        return f
    }()
    static let iso: ISO8601DateFormatter = {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime]
        return f
    }()
    static let relative: RelativeDateTimeFormatter = {
        let f = RelativeDateTimeFormatter()
        f.unitsStyle = .short
        return f
    }()

    static func parse(_ s: String?) -> Date? {
        guard let s, !s.isEmpty else { return nil }
        return isoFractional.date(from: s) ?? iso.date(from: s)
    }

    static func relativeString(_ d: Date?) -> String {
        guard let d else { return "never" }
        if Date().timeIntervalSince(d) < 60 { return "just now" }
        return relative.localizedString(for: d, relativeTo: Date())
    }

    static func absoluteString(_ d: Date?) -> String {
        guard let d else { return "—" }
        return d.formatted(date: .abbreviated, time: .shortened)
    }
}

extension Int {
    var grouped: String { self.formatted(.number.grouping(.automatic)) }
}

enum SidebarItem: String, CaseIterable, Identifiable, Hashable, Sendable {
    case overview, silos, services, activity
    var id: String { rawValue }
    var title: String {
        switch self {
        case .overview: return "Overview"
        case .silos: return "Silos"
        case .services: return "Services"
        case .activity: return "Activity"
        }
    }
    var symbol: String {
        switch self {
        case .overview: return "gauge.with.needle"
        case .silos: return "folder"
        case .services: return "gearshape.2"
        case .activity: return "clock"
        }
    }
    var shortcut: Character { Character(String(SidebarItem.allCases.firstIndex(of: self)! + 1)) }
}

/// One concrete, actionable problem shown on the Overview.
struct Issue: Identifiable, Hashable, Sendable {
    let id: String
    let severity: HealthState
    let title: String
    let detail: String
    let page: SidebarItem
    var serviceLabel: String? = nil
    var siloSlug: String? = nil
}

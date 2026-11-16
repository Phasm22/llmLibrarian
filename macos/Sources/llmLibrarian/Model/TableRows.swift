import Foundation

/// Resolve displayed values before sorting, so headers order exactly what the
/// user sees, with numeric counts and actual dates rather than formatted text.
struct CollectionTableRow: Identifiable {
    let silo: Silo
    let status: String
    var stats = ActivityCollectionStats()
    var id: String { silo.id }
    var name: String { silo.displayName }
    var folder: String { silo.tildePath }
    var files: Int { silo.filesIndexed }
    var chunks: Int { silo.chunks }
    var updated: Date { silo.updated ?? .distantPast }
    var scope: String { silo.isPrivate ? "Private" : "Shared" }
    var changed: Int { stats.changes }
    var searches: Int { stats.searches }
    var lastActivity: Date { stats.lastActivity ?? .distantPast }
}

struct CollectionActivityRow<Record: Identifiable>: Identifiable {
    let record: Record
    let collectionName: String
    var id: Record.ID { record.id }
}

extension ServiceStatus {
    var sortPID: Int { pid ?? -1 }
    var sortMemory: Double { memoryMB ?? -1 }
    var sortLastExit: Int { lastExit ?? -1 }

    /// The display uses duration components (e.g. "2d 3h"), never lexical order.
    var uptimeSeconds: Int {
        (uptime ?? "").split(separator: " ").reduce(0) { total, part in
            let scale = [Character("d"): 86400, "h": 3600, "m": 60, "s": 1]
            guard let unit = part.last, let multiplier = scale[unit], let amount = Int(part.dropLast()) else { return total }
            return total + amount * multiplier
        }
    }
}

extension CollectionActivityRow where Record == ActivitySearchRow {
    var queryText: String { record.query ?? "(query text not kept)" }
}

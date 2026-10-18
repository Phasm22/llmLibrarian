import Foundation

/// Overview time range. Periods start at local midnight and end now; the
/// previous period is the same span one window earlier (see activity_ledger.py).
enum ActivityWindow: String, CaseIterable, Identifiable, Hashable, Sendable {
    case today, week, month

    var id: String { rawValue }
    var days: Int { switch self { case .today: 1; case .week: 7; case .month: 30 } }
    var title: String { switch self { case .today: "Today"; case .week: "7 days"; case .month: "30 days" } }
    /// "Research was the busiest collection <span>."
    var span: String { switch self { case .today: "today"; case .week: "this week"; case .month: "in the last 30 days" } }
    /// "… compared with <compared>"
    var compared: String {
        switch self {
        case .today: "the same time yesterday"
        case .week: "the previous week"
        case .month: "the 30 days before"
        }
    }
}

struct ActivityFile: Hashable, Sendable {
    let path: String
    let kind: String   // added | updated | removed
    let at: Date
    var name: String { (path as NSString).lastPathComponent }
}

struct ActivityChangeSilo: Identifiable, Hashable, Sendable {
    let slug: String
    var added = 0, updated = 0, removed = 0
    var previousTotal = 0
    var files: [ActivityFile] = []
    var id: String { slug }
    var total: Int { added + updated + removed }
}

struct ActivityCollectionEvent: Hashable, Sendable {
    let slug: String
    let added: Bool
    let files: Int
    let at: Date
}

struct ActivityBucket: Identifiable, Hashable, Sendable {
    let start: Date
    var added = 0, updated = 0, removed = 0
    var searches = 0, withoutSources = 0
    var id: Date { start }
}

struct ActivitySearch: Identifiable, Hashable, Sendable {
    let id: Int
    let at: Date
    let silo: String?
    let query: String?
    let sourcesFound: Bool
    let client: String
}

struct ActivityUseSilo: Identifiable, Hashable, Sendable {
    let slug: String
    var searches = 0
    var uniqueFiles = 0
    var id: String { slug }
}

struct ActivityDocument: Identifiable, Hashable, Sendable {
    let slug: String
    let path: String
    let searches: Int
    var id: String { slug + "\u{0}" + path }
    var name: String { (path as NSString).lastPathComponent }
}

struct ActivityFailureGroup: Identifiable, Hashable, Sendable {
    let slug: String
    let errorClass: String
    var files = 0
    var occurrences = 0
    var sampleError = ""
    var lastSeen: Date
    var paths: [String] = []
    var id: String { slug + "\u{0}" + errorClass }
}

struct ActivityQueryGroup: Identifiable, Hashable, Sendable {
    let query: String
    let silo: String?
    var count = 0
    var lastSeen: Date
    var searchIDs: [Int] = []
    var id: String { (silo ?? "") + "\u{0}" + query }
}

struct ActivityOverview: Hashable, Sendable {
    let window: ActivityWindow
    let periodStart: Date
    let periodEnd: Date
    let previousStart: Date
    let previousEnd: Date
    let filesSince: Date?
    let searchesSince: Date?

    var changeTotals: (added: Int, updated: Int, removed: Int) = (0, 0, 0)
    var previousChangeTotal = 0
    var changeSilos: [ActivityChangeSilo] = []
    var collections: [ActivityCollectionEvent] = []
    var timeline: [ActivityBucket] = []

    var searches: [ActivitySearch] = []
    var previousSearches = 0
    var useSilos: [ActivityUseSilo] = []
    var topDocuments: [ActivityDocument] = []
    var clients: [(name: String, searches: Int)] = []

    var failureGroups: [ActivityFailureGroup] = []
    var noSourceGroups: [ActivityQueryGroup] = []

    var lastChange: [String: Date] = [:]
    var lastSearch: [String: Date] = [:]
    var openFailures: [String: Int] = [:]

    var fullFileComparison: Bool { filesSince.map { $0 <= previousStart } ?? false }
    var fullSearchComparison: Bool { searchesSince.map { $0 <= previousStart } ?? false }
    var changeTotal: Int { changeTotals.added + changeTotals.updated + changeTotals.removed }
    var searchCount: Int { searches.count }
    var withSources: Int { searches.filter(\.sourcesFound).count }
    var withoutSources: Int { searchCount - withSources }

    static func == (a: ActivityOverview, b: ActivityOverview) -> Bool {
        a.window == b.window && a.periodEnd == b.periodEnd && a.searches == b.searches
            && a.changeSilos == b.changeSilos && a.failureGroups == b.failureGroups
            && a.previousSearches == b.previousSearches && a.previousChangeTotal == b.previousChangeTotal
    }

    func hash(into h: inout Hasher) {
        h.combine(window); h.combine(periodEnd); h.combine(searches.count); h.combine(changeTotal)
    }

    /// Same templates as `activity_ledger.overview()["summary"]`; the shared
    /// fixture test pins the wording on both sides.
    func summary(name: (String?) -> String) -> (headline: String, sentences: [String]) {
        var score: [String: Int] = [:]
        for r in changeSilos { score[r.slug, default: 0] += r.total }
        for r in useSilos { score[r.slug, default: 0] += r.searches }
        let busiest = score.sorted { ($0.value, name($1.key)) > ($1.value, name($0.key)) }.first
        let headline = busiest.map { $0.value > 0 } == true
            ? "\(name(busiest!.key)) was the busiest collection \(window.span)."
            : "No library activity \(window.span)."

        var out: [String] = []
        let n = changeTotal
        if n > 0 {
            let top = changeSilos.prefix(2).filter { $0.total > 0 }.map { name($0.slug) }
            if changeSilos.count == 1 {
                out.append("All \(n) document change\(n == 1 ? "" : "s") were in \(top[0]).")
            } else {
                out.append("Most document changes were in \(Self.list(top)).")
            }
        } else if !fullFileComparison, let since = filesSince, since > periodStart {
            out.append("No document changes recorded since history began on \(Self.day(since)).")
        } else {
            out.append("No documents changed.")
        }
        for ev in collections where ev.added {
            out.append("\(name(ev.slug)) was added with \(ev.files) file\(ev.files == 1 ? "" : "s").")
        }
        let s = searchCount
        var line: String
        if fullSearchComparison {
            if s > previousSearches {
                line = "Searches increased to \(s) from \(previousSearches) compared with \(window.compared)"
            } else if s < previousSearches {
                line = "Searches decreased to \(s) from \(previousSearches) compared with \(window.compared)"
            } else {
                line = "Searches held steady at \(s) compared with \(window.compared)"
            }
        } else {
            line = "\(s) search\(s == 1 ? "" : "es") \(window.span)"
        }
        line += withoutSources > 0 ? "; \(withoutSources) returned no sources." : "."
        out.append(line)
        if !failureGroups.isEmpty {
            let nf = failureGroups.reduce(0) { $0 + $1.files }
            out.append("\(nf) file\(nf == 1 ? "" : "s") could not be indexed.")
        }
        return (headline, out)
    }

    static func list(_ names: [String]) -> String {
        guard names.count > 1 else { return names.joined() }
        return names.dropLast().joined(separator: ", ") + " and " + names.last!
    }

    /// yyyy-MM-dd in the local time zone (matches Python's ISO date prefix).
    static func day(_ d: Date, calendar: Calendar = .current) -> String {
        let c = calendar.dateComponents([.year, .month, .day], from: d)
        return String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
    }
}

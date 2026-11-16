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
    /// "vs <previous>" under a tile.
    var previous: String { switch self { case .today: "yesterday"; case .week: "last week"; case .month: "prior 30 days" } }
}

struct ActivityBucket: Identifiable, Hashable, Sendable {
    let start: Date
    var added = 0, updated = 0, removed = 0
    var searches = 0, noSource = 0
    var id: Date { start }
    var changes: Int { added + updated + removed }
}

/// One collection's numbers for the window, plus all-time last activity.
struct ActivityCollectionStats: Hashable, Sendable {
    var changes = 0
    var searches = 0
    var lastChange: Date?
    var lastSearch: Date?
    var openFailures = 0
    var lastActivity: Date? { [lastChange, lastSearch].compactMap { $0 }.max() }
}

struct ActivityFailureGroup: Identifiable, Hashable, Sendable {
    let slug: String
    let errorClass: String
    let files: Int
    let occurrences: Int
    let lastSeen: Date
    let sampleError: String
    var id: String { slug + "\u{0}" + errorClass }
}

struct ActivityCollectionEvent: Hashable, Sendable {
    let slug: String
    let added: Bool
    let files: Int
    let at: Date
}

/// Aggregates only: size is O(collections + buckets), whatever the event volume.
/// Rows behind a number come from the `ActivityLedger.changes/searches/…` detail reads.
struct ActivitySummary: Hashable, Sendable {
    let window: ActivityWindow
    let periodStart: Date
    let periodEnd: Date
    let previousStart: Date
    let previousEnd: Date
    let filesSince: Date?
    let searchesSince: Date?

    var added = 0, updated = 0, removed = 0
    var previousChangeCount = 0
    var searches = 0, previousSearchCount = 0
    var noSource = 0, previousNoSourceCount = 0
    var buckets: [ActivityBucket] = []
    var perSilo: [String: ActivityCollectionStats] = [:]
    var collectionEvents: [ActivityCollectionEvent] = []
    var failures: [ActivityFailureGroup] = []

    var changes: Int { added + updated + removed }
    var fullFileComparison: Bool { filesSince.map { $0 <= previousStart } ?? false }
    var fullSearchComparison: Bool { searchesSince.map { $0 <= previousStart } ?? false }
    /// Nil when the ledger's history does not cover the previous period.
    var previousChanges: Int? { fullFileComparison ? previousChangeCount : nil }
    var previousSearches: Int? { fullSearchComparison ? previousSearchCount : nil }
    var previousNoSource: Int? { fullSearchComparison ? previousNoSourceCount : nil }

    func stats(_ slug: String) -> ActivityCollectionStats { perSilo[slug] ?? ActivityCollectionStats() }

    /// Registered collections ordered by window activity, then name — the
    /// collections table order and the busiest-collection rule, same as Python.
    func ranked(_ collections: [(slug: String, name: String)]) -> [(slug: String, name: String)] {
        collections.sorted {
            let a = stats($0.slug), b = stats($1.slug)
            let x = a.changes + a.searches, y = b.changes + b.searches
            return x != y ? x > y : $0.name.lowercased() < $1.name.lowercased()
        }
    }

    func headline(_ collections: [(slug: String, name: String)]) -> String {
        if let top = ranked(collections).first, stats(top.slug).changes + stats(top.slug).searches > 0 {
            return "\(top.name) was the busiest collection \(window.span)."
        }
        return "No library activity \(window.span)."
    }
}

// MARK: detail rows

struct ActivityChangeRow: Identifiable, Hashable, Sendable {
    let id: Int
    let at: Date
    let slug: String
    let path: String
    let kind: String   // added | updated | removed
    var name: String { (path as NSString).lastPathComponent }
}

struct ActivityHit: Hashable, Sendable {
    let slug: String
    let path: String
    var name: String { (path as NSString).lastPathComponent }
}

struct ActivitySearchRow: Identifiable, Hashable, Sendable {
    let id: Int
    let at: Date
    let slug: String?
    let query: String?
    let sourcesFound: Bool
    let uniqueFiles: Int
    let client: String
    var hits: [ActivityHit] = []
}

struct ActivityDocumentRow: Identifiable, Hashable, Sendable {
    let slug: String
    let path: String
    let searches: Int
    var id: String { slug + "\u{0}" + path }
    var name: String { (path as NSString).lastPathComponent }
}

struct ActivityFailureRow: Identifiable, Hashable, Sendable {
    let id: Int
    let slug: String
    let path: String
    let errorClass: String
    let error: String
    let lastSeen: Date
    let occurrences: Int
    var name: String { (path as NSString).lastPathComponent }
    var remedy: FailureRemedy { FailureRemedy.classify(errorClass: errorClass, error: error) }
    func matches(_ filter: String) -> Bool {
        filter.isEmpty || [path, error, slug, remedy.title].contains { $0.localizedCaseInsensitiveContains(filter) }
    }
}

/// Presentation guidance only; retain the ledger's original error and category.
enum FailureRemedy: String, Sendable {
    case imageModel, encrypted, tooLarge, unsupported, timeout, permission, noText, busy, notFound, unknown

    static func classify(errorClass: String, error: String) -> Self {
        if error.lowercased().contains("image model not ready") { return .imageModel }
        switch errorClass {
        case "encrypted": return .encrypted
        case "too_large": return .tooLarge
        case "unsupported": return .unsupported
        case "timeout": return .timeout
        case "permission": return .permission
        case "no_text": return .noText
        case "busy": return .busy
        case "not_found": return .notFound
        default: return .unknown
        }
    }

    var title: String {
        switch self {
        case .imageModel: return "Image indexing is not ready"
        case .encrypted: return "Files are password-protected"
        case .tooLarge: return "Files exceed the size limit"
        case .unsupported: return "File format is not supported"
        case .timeout: return "Indexing timed out"
        case .permission: return "Files could not be read"
        case .noText: return "No text could be extracted"
        case .busy: return "The index was busy"
        case .notFound: return "Files were not found"
        case .unknown: return "Indexing needs investigation"
        }
    }

    var nextStep: String {
        switch self {
        case .imageModel:
            return "Open the watcher log to identify the image model or dependency that could not start. Fix that issue before reindexing. Pause the watcher if it keeps repeating the same failure; resume it when ready."
        case .encrypted:
            return "Save an unlocked copy of each document in the collection, then reindex. Retrying the same locked file will not help."
        case .tooLarge:
            return "Split or reduce these files, then reindex the collection. Open the log for the size limit that was exceeded."
        case .unsupported:
            return "Export these files to a supported format such as PDF or plain text, then reindex the collection."
        case .timeout:
            return "Check the watcher log for the operation that timed out. Once the local model or service is responding, reindex the collection."
        case .permission:
            return "Reveal the file in Finder and check that it opens and is readable by your account. Fix access, then reindex."
        case .noText:
            return "Open the file to check its content. For a scanned document, save a searchable copy with OCR, then reindex."
        case .busy:
            return "Wait for the current indexing job to finish, then reindex this collection. Check Services if the index stays busy."
        case .notFound:
            return "Check whether the files or collection folder moved. Update the folder location from the collection details, then reindex."
        case .unknown:
            return "Expand a file below to read its full error, then open the watcher log for context. Fix the reported cause before reindexing. Copy the error details if you need help."
        }
    }
}

extension ActivityFailureGroup {
    /// Plain-language cause for an `error_class` from activity_ledger.error_class().
    static func reason(_ errorClass: String) -> String {
        switch errorClass {
        case "encrypted": return "encrypted or password-protected"
        case "too_large": return "over the size limit"
        case "unsupported": return "unsupported format"
        case "timeout": return "timed out"
        case "permission": return "permission denied"
        case "no_text": return "no extractable text"
        case "busy": return "index was busy"
        case "not_found": return "file not found"
        case "other": return "other error"
        default: return errorClass
        }
    }
}

// MARK: Activity page

enum ActivityTab: String, CaseIterable, Identifiable, Hashable, Sendable {
    case searches, changes, problems
    var id: String { rawValue }
    var title: String { switch self { case .searches: "Searches"; case .changes: "Changes"; case .problems: "Problems" } }
}

/// Rows for the Activity page's current tab and filters.
struct ActivityDetail: Sendable {
    var searches: [ActivitySearchRow] = []
    var documents: [ActivityDocumentRow] = []
    var clients: [(name: String, searches: Int)] = []
    var changes: [ActivityChangeRow] = []
    var failures: [ActivityFailureRow] = []
    /// False when there is no ledger (or it could not be read).
    var available = true
}

struct ActivityDetailRequest: Hashable, Sendable {
    let window: ActivityWindow
    let tab: ActivityTab
    let silo: String?
    let noSourceOnly: Bool
    var failureLimit = 200
}

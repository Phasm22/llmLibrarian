import Foundation
import SQLite3

/// Read-only view of `<db>/llmli_activity.sqlite3`, the ledger `activity_ledger.py`
/// writes. Mirrors `overview()` so the app never has to start Python; the shared
/// fixture in tests/fixtures/activity keeps the two in step.
/// Foundation + SQLite only so `test.sh` can compile it alone.
enum ActivityLedger {
    static let basename = "llmli_activity.sqlite3"
    /// Writes tagged with these origins re-index existing files; they are not document changes.
    static let reindexOrigins = ["repair", "rehydrate"]
    static let detailLimit = 20

    static func path(dbPath: String) -> String { (dbPath as NSString).appendingPathComponent(basename) }

    static func bounds(_ window: ActivityWindow, now: Date, calendar: Calendar)
        -> (start: Date, previousStart: Date, previousEnd: Date, buckets: [Date]) {
        let today = calendar.startOfDay(for: now)
        let start = calendar.date(byAdding: .day, value: -(window.days - 1), to: today)!
        let previousStart = calendar.date(byAdding: .day, value: -window.days, to: start)!
        let previousEnd = now.addingTimeInterval(-Double(window.days) * 86_400)
        let buckets: [Date]
        if window == .today {
            let hour = calendar.component(.hour, from: now)
            buckets = (0...hour).map { start.addingTimeInterval(Double($0) * 3600) }
        } else {
            buckets = (0..<window.days).map { calendar.date(byAdding: .day, value: $0, to: start)! }
        }
        return (start, previousStart, previousEnd, buckets)
    }

    /// Nil when the ledger does not exist yet (nothing has been recorded on this machine).
    static func overview(dbPath: String, window: ActivityWindow, now: Date = Date(),
                         names: [String: String] = [:], calendar: Calendar = .current) -> ActivityOverview? {
        let file = path(dbPath: dbPath)
        guard FileManager.default.fileExists(atPath: file), let db = Database(path: file) else { return nil }
        let result = build(db: db, window: window, now: now, names: names, calendar: calendar)
        // A failed query must read as "no history", never as a confident zero.
        return db.failed ? nil : result
    }

    private static func build(db: Database, window: ActivityWindow, now: Date,
                              names: [String: String], calendar: Calendar) -> ActivityOverview {
        let b = bounds(window, now: now, calendar: calendar)
        let meta = Dictionary(db.rows("SELECT key, value FROM meta").compactMap { r -> (String, Double)? in
            guard let k = r.string(0), let v = r.string(1).flatMap(Double.init) else { return nil }
            return (k, v)
        }, uniquingKeysWith: { a, _ in a })
        var o = ActivityOverview(window: window, periodStart: b.start, periodEnd: now,
                                 previousStart: b.previousStart, previousEnd: b.previousEnd,
                                 filesSince: meta["files_since"].map(Date.init(timeIntervalSince1970:)),
                                 searchesSince: meta["searches_since"].map(Date.init(timeIntervalSince1970:)))
        func label(_ slug: String?) -> String { slug.map { names[$0] ?? $0 } ?? "All collections" }
        let s0 = b.start.timeIntervalSince1970, s1 = now.timeIntervalSince1970
        let p0 = b.previousStart.timeIntervalSince1970, p1 = b.previousEnd.timeIntervalSince1970
        let notReindex = "origin NOT IN (" + reindexOrigins.map { "'\($0)'" }.joined(separator: ",") + ")"
        o.timeline = b.buckets.map { ActivityBucket(start: $0) }
        func bucket(_ t: Double) -> Int? {
            guard let first = b.buckets.first, t >= first.timeIntervalSince1970 else { return nil }
            return b.buckets.lastIndex { $0.timeIntervalSince1970 <= t }
        }

        // Library changes
        var bySilo: [String: ActivityChangeSilo] = [:]
        for r in db.rows("SELECT ts, silo, path, kind FROM file_events WHERE ts >= ? AND ts <= ? AND \(notReindex) ORDER BY ts DESC", s0, s1) {
            let ts = r.double(0), slug = r.string(1) ?? "?", kind = r.string(3) ?? ""
            var row = bySilo[slug] ?? ActivityChangeSilo(slug: slug)
            switch kind {
            case "added": row.added += 1; o.changeTotals.added += 1
            case "updated": row.updated += 1; o.changeTotals.updated += 1
            case "removed": row.removed += 1; o.changeTotals.removed += 1
            default: break
            }
            if row.files.count < detailLimit {
                row.files.append(ActivityFile(path: r.string(2) ?? "", kind: kind, at: Date(timeIntervalSince1970: ts)))
            }
            bySilo[slug] = row
            if let i = bucket(ts) {
                switch kind {
                case "added": o.timeline[i].added += 1
                case "updated": o.timeline[i].updated += 1
                case "removed": o.timeline[i].removed += 1
                default: break
                }
            }
        }
        for r in db.rows("SELECT silo, COUNT(*) FROM file_events WHERE ts >= ? AND ts <= ? AND \(notReindex) GROUP BY silo", p0, p1) {
            let slug = r.string(0) ?? "?", n = r.int(1)
            o.previousChangeTotal += n
            bySilo[slug]?.previousTotal = n
        }
        o.changeSilos = bySilo.values.sorted { ($1.total, label($0.slug)) < ($0.total, label($1.slug)) }
        o.collections = db.rows("SELECT ts, silo, kind, files FROM silo_events WHERE ts >= ? AND ts <= ? AND \(notReindex) ORDER BY ts DESC", s0, s1).map {
            ActivityCollectionEvent(slug: $0.string(1) ?? "?", added: $0.string(2) == "silo_added",
                                    files: $0.int(3), at: Date(timeIntervalSince1970: $0.double(0)))
        }

        // Library use
        o.searches = db.rows("SELECT id, ts, silo, query_text, sources_found, client FROM searches WHERE ts >= ? AND ts <= ? ORDER BY ts DESC", s0, s1).map {
            ActivitySearch(id: $0.int(0), at: Date(timeIntervalSince1970: $0.double(1)), silo: $0.string(2),
                           query: $0.string(3), sourcesFound: $0.int(4) != 0, client: $0.string(5) ?? "unknown")
        }
        o.previousSearches = db.rows("SELECT COUNT(*) FROM searches WHERE ts >= ? AND ts <= ?", p0, p1).first?.int(0) ?? 0
        var hits: [Int: [(String, String)]] = [:]
        for r in db.rows("SELECT h.search_id, h.silo, h.path FROM search_hits h JOIN searches s ON s.id = h.search_id WHERE s.ts >= ? AND s.ts <= ?", s0, s1) {
            hits[r.int(0), default: []].append((r.string(1) ?? "?", r.string(2) ?? ""))
        }
        var use: [String: (searches: Int, files: Set<String>)] = [:]
        var docs: [String: [String: Int]] = [:]
        var clients: [String: Int] = [:]
        var noSource: [String: ActivityQueryGroup] = [:]
        for s in o.searches {
            clients[s.client, default: 0] += 1
            let i = bucket(s.at.timeIntervalSince1970)
            if let i { o.timeline[i].searches += 1 }
            if !s.sourcesFound {
                if let i { o.timeline[i].withoutSources += 1 }
                let q = normalize(s.query)
                let text = q.isEmpty ? "(query text not kept)" : q
                let key = (s.silo ?? "") + "\u{0}" + text
                var g = noSource[key] ?? ActivityQueryGroup(query: text, silo: s.silo, lastSeen: s.at)
                g.count += 1
                g.searchIDs.append(s.id)
                noSource[key] = g
            }
            var touched: [String: Set<String>] = [:]
            for (slug, path) in hits[s.id] ?? [] {
                touched[slug, default: []].insert(path)
                docs[slug, default: [:]][path, default: 0] += 1
            }
            if let slug = s.silo, touched[slug] == nil { touched[slug] = [] }
            for (slug, paths) in touched {
                use[slug, default: (0, [])].searches += 1
                use[slug]!.files.formUnion(paths)
            }
        }
        o.useSilos = use.map { ActivityUseSilo(slug: $0.key, searches: $0.value.searches, uniqueFiles: $0.value.files.count) }
            .sorted { ($1.searches, label($0.slug)) < ($0.searches, label($1.slug)) }
        o.topDocuments = Array(docs.flatMap { slug, paths in paths.map { ActivityDocument(slug: slug, path: $0.key, searches: $0.value) } }
            .sorted { ($1.searches, $0.path) < ($0.searches, $1.path) }
            .prefix(detailLimit))
        o.clients = clients.map { (name: $0.key, searches: $0.value) }.sorted { ($1.searches, $0.name) < ($0.searches, $1.name) }
        o.noSourceGroups = Array(noSource.values.sorted { ($1.count, $0.query) < ($0.count, $1.query) }.prefix(detailLimit))

        // Attention
        var groups: [String: ActivityFailureGroup] = [:]
        for r in db.rows("SELECT silo, path, error_class, error, last_seen, occurrences FROM failures WHERE resolved_at IS NULL ORDER BY last_seen DESC") {
            let slug = r.string(0) ?? "?", cls = r.string(2) ?? "other"
            o.openFailures[slug, default: 0] += 1
            let key = slug + "\u{0}" + cls
            var g = groups[key] ?? ActivityFailureGroup(slug: slug, errorClass: cls, sampleError: r.string(3) ?? "",
                                                        lastSeen: Date(timeIntervalSince1970: r.double(4)))
            g.files += 1
            g.occurrences += r.int(5)
            if g.paths.count < 5, let p = r.string(1) { g.paths.append(p) }
            groups[key] = g
        }
        o.failureGroups = groups.values.sorted { ($1.files, label($0.slug)) < ($0.files, label($1.slug)) }

        // Collection health
        for r in db.rows("SELECT silo, MAX(ts) FROM file_events WHERE \(notReindex) GROUP BY silo") {
            if let slug = r.string(0) { o.lastChange[slug] = Date(timeIntervalSince1970: r.double(1)) }
        }
        for r in db.rows("SELECT silo, MAX(ts) FROM searches WHERE silo IS NOT NULL GROUP BY silo")
            + db.rows("SELECT h.silo, MAX(s.ts) FROM search_hits h JOIN searches s ON s.id = h.search_id GROUP BY h.silo") {
            guard let slug = r.string(0) else { continue }
            let d = Date(timeIntervalSince1970: r.double(1))
            o.lastSearch[slug] = max(o.lastSearch[slug] ?? d, d)
        }
        return o
    }

    static func normalize(_ text: String?) -> String {
        (text ?? "").lowercased().split(whereSeparator: \.isWhitespace).joined(separator: " ")
    }
}

/// Minimal SQLite wrapper that only ever runs SELECTs.
///
/// Opened read-write (never create) on purpose: the ledger is in WAL mode, and a
/// SQLITE_OPEN_READONLY connection cannot create the `-shm` file when no writer
/// has it open — every query then fails with "unable to open database file".
private final class Database {
    private var handle: OpaquePointer?
    private(set) var failed = false

    init?(path: String) {
        guard sqlite3_open_v2(path, &handle, SQLITE_OPEN_READWRITE, nil) == SQLITE_OK else {
            sqlite3_close(handle)
            return nil
        }
        sqlite3_busy_timeout(handle, 2000)
    }

    deinit { sqlite3_close(handle) }

    struct Row {
        fileprivate let values: [Any?]
        func string(_ i: Int) -> String? { values[i] as? String }
        func double(_ i: Int) -> Double { (values[i] as? Double) ?? Double((values[i] as? Int) ?? 0) }
        func int(_ i: Int) -> Int { (values[i] as? Int) ?? Int((values[i] as? Double) ?? 0) }
    }

    func rows(_ sql: String, _ args: Double...) -> [Row] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &stmt, nil) == SQLITE_OK else { failed = true; return [] }
        defer { sqlite3_finalize(stmt) }
        for (i, a) in args.enumerated() { sqlite3_bind_double(stmt, Int32(i + 1), a) }
        var out: [Row] = []
        var rc = sqlite3_step(stmt)
        while rc == SQLITE_ROW {
            let n = sqlite3_column_count(stmt)
            out.append(Row(values: (0..<n).map { c -> Any? in
                switch sqlite3_column_type(stmt, c) {
                case SQLITE_INTEGER: return Int(sqlite3_column_int64(stmt, c))
                case SQLITE_FLOAT: return sqlite3_column_double(stmt, c)
                case SQLITE_TEXT: return String(cString: sqlite3_column_text(stmt, c))
                default: return nil
                }
            }))
            rc = sqlite3_step(stmt)
        }
        if rc != SQLITE_DONE { failed = true }
        return out
    }
}

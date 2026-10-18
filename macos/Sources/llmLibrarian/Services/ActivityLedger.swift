import Foundation
import SQLite3

/// Reads `<db>/llmli_activity.sqlite3`, the ledger `activity_ledger.py` writes.
///
/// What an event *means* (re-index origins are not changes, local-time
/// day/hour buckets, which silos a search touched, failure grouping) lives in
/// the ledger's `activity_*_v1` views, defined once in Python. This reader runs
/// the same SQL strings as `activity_ledger.SUMMARY_SQL` / `detail()`; the shared
/// fixture in tests/fixtures/activity checks the summary end to end.
/// Foundation + SQLite only so `test.sh` can compile it alone.
enum ActivityLedger {
    static let basename = "llmli_activity.sqlite3"

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

    /// The views' bucket key: local "yyyy-MM-dd", plus the hour for Today.
    static func bucketKey(_ date: Date, window: ActivityWindow, calendar: Calendar) -> String {
        let c = calendar.dateComponents([.year, .month, .day, .hour], from: date)
        let day = String(format: "%04d-%02d-%02d", c.year ?? 0, c.month ?? 0, c.day ?? 0)
        return window == .today ? "\(day) \(c.hour ?? 0)" : day
    }

    // Keep identical to activity_ledger.SUMMARY_SQL.
    enum SQL {
        static let changeBuckets = "SELECT day, hour, kind, COUNT(*) FROM activity_changes_v1 WHERE ts >= ? AND ts <= ? GROUP BY day, hour, kind"
        static let changeSilos = "SELECT silo, COUNT(*) FROM activity_changes_v1 WHERE ts >= ? AND ts <= ? GROUP BY silo"
        static let changeCount = "SELECT COUNT(*) FROM activity_changes_v1 WHERE ts >= ? AND ts <= ?"
        static let searchBuckets = "SELECT day, hour, COUNT(*), SUM(sources_found = 0) FROM activity_searches_v1 WHERE ts >= ? AND ts <= ? GROUP BY day, hour"
        static let searchCount = "SELECT COUNT(*), IFNULL(SUM(sources_found = 0), 0) FROM activity_searches_v1 WHERE ts >= ? AND ts <= ?"
        static let searchSilos = "SELECT silo, COUNT(DISTINCT search_id) FROM activity_search_silos_v1 WHERE ts >= ? AND ts <= ? GROUP BY silo"
        static let collectionEvents = "SELECT ts, silo, kind, files FROM activity_collection_events_v1 WHERE ts >= ? AND ts <= ? ORDER BY ts DESC"
        static let lastChange = "SELECT silo, MAX(ts) FROM activity_changes_v1 GROUP BY silo"
        static let lastSearch = "SELECT silo, MAX(ts) FROM activity_search_silos_v1 GROUP BY silo"
        static let failureGroups = "SELECT silo, error_class, files, occurrences, last_seen, sample_error FROM activity_failure_groups_v1"
    }

    /// Nil when there is no ledger yet, or it can't be read (a failed query must
    /// read as "no history", never as a confident zero).
    static func summary(dbPath: String, window: ActivityWindow, now: Date = Date(),
                        calendar: Calendar = .current) -> ActivitySummary? {
        guard let db = open(dbPath) else { return nil }
        let b = bounds(window, now: now, calendar: calendar)
        let cur = [b.start.timeIntervalSince1970, now.timeIntervalSince1970]
        let prev = [b.previousStart.timeIntervalSince1970, b.previousEnd.timeIntervalSince1970]
        let meta = db.meta()
        var s = ActivitySummary(window: window, periodStart: b.start, periodEnd: now,
                                previousStart: b.previousStart, previousEnd: b.previousEnd,
                                filesSince: meta["files_since"].map(Date.init(timeIntervalSince1970:)),
                                searchesSince: meta["searches_since"].map(Date.init(timeIntervalSince1970:)))
        s.buckets = b.buckets.map { ActivityBucket(start: $0) }
        let index = Dictionary(uniqueKeysWithValues: b.buckets.enumerated().map { (bucketKey($1, window: window, calendar: calendar), $0) })
        func slot(_ r: Database.Row) -> Int? {
            index[window == .today ? "\(r.string(0) ?? "") \(r.int(1))" : (r.string(0) ?? "")]
        }

        for r in db.rows(SQL.changeBuckets, cur) {
            let n = r.int(3), i = slot(r)
            switch r.string(2) {
            case "added": s.added += n; if let i { s.buckets[i].added += n }
            case "updated": s.updated += n; if let i { s.buckets[i].updated += n }
            case "removed": s.removed += n; if let i { s.buckets[i].removed += n }
            default: break
            }
        }
        for r in db.rows(SQL.searchBuckets, cur) {
            s.searches += r.int(2)
            s.noSource += r.int(3)
            if let i = slot(r) { s.buckets[i].searches += r.int(2); s.buckets[i].noSource += r.int(3) }
        }
        s.previousChangeCount = db.rows(SQL.changeCount, prev).first?.int(0) ?? 0
        if let r = db.rows(SQL.searchCount, prev).first { s.previousSearchCount = r.int(0); s.previousNoSourceCount = r.int(1) }
        for r in db.rows(SQL.changeSilos, cur) { if let k = r.string(0) { s.perSilo[k, default: .init()].changes = r.int(1) } }
        for r in db.rows(SQL.searchSilos, cur) { if let k = r.string(0) { s.perSilo[k, default: .init()].searches = r.int(1) } }
        for r in db.rows(SQL.lastChange, []) { if let k = r.string(0) { s.perSilo[k, default: .init()].lastChange = r.date(1) } }
        for r in db.rows(SQL.lastSearch, []) { if let k = r.string(0) { s.perSilo[k, default: .init()].lastSearch = r.date(1) } }
        s.collectionEvents = db.rows(SQL.collectionEvents, cur).map {
            ActivityCollectionEvent(slug: $0.string(1) ?? "?", added: $0.string(2) == "silo_added", files: $0.int(3), at: $0.date(0))
        }
        s.failures = db.rows(SQL.failureGroups, []).map {
            ActivityFailureGroup(slug: $0.string(0) ?? "?", errorClass: $0.string(1) ?? "other", files: $0.int(2),
                                 occurrences: $0.int(3), lastSeen: $0.date(4), sampleError: $0.string(5) ?? "")
        }
        for g in s.failures { s.perSilo[g.slug, default: .init()].openFailures += g.files }
        return db.failed ? nil : s
    }

    // MARK: detail — the rows behind one number, read only while the Activity page shows them.

    static func changes(dbPath: String, window: ActivityWindow, silo: String? = nil, limit: Int = 200,
                        now: Date = Date(), calendar: Calendar = .current) -> [ActivityChangeRow]? {
        guard let db = open(dbPath) else { return nil }
        let b = bounds(window, now: now, calendar: calendar)
        var sql = "SELECT id, ts, silo, path, kind FROM activity_changes_v1 WHERE ts >= ? AND ts <= ?"
        var args: [Any] = [b.start.timeIntervalSince1970, now.timeIntervalSince1970]
        if let silo { sql += " AND silo = ?"; args.append(silo) }
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        let rows = db.rows(sql, args).map {
            ActivityChangeRow(id: $0.int(0), at: $0.date(1), slug: $0.string(2) ?? "?", path: $0.string(3) ?? "", kind: $0.string(4) ?? "")
        }
        return db.failed ? nil : rows
    }

    static func searches(dbPath: String, window: ActivityWindow, silo: String? = nil, noSourceOnly: Bool = false,
                         limit: Int = 200, now: Date = Date(), calendar: Calendar = .current) -> [ActivitySearchRow]? {
        guard let db = open(dbPath) else { return nil }
        let b = bounds(window, now: now, calendar: calendar)
        var sql = "SELECT id, ts, silo, query_text, sources_found, unique_files, client FROM activity_searches_v1 WHERE ts >= ? AND ts <= ?"
        var args: [Any] = [b.start.timeIntervalSince1970, now.timeIntervalSince1970]
        if let silo { sql += " AND id IN (SELECT search_id FROM activity_search_silos_v1 WHERE silo = ?)"; args.append(silo) }
        if noSourceOnly { sql += " AND sources_found = 0" }
        sql += " ORDER BY ts DESC, id DESC LIMIT ?"
        args.append(limit)
        var rows = db.rows(sql, args).map {
            ActivitySearchRow(id: $0.int(0), at: $0.date(1), slug: $0.string(2), query: $0.string(3),
                              sourcesFound: $0.int(4) != 0, uniqueFiles: $0.int(5), client: $0.string(6) ?? "unknown")
        }
        if !rows.isEmpty {
            let ids = rows.map(\.id)
            var hits: [Int: [ActivityHit]] = [:]
            let marks = Array(repeating: "?", count: ids.count).joined(separator: ",")
            for r in db.rows("SELECT search_id, silo, path FROM search_hits WHERE search_id IN (\(marks)) ORDER BY rowid", ids) {
                hits[r.int(0), default: []].append(ActivityHit(slug: r.string(1) ?? "?", path: r.string(2) ?? ""))
            }
            for i in rows.indices { rows[i].hits = hits[rows[i].id] ?? [] }
        }
        return db.failed ? nil : rows
    }

    static func documents(dbPath: String, window: ActivityWindow, silo: String? = nil, limit: Int = 50,
                          now: Date = Date(), calendar: Calendar = .current) -> [ActivityDocumentRow]? {
        guard let db = open(dbPath) else { return nil }
        let b = bounds(window, now: now, calendar: calendar)
        var sql = "SELECT h.silo, h.path, COUNT(DISTINCT h.search_id) AS n FROM search_hits h JOIN searches s ON s.id = h.search_id WHERE s.ts >= ? AND s.ts <= ?"
        var args: [Any] = [b.start.timeIntervalSince1970, now.timeIntervalSince1970]
        if let silo { sql += " AND h.silo = ?"; args.append(silo) }
        sql += " GROUP BY h.silo, h.path ORDER BY n DESC, h.path LIMIT ?"
        args.append(limit)
        let rows = db.rows(sql, args).map { ActivityDocumentRow(slug: $0.string(0) ?? "?", path: $0.string(1) ?? "", searches: $0.int(2)) }
        return db.failed ? nil : rows
    }

    static func clients(dbPath: String, window: ActivityWindow, now: Date = Date(),
                        calendar: Calendar = .current) -> [(name: String, searches: Int)]? {
        guard let db = open(dbPath) else { return nil }
        let b = bounds(window, now: now, calendar: calendar)
        let rows = db.rows("SELECT client, COUNT(*) FROM activity_searches_v1 WHERE ts >= ? AND ts <= ? GROUP BY client ORDER BY COUNT(*) DESC, client",
                           [b.start.timeIntervalSince1970, now.timeIntervalSince1970])
            .map { (name: $0.string(0) ?? "unknown", searches: $0.int(1)) }
        return db.failed ? nil : rows
    }

    static func failures(dbPath: String, silo: String? = nil, limit: Int = 200) -> [ActivityFailureRow]? {
        guard let db = open(dbPath) else { return nil }
        var sql = "SELECT id, silo, path, error_class, error, last_seen, occurrences FROM failures WHERE resolved_at IS NULL"
        var args: [Any] = []
        if let silo { sql += " AND silo = ?"; args.append(silo) }
        sql += " ORDER BY last_seen DESC, id DESC LIMIT ?"
        args.append(limit)
        let rows = db.rows(sql, args).map {
            ActivityFailureRow(id: $0.int(0), slug: $0.string(1) ?? "?", path: $0.string(2) ?? "", errorClass: $0.string(3) ?? "other",
                               error: $0.string(4) ?? "", lastSeen: $0.date(5), occurrences: $0.int(6))
        }
        return db.failed ? nil : rows
    }

    private static func open(_ dbPath: String) -> Database? {
        let file = path(dbPath: dbPath)
        guard FileManager.default.fileExists(atPath: file) else { return nil }
        return Database(path: file)
    }
}

/// Minimal SQLite wrapper that only ever runs SELECTs.
///
/// Opened read-write (never create) on purpose: the ledger is in WAL mode, and a
/// SQLITE_OPEN_READONLY connection cannot create the `-shm` file when no writer
/// has it open — every query then fails with "unable to open database file".
final class Database {
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
        func date(_ i: Int) -> Date { Date(timeIntervalSince1970: double(i)) }
    }

    func meta() -> [String: Double] {
        Dictionary(rows("SELECT key, value FROM meta", []).compactMap { r -> (String, Double)? in
            guard let k = r.string(0), let v = r.string(1).flatMap(Double.init) else { return nil }
            return (k, v)
        }, uniquingKeysWith: { a, _ in a })
    }

    func rows(_ sql: String, _ args: [Any]) -> [Row] {
        var stmt: OpaquePointer?
        guard sqlite3_prepare_v2(handle, sql, -1, &stmt, nil) == SQLITE_OK else { failed = true; return [] }
        defer { sqlite3_finalize(stmt) }
        let transient = unsafeBitCast(-1, to: sqlite3_destructor_type.self)
        for (i, a) in args.enumerated() {
            let pos = Int32(i + 1)
            switch a {
            case let d as Double: sqlite3_bind_double(stmt, pos, d)
            case let n as Int: sqlite3_bind_int64(stmt, pos, Int64(n))
            case let s as String: sqlite3_bind_text(stmt, pos, s, -1, transient)
            default: sqlite3_bind_null(stmt, pos)
            }
        }
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

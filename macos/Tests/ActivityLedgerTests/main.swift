import Foundation
import SQLite3

// Reads the shared fixture (tests/fixtures/activity) with the Swift reader and
// checks it against the summaries Python produced from the same ledger.
// Usage: activity-ledger-tests <fixture dir>

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}

// The views bucket by SQLite 'localtime', which follows the process time zone.
setenv("TZ", "UTC", 1)
tzset()
var utc = Calendar(identifier: .gregorian)
utc.timeZone = TimeZone(identifier: "UTC")!

let fixture = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "../tests/fixtures/activity"
let dbPath = fixture + "/db"
func json(_ path: String) -> [String: Any] {
    guard let data = FileManager.default.contents(atPath: path),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { fatalError("cannot read \(path)") }
    return obj
}
func dict(_ any: Any?) -> [String: Any] { any as? [String: Any] ?? [:] }
func list(_ any: Any?) -> [[String: Any]] { any as? [[String: Any]] ?? [] }
func date(_ any: Any?) -> Date? { Dates.parse(any as? String) }

let registry = json(dbPath + "/llmli_registry.json")
let collections: [(slug: String, name: String)] = registry.compactMap { slug, v in
    ((v as? [String: Any])?["display_name"] as? String).map { (slug, $0) }
}

expect(ActivityLedger.summary(dbPath: "/nonexistent", window: .week) == nil, "Missing ledger reads as nil, not an error")

var weekSummary: ActivitySummary?
for (window, file) in [(ActivityWindow.today, "summary-today.json"), (.week, "summary-7d.json")] {
    let want = json(fixture + "/" + file)
    let now = date(dict(want["period"])["end"])!
    guard let got = ActivityLedger.summary(dbPath: dbPath, window: window, now: now, calendar: utc) else {
        fatalError("fixture ledger did not open")
    }
    if window == .week { weekSummary = got }
    let tag = window.rawValue

    let prev = dict(want["previous_period"]), hist = dict(want["history"])
    expect(got.periodStart == date(dict(want["period"])["start"]), "\(tag): period start")
    expect(got.previousStart == date(prev["start"]) && got.previousEnd == date(prev["end"]), "\(tag): previous period")
    expect(got.fullFileComparison == hist["full_file_comparison"] as? Bool
           && got.fullSearchComparison == hist["full_search_comparison"] as? Bool, "\(tag): history coverage")

    let t = dict(want["totals"])
    expect(got.added == t["added"] as? Int && got.updated == t["updated"] as? Int && got.removed == t["removed"] as? Int,
           "\(tag): change totals \(got.added)/\(got.updated)/\(got.removed)")
    expect(got.previousChanges == t["previous_changes"] as? Int, "\(tag): previous changes")
    expect(got.searches == t["searches"] as? Int && got.previousSearches == t["previous_searches"] as? Int, "\(tag): searches")
    expect(got.noSource == t["no_source"] as? Int && got.previousNoSource == t["previous_no_source"] as? Int, "\(tag): no source")

    let buckets = list(want["buckets"])
    expect(got.buckets.count == buckets.count, "\(tag): bucket count \(got.buckets.count)")
    for (g, w) in zip(got.buckets, buckets) {
        expect(g.start == date(w["start"]) && g.added == w["added"] as? Int && g.updated == w["updated"] as? Int
               && g.removed == w["removed"] as? Int && g.searches == w["searches"] as? Int && g.noSource == w["no_source"] as? Int,
               "\(tag): bucket \(g.start)")
    }

    let rows = list(want["collections"])
    expect(got.ranked(collections).map(\.slug) == rows.map { $0["silo"] as? String ?? "" }, "\(tag): collection order")
    for r in rows {
        let s = got.stats(r["silo"] as! String)
        expect(s.changes == r["changes"] as? Int && s.searches == r["searches"] as? Int, "\(tag): counts for \(r["silo"]!)")
        expect(s.lastChange == date(r["last_change"]) && s.lastSearch == date(r["last_search"])
               && s.lastActivity == date(r["last_activity"]), "\(tag): last activity for \(r["silo"]!)")
        expect(s.openFailures == r["open_failures"] as? Int, "\(tag): open failures for \(r["silo"]!)")
    }
    let failures = got.failures.sorted { ($1.files, $0.slug, $0.errorClass) < ($0.files, $1.slug, $1.errorClass) }
    expect(failures.map { "\($0.slug):\($0.errorClass):\($0.files):\($0.occurrences)" }
           == list(want["failures"]).map { "\($0["silo"]!):\($0["error_class"]!):\($0["files"]!):\($0["occurrences"]!)" },
           "\(tag): failure groups")
    expect(got.collectionEvents.map { "\($0.slug):\($0.added):\($0.files)" }
           == list(want["collection_events"]).map { "\($0["silo"]!):\(($0["kind"] as? String) == "silo_added"):\($0["files"]!)" },
           "\(tag): collection events")
    expect(got.headline(collections) == want["headline"] as? String, "\(tag): headline \(got.headline(collections))")
}

// Detail reads agree with the summary they sit behind.
let week = weekSummary!
let weekNow = week.periodEnd
let changeRows = ActivityLedger.changes(dbPath: dbPath, window: .week, now: weekNow, calendar: utc)!
expect(changeRows.count == week.changes, "change rows = change total")
expect(ActivityLedger.changes(dbPath: dbPath, window: .week, silo: "research-1", now: weekNow, calendar: utc)!.count
       == week.stats("research-1").changes, "per-collection change rows")
let searchRows = ActivityLedger.searches(dbPath: dbPath, window: .week, now: weekNow, calendar: utc)!
expect(searchRows.count == week.searches, "search rows = search total")
expect(ActivityLedger.searches(dbPath: dbPath, window: .week, noSourceOnly: true, now: weekNow, calendar: utc)!.count
       == week.noSource, "no-source rows = no-source total")
expect(ActivityLedger.searches(dbPath: dbPath, window: .week, silo: "notes-2", now: weekNow, calendar: utc)!.count
       == week.stats("notes-2").searches, "per-collection search rows")
expect(searchRows.first { $0.query == "hybrid retrieval" }?.hits.isEmpty == false, "search rows carry their hits")
expect(ActivityLedger.documents(dbPath: dbPath, window: .week, now: weekNow, calendar: utc)!.first?.name == "a.md", "top document")
expect(ActivityLedger.clients(dbPath: dbPath, window: .week, now: weekNow, calendar: utc)!.reduce(0) { $0 + $1.searches }
       == week.searches, "clients add up")
expect(ActivityLedger.failures(dbPath: dbPath)!.count == week.failures.reduce(0) { $0 + $1.files }, "failure rows")

// Window math
let today = ActivityLedger.bounds(.today, now: weekNow, calendar: utc)
expect(today.buckets.count == 16 && today.previousEnd == weekNow.addingTimeInterval(-86_400), "today is hourly, vs yesterday-to-date")
expect(ActivityLedger.bounds(.month, now: weekNow, calendar: utc).buckets.count == 30, "30 daily buckets")

// Regression: the live ledger is in WAL mode. With no writer holding it open there
// is no -shm file, and a read-only connection could not create one, so every query
// failed and the Overview showed zeros. Read a WAL copy with no sidecar files.
let walDir = NSTemporaryDirectory() + "activity-wal-\(getpid())"
try? FileManager.default.removeItem(atPath: walDir)
try! FileManager.default.createDirectory(atPath: walDir, withIntermediateDirectories: true)
let walFile = walDir + "/" + ActivityLedger.basename
try! FileManager.default.copyItem(atPath: ActivityLedger.path(dbPath: dbPath), toPath: walFile)
var conn: OpaquePointer?
sqlite3_open(walFile, &conn)
sqlite3_exec(conn, "PRAGMA journal_mode=WAL", nil, nil, nil)
sqlite3_close(conn)
expect(!FileManager.default.fileExists(atPath: walFile + "-shm"), "WAL copy starts without a -shm file")
let fromWAL = ActivityLedger.summary(dbPath: walDir, window: .week, now: weekNow, calendar: utc)
expect(fromWAL?.searches == week.searches && fromWAL?.changes == week.changes,
       "WAL ledger with no sidecar files reads the same numbers (got \(String(describing: fromWAL?.searches)))")
try? FileManager.default.removeItem(atPath: walDir)

print("ActivityLedgerTests: passed")

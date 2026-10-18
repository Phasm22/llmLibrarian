import Foundation
import SQLite3

// Reads the shared fixture (tests/fixtures/activity) with the Swift reader and
// checks it against the overview Python produced from the same ledger.
// Usage: activity-ledger-tests <fixture dir>

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}

let fixture = CommandLine.arguments.count > 1 ? CommandLine.arguments[1] : "../tests/fixtures/activity"
let dbPath = fixture + "/db"
func json(_ path: String) -> [String: Any] {
    guard let data = FileManager.default.contents(atPath: path),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
        fatalError("cannot read \(path)")
    }
    return obj
}
let want = json(fixture + "/overview-7d.json")
let registry = json(dbPath + "/llmli_registry.json")
let names = registry.compactMapValues { ($0 as? [String: Any])?["display_name"] as? String }

var utc = Calendar(identifier: .gregorian)
utc.timeZone = TimeZone(identifier: "UTC")!
let now = Dates.parse((want["period"] as! [String: Any])["end"] as? String)!

guard let got = ActivityLedger.overview(dbPath: dbPath, window: .week, now: now, names: names, calendar: utc) else {
    fatalError("fixture ledger did not open")
}
expect(ActivityLedger.overview(dbPath: "/nonexistent", window: .week) == nil, "Missing ledger reads as nil, not an error")

func dict(_ any: Any?) -> [String: Any] { any as? [String: Any] ?? [:] }
func list(_ any: Any?) -> [[String: Any]] { any as? [[String: Any]] ?? [] }
func date(_ any: Any?) -> Date? { Dates.parse(any as? String) }

// Periods + history coverage
let period = dict(want["period"]), previous = dict(want["previous_period"]), history = dict(want["history"])
expect(got.periodStart == date(period["start"]), "period start \(got.periodStart)")
expect(got.previousStart == date(previous["start"]) && got.previousEnd == date(previous["end"]), "previous period")
expect(got.fullFileComparison == (history["full_file_comparison"] as? Bool), "file history coverage")
expect(got.fullSearchComparison == (history["full_search_comparison"] as? Bool), "search history coverage")

// Library changes
let changes = dict(want["changes"])
let totals = dict(changes["totals"])
expect(got.changeTotals.added == totals["added"] as? Int && got.changeTotals.updated == totals["updated"] as? Int
       && got.changeTotals.removed == totals["removed"] as? Int, "change totals \(got.changeTotals)")
let prevTotals = dict(changes["previous_totals"]).values.compactMap { $0 as? Int }.reduce(0, +)
expect(got.previousChangeTotal == prevTotals, "previous change total \(got.previousChangeTotal)")
let wantSilos = list(changes["by_silo"])
expect(got.changeSilos.map(\.slug) == wantSilos.map { $0["silo"] as? String ?? "" }, "change silo order \(got.changeSilos.map(\.slug))")
for (g, w) in zip(got.changeSilos, wantSilos) {
    expect(g.added == w["added"] as? Int && g.updated == w["updated"] as? Int && g.removed == w["removed"] as? Int
           && g.previousTotal == w["previous_total"] as? Int, "change counts for \(g.slug)")
    expect(g.files.map(\.path) == list(w["files"]).map { $0["path"] as? String ?? "" }, "change files for \(g.slug)")
}
expect(got.collections.map { "\($0.slug):\($0.added):\($0.files)" }
       == list(changes["collections"]).map { "\($0["silo"]!):\(($0["kind"] as? String) == "silo_added"):\($0["files"]!)" },
       "collection events")
let changeTimeline = list(changes["timeline"])
expect(got.timeline.count == changeTimeline.count, "bucket count")
for (g, w) in zip(got.timeline, changeTimeline) {
    expect(g.start == date(w["start"]) && g.added == w["added"] as? Int && g.updated == w["updated"] as? Int
           && g.removed == w["removed"] as? Int, "change bucket \(g.start)")
}

// Library use
let use = dict(want["use"])
expect(got.searchCount == use["searches"] as? Int && got.previousSearches == use["previous_searches"] as? Int,
       "search counts \(got.searchCount)/\(got.previousSearches)")
expect(got.withSources == use["with_sources"] as? Int && got.withoutSources == use["without_sources"] as? Int, "sources found split")
expect(got.useSilos.map { "\($0.slug):\($0.searches):\($0.uniqueFiles)" }
       == list(use["by_silo"]).map { "\($0["silo"]!):\($0["searches"]!):\($0["unique_files"]!)" },
       "use by silo \(got.useSilos)")
expect(got.topDocuments.map { "\($0.path):\($0.searches)" }
       == list(use["top_documents"]).map { "\($0["path"]!):\($0["searches"]!)" }, "top documents")
expect(got.clients.map { "\($0.name):\($0.searches)" }
       == list(use["by_client"]).map { "\($0["client"]!):\($0["searches"]!)" }, "clients \(got.clients)")
for (g, w) in zip(got.timeline, list(use["timeline"])) {
    expect(g.searches == w["searches"] as? Int && g.withoutSources == w["without_sources"] as? Int, "search bucket \(g.start)")
}

// Attention
let attention = dict(want["attention"])
expect(got.failureGroups.map { "\($0.slug):\($0.errorClass):\($0.files):\($0.occurrences):\($0.paths)" }
       == list(attention["failures"]).map { "\($0["silo"]!):\($0["error_class"]!):\($0["files"]!):\($0["occurrences"]!):\(($0["paths"] as? [String]) ?? [])" },
       "failure groups \(got.failureGroups)")
expect(got.noSourceGroups.map { "\($0.silo ?? "-"):\($0.query):\($0.count)" }
       == list(attention["no_source_searches"]).map { "\(($0["silo"] as? String) ?? "-"):\($0["query"]!):\($0["count"]!)" },
       "no-source groups \(got.noSourceGroups)")

// Collection health
for h in list(want["health"]) {
    let slug = h["silo"] as! String
    expect(got.lastChange[slug] == date(h["last_change"]), "last change for \(slug)")
    expect(got.lastSearch[slug] == date(h["last_search"]), "last search for \(slug)")
    expect((got.openFailures[slug] ?? 0) == h["open_failures"] as? Int, "open failures for \(slug)")
}

// Summary wording
let summary = dict(want["summary"])
let mine = got.summary { slug in slug.map { names[$0] ?? $0 } ?? "All collections" }
expect(mine.headline == summary["headline"] as? String, "headline: \(mine.headline)")
expect(mine.sentences == summary["sentences"] as? [String], "sentences: \(mine.sentences)")

// Window math
let today = ActivityLedger.bounds(.today, now: now, calendar: utc)
expect(today.buckets.count == 16 && today.previousEnd == now.addingTimeInterval(-86_400), "today is hourly, vs yesterday-to-date")
expect(ActivityLedger.bounds(.month, now: now, calendar: utc).buckets.count == 30, "30 daily buckets")

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
let fromWAL = ActivityLedger.overview(dbPath: walDir, window: .week, now: now, names: names, calendar: utc)
expect(fromWAL?.searchCount == got.searchCount && fromWAL?.changeTotal == got.changeTotal,
       "WAL ledger with no sidecar files reads the same numbers (got \(String(describing: fromWAL?.searchCount)))")
try? FileManager.default.removeItem(atPath: walDir)

print("ActivityLedgerTests: passed")

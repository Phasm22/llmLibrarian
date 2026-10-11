import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}

let log = """
2026-10-10 23:00:00,000 Watching this folder (check every 3600s, wait 30.0s after edits). Ctrl+C to stop.
2026-10-10 23:01:00,000 {"event":"batch_drain","ts":"2026-10-11T05:01:00+00:00","silo":"s","updated":3,"removed":0,"skipped":0,"errors":[]}
2026-10-10 23:02:00,000 {"event":"reconcile","ts":"2026-10-11T05:02:00+00:00","silo":"s"}
2026-10-10 23:03:00,000 {"event":"batch_drain","ts":"2026-10-11T05:03:00+00:00","silo":"s","updated":0,"removed":0,"skipped":2,"errors":[]}
2026-10-10 23:04:00,000 {"event":"batch_drain","ts":"2026-10-11T05:04:00.5+00:00","silo":"s","updated":7,"removed":1,"skipped":0,"updated_paths":["much thinks/2026-10-10.md","a.md","b.md","c.md","d.md"],"removed_paths":["old.md"],"errors":[]}
"""

let all = WatchActivity.parse(log, limit: 20)
expect(all.map(\.path) == ["much thinks/2026-10-10.md", "a.md", "b.md", "c.md", "d.md", "2 more", "old.md", "3 files"],
       "Newest event first, updates before removals, overflow and legacy counts summarized: \(all.map(\.path))")
expect(all[0].name == "2026-10-10.md" && !all[0].isCount, "Rows show the file name, not the folder path")
expect(all[5].isCount && !all[5].removed, "Overflow is a count line on the update side")
expect(all[6].removed && all[6].name == "old.md", "Removals are marked")
expect(all[7].isCount && all[7].name == "3 files", "Events without paths fall back to a count")
expect(all.allSatisfy { $0.at != nil }, "Timestamps parse with and without fractional seconds")
expect(Set(all.map(\.id)).count == all.count, "Row ids are unique")

expect(WatchActivity.parse(log, limit: 4).count == 4, "The limit caps the rows")
expect(WatchActivity.parse("", limit: 4).isEmpty, "An empty log shows nothing")
expect(WatchActivity.parse("noise {not json\n", limit: 4).isEmpty, "Malformed lines are ignored")

print("WatchActivityTests: passed")

import Foundation

/// What a watcher recently pushed into its silo, read from the `batch_drain`
/// JSON lines in its log. Foundation-only so `test.sh` can compile it alone.
enum WatchActivity {
    /// Only the last 64 KB of the log is read; a quiet watcher just shows nothing.
    static func recentChanges(logPath: String, limit: Int) -> [WatchChange] {
        guard let handle = FileHandle(forReadingAtPath: logPath) else { return [] }
        defer { try? handle.close() }
        let size = (try? handle.seekToEnd()) ?? 0
        try? handle.seek(toOffset: size > 65_536 ? size - 65_536 : 0)
        guard let data = try? handle.readToEnd() else { return [] }
        return parse(String(decoding: data, as: UTF8.self), limit: limit)
    }

    /// Newest first. Events written before paths were recorded become a count line.
    static func parse(_ text: String, limit: Int) -> [WatchChange] {
        var out: [WatchChange] = []
        for line in text.split(whereSeparator: \.isNewline).reversed() where line.contains("\"event\":\"batch_drain\"") {
            guard let brace = line.firstIndex(of: "{"),
                  let obj = try? JSONSerialization.jsonObject(with: Data(line[brace...].utf8)) as? [String: Any] else { continue }
            let at = Dates.parse(obj["ts"] as? String)
            for (removed, countKey, pathsKey) in [(false, "updated", "updated_paths"), (true, "removed", "removed_paths")] {
                let count = (obj[countKey] as? Int) ?? 0
                guard count > 0 else { continue }
                let paths = (obj[pathsKey] as? [String]) ?? []
                func add(_ path: String, isCount: Bool) {
                    out.append(WatchChange(id: out.count, removed: removed, path: path, isCount: isCount, at: at))
                }
                if paths.isEmpty {
                    add(count == 1 ? "1 file" : "\(count) files", isCount: true)
                } else {
                    paths.forEach { add($0, isCount: false) }
                    if count > paths.count { add("\(count - paths.count) more", isCount: true) }
                }
            }
            if out.count >= limit { break }
        }
        return Array(out.prefix(limit))
    }
}

import Foundation

/// Explicit, isolated fixtures for public screenshots. Never reads a real index.
enum SampleLibrary {
    static var enabled: Bool {
        let env = ProcessInfo.processInfo.environment
        return env["LLMLIBRARIAN_SNAPSHOT_DIR"] != nil && env["LLMLIBRARIAN_SNAPSHOT_SAMPLE"] == "1"
    }

    static let root = "/tmp/llmlibrarian-sample"
    static var config: PalConfig {
        PalConfig(palHome: root + "/.pal", workdir: root, dbPath: root + "/index",
                  python: "/usr/bin/false", palPath: root + "/pal.py")
    }

    static func prepare() throws {
        let fm = FileManager.default
        for folder in [root, root + "/Research", root + "/Project Notes", root + "/Photo Journal"] {
            try fm.createDirectory(atPath: folder, withIntermediateDirectories: true)
        }
        // Installation marker only; no executable or live service is used.
        if !fm.fileExists(atPath: config.palPath) {
            try Data("# Screenshot fixture only\n".utf8).write(to: URL(fileURLWithPath: config.palPath))
        }
    }

    static func snapshot(now: Date = Date(), window: ActivityWindow = .week) -> Snapshot {
        let names = ["Research", "Project Notes", "Photo Journal"]
        let slugs = ["research", "project-notes", "photo-journal"]
        let silos = names.enumerated().map { i, name in
            Silo(slug: slugs[i], displayName: name, path: root + "/" + name,
                 filesIndexed: [128, 64, 36][i], chunks: [840, 312, 36][i],
                 updated: now.addingTimeInterval(-120), imageVision: i == 2,
                 isPrivate: i == 2, host: "Sample Mac")
        }
        func service(_ kind: ServiceStatus.Kind, _ title: String, _ label: String, _ pid: Int) -> ServiceStatus {
            ServiceStatus(kind: kind, label: "sample." + label, title: title, subtitle: "Sample library",
                          installed: true, loaded: true, pid: pid, lastExit: 0, uptime: "2h 14m",
                          memoryMB: 32, probeOK: true, probeDetail: "Sample status",
                          logPath: root + "/.pal/logs/" + label + ".log", plistPath: root + "/" + label + ".plist")
        }
        let watchers = silos.enumerated().map { i, silo in
            var watcher = service(.watcher, silo.displayName, "watch." + silo.slug, 4200 + i)
            watcher.siloSlug = silo.slug
            watcher.recentChanges = [WatchChange(id: i, removed: false, path: "meeting-notes.md", isCount: false, at: now.addingTimeInterval(-120))]
            return watcher
        }
        let questions = ["What did we decide about offline search?", "Where are the launch checklist notes?", "Which sources explain hybrid retrieval?"]
        let queries = questions.enumerated().map { i, question in
            QueryRecord(id: i, timestamp: now.addingTimeInterval(Double(-180 - i * 300)),
                        tool: "query_personal_knowledge", silo: slugs[i % 2], queries: [question], chunks: 4,
                        sources: [QuerySource(file: "meeting-notes.md", path: silos[i % 2].path + "/meeting-notes.md", chunks: 4)],
                        truncated: false, errored: false,
                        client: QueryClient(label: i == 1 ? "Cursor" : "Claude", name: nil, version: nil,
                                            app: nil, pid: nil, transport: "http", endpoint: "/mcp", session: nil,
                                            entrypoint: nil, agent: nil, userAgent: nil, tag: nil))
        }
        var snap = Snapshot(silos: silos,
                        services: [service(.chroma, "Chroma vector store", "chroma", 4100), service(.mcp, "MCP server", "mcp", 4101)],
                        watchers: watchers, queries: queries,
                        mcpHealth: MCPHealth(ok: true, version: "0.1.0", startedAt: now.addingTimeInterval(-8040), dbPath: config.dbPath),
                        chromaReachable: true)
        snap.activity = activity(window: window, now: now, silos: silos, questions: questions)
        return snap
    }

    /// Synthetic history scaled to the window, so every range has a story to show.
    static func activity(window: ActivityWindow, now: Date, silos: [Silo], questions: [String]) -> ActivityOverview {
        let b = ActivityLedger.bounds(window, now: now, calendar: .current)
        var o = ActivityOverview(window: window, periodStart: b.start, periodEnd: now,
                                 previousStart: b.previousStart, previousEnd: b.previousEnd,
                                 filesSince: now.addingTimeInterval(-90 * 86_400),
                                 searchesSince: now.addingTimeInterval(-90 * 86_400))
        let scale = window.days
        o.timeline = b.buckets.enumerated().map { i, start in
            var bucket = ActivityBucket(start: start)
            bucket.added = (i * 7 + 3) % 4
            bucket.updated = (i * 5 + 1) % 6
            bucket.removed = i % 5 == 4 ? 1 : 0
            return bucket
        }
        let added = o.timeline.reduce(0) { $0 + $1.added }
        let updated = o.timeline.reduce(0) { $0 + $1.updated }
        let removed = o.timeline.reduce(0) { $0 + $1.removed }
        o.changeTotals = (added, updated, removed)
        o.previousChangeTotal = (added + updated + removed) * 4 / 5
        let files = ["meeting-notes.md", "offline-search.md", "launch-checklist.md", "retrieval-notes.md", "roadmap.md"]
        func changes(_ silo: Silo, _ add: Int, _ upd: Int, _ rem: Int, _ prev: Int) -> ActivityChangeSilo {
            var row = ActivityChangeSilo(slug: silo.slug, added: add, updated: upd, removed: rem)
            row.previousTotal = prev
            row.files = files.enumerated().map { i, f in
                ActivityFile(path: silo.path + "/" + f, kind: i == 0 ? "added" : "updated",
                             at: now.addingTimeInterval(Double(-3600 * (i + 1))))
            }
            return row
        }
        let (a1, u1, r1) = (added * 3 / 5, updated * 3 / 5, removed)
        o.changeSilos = [changes(silos[0], a1, u1, r1, o.previousChangeTotal / 2),
                         changes(silos[1], added - a1, updated - u1, 0, o.previousChangeTotal - o.previousChangeTotal / 2)]
            .filter { $0.total > 0 }
        let clients = ["Claude", "Cursor", "Claude", "Codex"]
        let count = 6 * scale
        let span = now.timeIntervalSince(b.start)
        o.searches = (0..<count).map { (i: Int) -> ActivitySearch in
            let offset: Double = span * (Double(i) + 0.5) / Double(count)
            let silo: String? = i % 3 == 2 ? nil : silos[i % 2].slug
            let missed = i % 7 == 6
            return ActivitySearch(id: i, at: now.addingTimeInterval(-offset), silo: silo,
                                  query: missed ? "quarterly budget owner" : questions[i % questions.count],
                                  sourcesFound: !missed, client: clients[i % clients.count])
        }
        for search in o.searches {
            guard let i = o.timeline.lastIndex(where: { $0.start <= search.at }) else { continue }
            o.timeline[i].searches += 1
            if !search.sourcesFound { o.timeline[i].withoutSources += 1 }
        }
        o.previousSearches = 4 * scale
        o.useSilos = silos.prefix(2).enumerated().map { i, silo in
            ActivityUseSilo(slug: silo.slug, searches: o.searches.filter { $0.silo == silo.slug }.count, uniqueFiles: [9, 5][i])
        }
        o.topDocuments = files.prefix(4).enumerated().map { i, f in
            ActivityDocument(slug: silos[i % 2].slug, path: silos[i % 2].path + "/" + f, searches: max(1, (4 - i) * scale / 2))
        }
        var counts: [String: Int] = [:]
        for s in o.searches { counts[s.client, default: 0] += 1 }
        o.clients = counts.map { (name: $0.key, searches: $0.value) }.sorted { $0.searches > $1.searches }
        let empty = o.searches.filter { !$0.sourcesFound }
        if let first = empty.first {
            o.noSourceGroups = [ActivityQueryGroup(query: "quarterly budget owner", silo: first.silo, count: empty.count,
                                                   lastSeen: first.at, searchIDs: empty.map(\.id))]
        }
        o.failureGroups = [ActivityFailureGroup(slug: silos[0].slug, errorClass: "encrypted", files: 2, occurrences: 5,
                                                sampleError: "PdfReadError: file has not been decrypted",
                                                lastSeen: now.addingTimeInterval(-7200),
                                                paths: [silos[0].path + "/scans/contract.pdf", silos[0].path + "/scans/receipt.pdf"])]
        o.openFailures = [silos[0].slug: 2]
        for (i, silo) in silos.enumerated() {
            o.lastChange[silo.slug] = now.addingTimeInterval(Double(-3600 * (i + 1)))
            if i < 2 { o.lastSearch[silo.slug] = now.addingTimeInterval(Double(-600 * (i + 1))) }
        }
        return o
    }
}

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

    static let sampleFiles = ["meeting-notes.md", "offline-search.md", "launch-checklist.md", "retrieval-notes.md", "roadmap.md"]
    static let sampleClients = ["Claude", "Cursor", "Claude", "Codex"]

    /// Synthetic searches spread evenly over the window; every 7th finds no sources.
    static func sampleSearches(window: ActivityWindow, now: Date, silos: [Silo], questions: [String]) -> [ActivitySearchRow] {
        let b = ActivityLedger.bounds(window, now: now, calendar: .current)
        let count = 6 * window.days
        let span = now.timeIntervalSince(b.start)
        return (0..<count).map { (i: Int) -> ActivitySearchRow in
            let offset: Double = span * (Double(i) + 0.5) / Double(count)
            let silo = silos[i % 2]
            let missed = i % 7 == 6
            var row = ActivitySearchRow(id: i, at: now.addingTimeInterval(-offset), slug: i % 3 == 2 ? nil : silo.slug,
                                        query: missed ? "quarterly budget owner" : questions[i % questions.count],
                                        sourcesFound: !missed, uniqueFiles: missed ? 0 : 2,
                                        client: sampleClients[i % sampleClients.count])
            if !missed {
                row.hits = (0..<2).map { k in ActivityHit(slug: silo.slug, path: silo.path + "/" + sampleFiles[(i + k) % sampleFiles.count]) }
            }
            return row
        }
    }

    static func sampleChanges(window: ActivityWindow, now: Date, silos: [Silo]) -> [ActivityChangeRow] {
        let b = ActivityLedger.bounds(window, now: now, calendar: .current)
        var rows: [ActivityChangeRow] = []
        for (i, start) in b.buckets.enumerated() {
            let kinds = Array(repeating: "added", count: (i * 7 + 3) % 4) + Array(repeating: "updated", count: (i * 5 + 1) % 6)
                + (i % 5 == 4 ? ["removed"] : [])
            for (k, kind) in kinds.enumerated() {
                let silo = silos[(k + i) % 5 < 3 ? 0 : 1]
                let at = min(now, start.addingTimeInterval(Double(k + 1) * (window == .today ? 300 : 3000)))
                rows.append(ActivityChangeRow(id: rows.count, at: at, slug: silo.slug,
                                              path: silo.path + "/" + sampleFiles[(i + k) % sampleFiles.count], kind: kind))
            }
        }
        return rows.sorted { $0.at > $1.at }
    }

    static func sampleFailures(now: Date, silos: [Silo]) -> [ActivityFailureRow] {
        ["contract.pdf", "receipt.pdf"].enumerated().map { i, f in
            ActivityFailureRow(id: i, slug: silos[0].slug, path: silos[0].path + "/scans/" + f, errorClass: "encrypted",
                               error: "PdfReadError: file has not been decrypted", lastSeen: now.addingTimeInterval(Double(-7200 - i * 600)),
                               occurrences: [3, 2][i])
        }
    }

    /// Summary built from the same synthetic rows the Activity page lists, so every
    /// number on the Overview matches what a click-through shows.
    static func activity(window: ActivityWindow, now: Date, silos: [Silo], questions: [String]) -> ActivitySummary {
        let b = ActivityLedger.bounds(window, now: now, calendar: .current)
        var s = ActivitySummary(window: window, periodStart: b.start, periodEnd: now,
                                previousStart: b.previousStart, previousEnd: b.previousEnd,
                                filesSince: now.addingTimeInterval(-90 * 86_400),
                                searchesSince: now.addingTimeInterval(-90 * 86_400))
        s.buckets = b.buckets.map { ActivityBucket(start: $0) }
        func slot(_ d: Date) -> Int? { s.buckets.lastIndex { $0.start <= d } }
        for c in sampleChanges(window: window, now: now, silos: silos) {
            switch c.kind {
            case "added": s.added += 1; if let i = slot(c.at) { s.buckets[i].added += 1 }
            case "updated": s.updated += 1; if let i = slot(c.at) { s.buckets[i].updated += 1 }
            default: s.removed += 1; if let i = slot(c.at) { s.buckets[i].removed += 1 }
            }
            s.perSilo[c.slug, default: .init()].changes += 1
            s.perSilo[c.slug, default: .init()].lastChange = max(s.perSilo[c.slug]?.lastChange ?? c.at, c.at)
        }
        for q in sampleSearches(window: window, now: now, silos: silos, questions: questions) {
            s.searches += 1
            if !q.sourcesFound { s.noSource += 1 }
            if let i = slot(q.at) { s.buckets[i].searches += 1; if !q.sourcesFound { s.buckets[i].noSource += 1 } }
            for slug in Set([q.slug].compactMap { $0 } + q.hits.map(\.slug)) {
                s.perSilo[slug, default: .init()].searches += 1
                s.perSilo[slug, default: .init()].lastSearch = max(s.perSilo[slug]?.lastSearch ?? q.at, q.at)
            }
        }
        s.previousChangeCount = s.changes * 4 / 5
        s.previousSearchCount = s.searches * 2 / 3
        s.previousNoSourceCount = max(0, s.noSource - 2)
        let failures = sampleFailures(now: now, silos: silos)
        s.failures = [ActivityFailureGroup(slug: silos[0].slug, errorClass: "encrypted", files: failures.count,
                                           occurrences: failures.reduce(0) { $0 + $1.occurrences },
                                           lastSeen: failures[0].lastSeen, sampleError: failures[0].error)]
        s.perSilo[silos[0].slug, default: .init()].openFailures = failures.count
        s.perSilo[silos[2].slug, default: .init()].lastChange = now.addingTimeInterval(-12 * 86_400)
        return s
    }

    static func detail(_ r: ActivityDetailRequest, now: Date = Date()) -> ActivityDetail {
        let snap = snapshot(now: now, window: r.window)
        let questions = ["What did we decide about offline search?", "Where are the launch checklist notes?", "Which sources explain hybrid retrieval?"]
        var d = ActivityDetail()
        switch r.tab {
        case .searches:
            d.searches = sampleSearches(window: r.window, now: now, silos: snap.silos, questions: questions).filter { q in
                (r.silo == nil || q.slug == r.silo || q.hits.contains { $0.slug == r.silo }) && (!r.noSourceOnly || !q.sourcesFound)
            }
            var docs: [String: (String, Int)] = [:]
            for q in d.searches { for h in q.hits { docs[h.path, default: (h.slug, 0)].1 += 1 } }
            d.documents = docs.map { ActivityDocumentRow(slug: $0.value.0, path: $0.key, searches: $0.value.1) }
                .sorted { ($1.searches, $0.path) < ($0.searches, $1.path) }
            var clients: [String: Int] = [:]
            for q in d.searches { clients[q.client, default: 0] += 1 }
            d.clients = clients.map { (name: $0.key, searches: $0.value) }.sorted { $0.searches > $1.searches }
        case .changes:
            d.changes = sampleChanges(window: r.window, now: now, silos: snap.silos).filter { r.silo == nil || $0.slug == r.silo }
        case .problems:
            d.failures = sampleFailures(now: now, silos: snap.silos).filter { r.silo == nil || $0.slug == r.silo }
        }
        return d
    }
}

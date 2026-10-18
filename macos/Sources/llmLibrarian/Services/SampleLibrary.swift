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

    static func snapshot(now: Date = Date()) -> Snapshot {
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
        return Snapshot(silos: silos,
                        services: [service(.chroma, "Chroma vector store", "chroma", 4100), service(.mcp, "MCP server", "mcp", 4101)],
                        watchers: watchers, queries: queries,
                        mcpHealth: MCPHealth(ok: true, version: "0.1.0", startedAt: now.addingTimeInterval(-8040), dbPath: config.dbPath),
                        chromaReachable: true)
    }
}

import Foundation

/// Gathers everything the UI shows. Runs off the main actor; pure functions over
/// files, launchctl, ps, and two local HTTP endpoints. No Python is started.
enum Collector {
    static func collect(config: PalConfig) async -> Snapshot {
        if SampleLibrary.enabled { return SampleLibrary.snapshot() }
        var snap = Snapshot()
        let (silos, regErr) = readSilos(config)
        snap.silos = silos
        snap.registryError = regErr

        async let mcpProbe = probeMCP(config)
        async let chromaProbe = probeChroma(config)
        let jobs = Launchctl.list()
        let locks = readWatchLocks(config)
        let plists = installedPlists(config)

        let mcp = await mcpProbe
        let chromaOK = await chromaProbe
        snap.mcpHealth = mcp
        snap.chromaReachable = chromaOK

        snap.services = [
            buildService(kind: .chroma, label: "com.llmlibrarian.chroma", title: "Chroma vector store",
                         subtitle: "\(config.chromaHost):\(config.chromaPort)",
                         logName: "llmlibrarian-chroma.stderr.log",
                         probeOK: chromaOK,
                         probeDetail: chromaOK == true ? "Heartbeat OK" : "No heartbeat on port \(config.chromaPort)",
                         jobs: jobs, plists: plists, config: config),
            buildService(kind: .mcp, label: "com.llmlibrarian.mcp", title: "MCP server",
                         subtitle: config.mcpURL,
                         logName: "llmlibrarian-mcp.stderr.log",
                         probeOK: mcp.map(\.ok),
                         probeDetail: mcp.map { h in
                             "/healthz OK · v\(h.version ?? "?")" + (h.litePath.map { " · lite at \($0)" } ?? "")
                         } ?? "/healthz not responding",
                         jobs: jobs, plists: plists, config: config),
        ]

        // Watchers: union of loaded launchd jobs, installed plists, and lock files.
        var slugs = Set<String>()
        for label in jobs.keys where label.hasPrefix("io.llmlibrarian.watch.") {
            slugs.insert(String(label.dropFirst("io.llmlibrarian.watch.".count)))
        }
        for label in plists where label.hasPrefix("io.llmlibrarian.watch.") {
            slugs.insert(String(label.dropFirst("io.llmlibrarian.watch.".count)))
        }
        for slug in locks.keys { slugs.insert(slug) }
        let siloByslug = Dictionary(uniqueKeysWithValues: silos.map { ($0.slug, $0) })
        snap.watchers = slugs.sorted().map { slug in
            let label = "io.llmlibrarian.watch.\(slug)"
            let silo = siloByslug[slug]
            var s = buildService(kind: .watcher, label: label,
                                 title: silo?.displayName ?? slug,
                                 subtitle: silo?.tildePath ?? (locks[slug]?.rootPath ?? slug),
                                 logName: "watch-\(slug).log",
                                 probeOK: nil, probeDetail: nil,
                                 jobs: jobs, plists: plists, config: config)
            s.siloSlug = slug
            s.recentChanges = WatchActivity.recentChanges(logPath: s.logPath, limit: 4)
            if let lock = locks[slug] {
                // Lock claims a pid that is not the one launchd is running → stale.
                if let jobPid = s.pid, lock.pid != jobPid { s.lockStale = true }
                if s.pid == nil, lock.pid != nil { s.lockStale = true }
            }
            return s
        }

        snap.queries = readQueries(config, limit: 100)
        snap.unindexedBookmarks = readBookmarks(config).filter { path in
            !silos.contains { $0.path == path }
        }
        return snap
    }

    // MARK: silos

    static func readSilos(_ config: PalConfig) -> ([Silo], String?) {
        guard let data = FileManager.default.contents(atPath: config.registryPath) else {
            return ([], "No silo registry at \(config.registryPath)")
        }
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return ([], "Silo registry is not valid JSON")
        }
        var out: [Silo] = []
        for (slug, raw) in obj {
            guard let e = raw as? [String: Any] else { continue }
            out.append(Silo(
                slug: (e["slug"] as? String) ?? slug,
                displayName: (e["display_name"] as? String) ?? slug,
                path: (e["path"] as? String) ?? "",
                filesIndexed: (e["files_indexed"] as? Int) ?? 0,
                chunks: (e["chunks_count"] as? Int) ?? 0,
                updated: Dates.parse(e["updated"] as? String),
                imageVision: (e["image_vision_enabled"] as? Bool) ?? false,
                isPrivate: (e["private"] as? Bool) ?? false,
                host: (e["host"] as? String) ?? ""
            ))
        }
        return (out.sorted { $0.displayName.localizedCaseInsensitiveCompare($1.displayName) == .orderedAscending }, nil)
    }

    static func readBookmarks(_ config: PalConfig) -> [String] {
        guard let data = FileManager.default.contents(atPath: config.bookmarksPath),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let marks = obj["bookmarks"] as? [[String: Any]] else { return [] }
        return marks.compactMap { $0["path"] as? String }
    }

    // MARK: services

    struct WatchLock { let pid: Int?; let rootPath: String?; let startedAt: Date? }

    static func readWatchLocks(_ config: PalConfig) -> [String: WatchLock] {
        let fm = FileManager.default
        guard let names = try? fm.contentsOfDirectory(atPath: config.watchLocksDir) else { return [:] }
        var out: [String: WatchLock] = [:]
        for name in names where name.hasSuffix(".pid") {
            let path = config.watchLocksDir + "/" + name
            guard let data = fm.contents(atPath: path),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
                  let slug = obj["silo"] as? String else { continue }
            let started = (obj["started_at"] as? Double).map { Date(timeIntervalSince1970: $0) }
            out[slug] = WatchLock(pid: obj["pid"] as? Int, rootPath: obj["root_path"] as? String, startedAt: started)
        }
        return out
    }

    static func installedPlists(_ config: PalConfig) -> Set<String> {
        guard let names = try? FileManager.default.contentsOfDirectory(atPath: config.launchAgentsDir) else { return [] }
        return Set(names.filter { $0.lowercased().contains("llmlibrarian") && $0.hasSuffix(".plist") }
            .map { String($0.dropLast(".plist".count)) })
    }

    static func buildService(kind: ServiceStatus.Kind, label: String, title: String, subtitle: String,
                             logName: String, probeOK: Bool?, probeDetail: String?,
                             jobs: [String: Launchctl.Job], plists: Set<String>, config: PalConfig) -> ServiceStatus {
        let job = jobs[label]
        var s = ServiceStatus(kind: kind, label: label, title: title, subtitle: subtitle,
                              installed: plists.contains(label) || job != nil,
                              loaded: job != nil, pid: job?.pid, lastExit: job?.lastExit,
                              probeOK: probeOK, probeDetail: probeDetail,
                              logPath: config.logsDir + "/" + logName,
                              plistPath: config.launchAgentsDir + "/" + label + ".plist")
        if let pid = job?.pid {
            let st = ProcessInfoProbe.stats(pid: pid)
            s.uptime = st.uptime
            s.memoryMB = st.memoryMB
        }
        return s
    }

    // MARK: probes

    static func probeMCP(_ config: PalConfig) async -> MCPHealth? {
        guard let data = await fetch(config.mcpHealthURL),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        var health = MCPHealth(ok: (obj["ok"] as? Bool) ?? false,
                               version: obj["version"] as? String,
                               startedAt: Dates.parse(obj["started_at"] as? String),
                               dbPath: obj["db_path"] as? String,
                               litePath: obj["lite_path"] as? String)
        // A server started before /healthz reported lite_path: ask the mount itself.
        if health.litePath == nil, let path = config.mcpLitePath,
           let url = URL(string: config.mcpURL(path: path)), await isRouted(url) {
            health.litePath = path
        }
        return health
    }

    /// True when the server routes `url` at all. A GET on an MCP endpoint is
    /// refused (405/406) rather than served, so only 404 means "not mounted".
    static func isRouted(_ url: URL) async -> Bool {
        var req = URLRequest(url: url)
        req.timeoutInterval = 2.5
        req.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        guard let (_, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse else { return false }
        return http.statusCode != 404
    }

    static func probeChroma(_ config: PalConfig) async -> Bool? {
        guard let data = await fetch(config.chromaHeartbeatURL) else { return false }
        return !data.isEmpty
    }

    static func fetch(_ url: URL) async -> Data? {
        var req = URLRequest(url: url)
        req.timeoutInterval = 2.5
        req.cachePolicy = .reloadIgnoringLocalAndRemoteCacheData
        guard let (data, resp) = try? await URLSession.shared.data(for: req),
              let http = resp as? HTTPURLResponse, (200..<300).contains(http.statusCode) else { return nil }
        return data
    }

    // MARK: query audit

    static func readQueries(_ config: PalConfig, limit: Int) -> [QueryRecord] {
        guard let text = try? String(contentsOfFile: config.queryAuditPath, encoding: .utf8) else { return [] }
        let lines = text.split(whereSeparator: \.isNewline).suffix(limit)
        var out: [QueryRecord] = []
        for (i, line) in lines.enumerated() {
            guard let data = line.data(using: .utf8),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { continue }
            let result = obj["result"] as? [String: Any] ?? [:]
            let sources = (result["sources"] as? [[String: Any]] ?? []).map {
                QuerySource(file: ($0["file"] as? String) ?? "?", path: ($0["path"] as? String) ?? "",
                            chunks: ($0["chunks"] as? Int) ?? 0)
            }
            let errors = result["errors"]
            let errored: Bool = {
                if let a = errors as? [Any] { return !a.isEmpty }
                if let d = errors as? [String: Any] { return !d.isEmpty }
                return errors != nil && !(errors is NSNull)
            }()
            let client: QueryClient? = (obj["client"] as? [String: Any]).map { c in
                QueryClient(
                    label: (c["label"] as? String) ?? "Unknown",
                    name: c["client_name"] as? String,
                    version: c["client_version"] as? String,
                    app: c["app"] as? String,
                    pid: (c["pid"] as? Int) ?? (c["server_pid"] as? Int),
                    transport: c["transport"] as? String,
                    endpoint: c["endpoint"] as? String,
                    session: c["session"] as? String,
                    entrypoint: c["entrypoint"] as? String,
                    agent: c["agent"] as? String,
                    userAgent: c["user_agent"] as? String,
                    tag: c["tag"] as? String
                )
            }
            out.append(QueryRecord(
                id: i,
                timestamp: Dates.parse(obj["ts"] as? String),
                tool: (obj["tool"] as? String) ?? "query",
                silo: obj["silo"] as? String,
                queries: (obj["queries"] as? [String]) ?? [],
                chunks: (result["chunks"] as? Int) ?? 0,
                sources: sources,
                truncated: (result["truncated"] as? Bool) ?? false,
                errored: errored,
                client: client
            ))
        }
        return out.reversed()
    }
}

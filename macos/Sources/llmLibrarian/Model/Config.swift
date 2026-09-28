import Foundation

/// Where the pal/llmli installation lives. Mirrors what `pal daemon install`
/// records in ~/.pal/daemon.json plus the ports from <workdir>/.env.mcp.
struct PalConfig: Sendable, Equatable {
    var palHome: String
    var workdir: String
    var dbPath: String
    var python: String
    var palPath: String
    var mcpHost = "127.0.0.1"
    var mcpPort = 8765
    var mcpPath = "/mcp"
    var chromaHost = "127.0.0.1"
    var chromaPort = 8000

    var logsDir: String { palHome + "/logs" }
    var watchLocksDir: String { palHome + "/watch_locks" }
    var registryPath: String { dbPath + "/llmli_registry.json" }
    var bookmarksPath: String { palHome + "/registry.json" }
    var queryAuditPath: String { logsDir + "/query-audit.jsonl" }
    var launchAgentsDir: String { NSHomeDirectory() + "/Library/LaunchAgents" }
    var mcpURL: String { "http://\(mcpHost):\(mcpPort)\(mcpPath)" }
    var mcpHealthURL: URL { URL(string: "http://\(mcpHost):\(mcpPort)/healthz")! }
    var chromaHeartbeatURL: URL { URL(string: "http://\(chromaHost):\(chromaPort)/api/v2/heartbeat")! }

    var isInstalled: Bool { FileManager.default.fileExists(atPath: palPath) }

    static func load() -> PalConfig {
        let env = ProcessInfo.processInfo.environment
        let home = NSHomeDirectory()
        let palHome = env["PAL_HOME"] ?? home + "/.pal"
        var cfg = PalConfig(
            palHome: palHome,
            workdir: home + "/llmLibrarian",
            dbPath: home + "/llmLibrarian/my_brain_db",
            python: home + "/llmLibrarian/.venv/bin/python3",
            palPath: home + "/llmLibrarian/pal.py"
        )
        if let data = FileManager.default.contents(atPath: palHome + "/daemon.json"),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            if let s = obj["workdir"] as? String { cfg.workdir = s }
            if let s = obj["db_path"] as? String { cfg.dbPath = s }
            if let s = obj["python_executable"] as? String { cfg.python = s }
            if let s = obj["pal_path"] as? String { cfg.palPath = s }
            if let s = obj["pal_home"] as? String { cfg.palHome = s }
        }
        if let db = env["LLMLIBRARIAN_DB"] { cfg.dbPath = db }
        let dotenv = Self.parseDotEnv(at: cfg.workdir + "/.env.mcp")
        func pick(_ key: String) -> String? { env[key] ?? dotenv[key] }
        if let s = pick("LLMLIBRARIAN_MCP_HOST"), !s.isEmpty { cfg.mcpHost = s }
        if let s = pick("LLMLIBRARIAN_MCP_PORT"), let p = Int(s) { cfg.mcpPort = p }
        if let s = pick("LLMLIBRARIAN_MCP_PATH"), !s.isEmpty { cfg.mcpPath = s }
        if let s = pick("LLMLIBRARIAN_CHROMA_HOST"), !s.isEmpty { cfg.chromaHost = s }
        if let s = pick("LLMLIBRARIAN_CHROMA_PORT"), let p = Int(s) { cfg.chromaPort = p }
        return cfg
    }

    /// Environment handed to pal subprocesses (GUI apps launch with a bare PATH).
    func subprocessEnvironment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let venvBin = (python as NSString).deletingLastPathComponent
        let path = [venvBin, "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"]
        env["PATH"] = path.joined(separator: ":")
        env["HOME"] = NSHomeDirectory()
        env["PAL_HOME"] = palHome
        env["LLMLIBRARIAN_DB"] = dbPath
        env["LLMLIBRARIAN_CHROMA_HOST"] = chromaHost
        env["LLMLIBRARIAN_CHROMA_PORT"] = String(chromaPort)
        env["LLMLIBRARIAN_MCP_HOST"] = mcpHost
        env["LLMLIBRARIAN_MCP_PORT"] = String(mcpPort)
        env["LLMLIBRARIAN_MCP_PATH"] = mcpPath
        env["PYTHONUNBUFFERED"] = "1"
        for (k, v) in Self.parseDotEnv(at: workdir + "/.env.mcp") where env[k] == nil { env[k] = v }
        return env
    }

    static func parseDotEnv(at path: String) -> [String: String] {
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return [:] }
        var out: [String: String] = [:]
        for raw in text.split(whereSeparator: \.isNewline) {
            var line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty || line.hasPrefix("#") { continue }
            if line.hasPrefix("export ") { line = String(line.dropFirst(7)) }
            guard let eq = line.firstIndex(of: "=") else { continue }
            let key = line[..<eq].trimmingCharacters(in: .whitespaces)
            var value = line[line.index(after: eq)...].trimmingCharacters(in: .whitespaces)
            if value.count >= 2, let f = value.first, let l = value.last, f == l, f == "\"" || f == "'" {
                value = String(value.dropFirst().dropLast())
            }
            out[key] = value
        }
        return out
    }
}

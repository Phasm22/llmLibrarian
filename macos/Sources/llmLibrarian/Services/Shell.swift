import Foundation

enum Shell {
    struct Result: Sendable {
        let status: Int32
        let stdout: String
        let stderr: String
        var ok: Bool { status == 0 }
        var combined: String {
            let s = stdout.trimmingCharacters(in: .whitespacesAndNewlines)
            let e = stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            return [s, e].filter { !$0.isEmpty }.joined(separator: "\n")
        }
    }

    @discardableResult
    static func run(_ executable: String, _ arguments: [String],
                    cwd: String? = nil, env: [String: String]? = nil) -> Result {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: executable)
        p.arguments = arguments
        if let cwd { p.currentDirectoryURL = URL(fileURLWithPath: cwd) }
        if let env { p.environment = env }
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        do { try p.run() } catch {
            return Result(status: -1, stdout: "", stderr: error.localizedDescription)
        }
        let o = out.fileHandleForReading.readDataToEndOfFile()
        let e = err.fileHandleForReading.readDataToEndOfFile()
        p.waitUntilExit()
        return Result(status: p.terminationStatus,
                      stdout: String(decoding: o, as: UTF8.self),
                      stderr: String(decoding: e, as: UTF8.self))
    }
}

enum Launchctl {
    struct Job: Sendable { let label: String; let pid: Int?; let lastExit: Int }

    static var domain: String { "gui/\(getuid())" }

    /// `launchctl list` filtered to llmLibrarian jobs. Columns: PID, last exit, label.
    static func list() -> [String: Job] {
        let r = Shell.run("/bin/launchctl", ["list"])
        var jobs: [String: Job] = [:]
        for line in r.stdout.split(separator: "\n").dropFirst() {
            let parts = line.split(separator: "\t", omittingEmptySubsequences: false)
            guard parts.count >= 3 else { continue }
            let label = String(parts[2])
            guard label.lowercased().contains("llmlibrarian") else { continue }
            jobs[label] = Job(label: label, pid: Int(parts[0]), lastExit: Int(parts[1]) ?? 0)
        }
        return jobs
    }

    static func kickstart(_ label: String) -> Shell.Result {
        Shell.run("/bin/launchctl", ["kickstart", "-k", "\(domain)/\(label)"])
    }

    static func bootout(_ label: String) -> Shell.Result {
        Shell.run("/bin/launchctl", ["bootout", "\(domain)/\(label)"])
    }

    static func bootstrap(plist: String) -> Shell.Result {
        Shell.run("/bin/launchctl", ["bootstrap", domain, plist])
    }
}

enum ProcessInfoProbe {
    /// (elapsed time string, resident MB) for a pid via `ps`.
    static func stats(pid: Int) -> (uptime: String?, memoryMB: Double?) {
        let r = Shell.run("/bin/ps", ["-o", "etime=,rss=", "-p", String(pid)])
        let parts = r.stdout.split(whereSeparator: { $0 == " " || $0 == "\n" })
        guard parts.count >= 2 else { return (nil, nil) }
        let mb = Double(parts[1]).map { $0 / 1024.0 }
        return (humanizeEtime(String(parts[0])), mb)
    }

    /// ps etime is [[dd-]hh:]mm:ss → "3d 4h", "2h 15m", "12m", "45s".
    static func humanizeEtime(_ s: String) -> String {
        var days = 0
        var rest = s
        if let dash = rest.firstIndex(of: "-") {
            days = Int(rest[..<dash]) ?? 0
            rest = String(rest[rest.index(after: dash)...])
        }
        let comps = rest.split(separator: ":").compactMap { Int($0) }
        var h = 0, m = 0, sec = 0
        switch comps.count {
        case 3: (h, m, sec) = (comps[0], comps[1], comps[2])
        case 2: (m, sec) = (comps[0], comps[1])
        case 1: sec = comps[0]
        default: return s
        }
        if days > 0 { return "\(days)d \(h)h" }
        if h > 0 { return "\(h)h \(m)m" }
        if m > 0 { return "\(m)m" }
        return "\(sec)s"
    }
}

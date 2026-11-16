import Foundation

enum AppRebuild {
    /// Write directly to a file: the installer must survive closing its parent
    /// GUI, and compiler output must never fill a pipe and stall the build.
    static func start(workdir: String, logPath: String, environment: [String: String],
                      onExit: @escaping @Sendable (Int32) -> Void) throws -> Process {
        let url = URL(fileURLWithPath: logPath)
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                withIntermediateDirectories: true)
        try Data().write(to: url)
        let log = try FileHandle(forWritingTo: url)
        defer { try? log.close() }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/bash")
        process.arguments = [workdir + "/macos/build.sh", "--install", "--relaunch"]
        process.currentDirectoryURL = URL(fileURLWithPath: workdir)
        process.environment = environment
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = log
        process.standardError = log
        process.terminationHandler = { onExit($0.terminationStatus) }
        try process.run()
        return process
    }
}

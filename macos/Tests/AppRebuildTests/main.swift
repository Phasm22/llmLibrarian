import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}

let root = FileManager.default.temporaryDirectory.appendingPathComponent("App rebuild \(UUID())")
try FileManager.default.createDirectory(at: root.appendingPathComponent("macos"), withIntermediateDirectories: true)
defer { try? FileManager.default.removeItem(at: root) }
let script = """
[[ "$1" == --install && "$2" == --relaunch ]] || exit 90
[[ "$BUILD_TEST_VALUE" == 'value with spaces' ]] || exit 91
echo 'compiler output'
echo 'compiler error' >&2
exit 7
"""
try script.write(to: root.appendingPathComponent("macos/build.sh"), atomically: true, encoding: .utf8)
let logPath = root.appendingPathComponent("logs/build.log").path
let done = DispatchSemaphore(value: 0)
let process = try AppRebuild.start(workdir: root.path, logPath: logPath,
                                   environment: ["BUILD_TEST_VALUE": "value with spaces"]) { status in
    expect(status == 7, "Build failure status must reach the UI callback")
    done.signal()
}
expect(done.wait(timeout: .now() + 10) == .success, "Build must finish without blocking on output")
process.waitUntilExit()
let output = try String(contentsOfFile: logPath, encoding: .utf8)
expect(output.contains("compiler output") && output.contains("compiler error"), "Both streams must reach the log")
do {
    _ = try AppRebuild.start(workdir: root.appendingPathComponent("missing").path,
                             logPath: logPath, environment: [:]) { _ in }
    fatalError("An invalid checkout must throw a launch error")
} catch { }
print("App rebuild tests passed")

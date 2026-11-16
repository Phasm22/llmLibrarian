import Foundation
import AppKit

// The production store is compiled unchanged. These two UI entry points are
// inert so tests cannot open windows or launch a snapshot on the user's app.
@MainActor enum AppDelegate { static func showMainWindow() {} }
enum SnapshotMode { static var directory: String? { nil } }

actor ScenarioWorld {
    var snapshot = Snapshot()
    func read() -> Snapshot { snapshot }
    func set(_ value: Snapshot) { snapshot = value }
}

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}

@main struct Scenarios {
    @MainActor static func wait(_ store: LibrarianStore) async {
        for _ in 0..<400 {
            if !store.jobs.contains(where: \.isRunning) && store.pendingServiceActions.isEmpty { return }
            try? await Task.sleep(for: .milliseconds(25))
        }
        fatalError("Scenario did not finish within 10 seconds")
    }

    @MainActor static func main() async throws {
        unsetenv("LLMLIBRARIAN_SNAPSHOT_DIR")
        unsetenv("LLMLIBRARIAN_SNAPSHOT_SAMPLE")
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("llmlibrarian-flow-tests-\(UUID())")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let folder = root.appendingPathComponent("Folder with spaces")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        try "A small indexable document".write(to: folder.appendingPathComponent("note.txt"), atomically: true, encoding: .utf8)
        let script = root.appendingPathComponent("fake-pal.sh")
        let cfg = PalConfig(palHome: root.path + "/pal", workdir: root.path, dbPath: root.path + "/db",
                            python: "/bin/bash", palPath: script.path)
        let silo = Silo(slug: "fixture", displayName: "Fixture", path: folder.path, filesIndexed: 1, chunks: 2,
                        updated: Date(), imageVision: false, isPrivate: false, host: "test")
        let world = ScenarioWorld()
        func runtime(_ config: PalConfig = cfg, commandResult: Int32 = 0) -> StoreRuntime {
            StoreRuntime(config: { config }, collect: { _, _ in await world.read() },
                         serviceCommand: { _, _, _ in Shell.Result(status: commandResult, stdout: "", stderr: commandResult == 0 ? "" : "Permission denied") },
                         settleDelays: [.milliseconds(1), .milliseconds(1)])
        }
        func writeScript(_ contents: String) throws { try contents.write(to: script, atomically: true, encoding: .utf8) }
        func indexed(failures: Int = 0) -> Snapshot {
            var snap = Snapshot(silos: [silo])
            snap.watchers = [ServiceStatus(kind: .watcher, label: "test.watch.fixture", title: "Fixture watcher", subtitle: "Fixture",
                                          installed: true, loaded: true, pid: 123, lastExit: 0, siloSlug: silo.slug,
                                          logPath: root.path + "/watch.log", plistPath: root.path + "/watch.plist")]
            var summary = ActivitySummary(window: .week, periodStart: Date(), periodEnd: Date(), previousStart: Date(), previousEnd: Date(), filesSince: nil, searchesSince: nil)
            summary.perSilo[silo.slug] = ActivityCollectionStats(openFailures: failures)
            snap.activity = summary
            return snap
        }

        // A missing folder must fail before a process is started.
        let missing = LibrarianStore(runtime: runtime(), autoRefresh: false)
        missing.addSilos(at: [root.appendingPathComponent("gone")])
        expect(missing.problem != nil && missing.jobs.isEmpty, "Missing-folder add must explain the failure")

        await world.set(indexed())
        let duplicate = LibrarianStore(runtime: runtime(), autoRefresh: false)
        await duplicate.refresh()
        duplicate.addSilos(at: [folder, folder])
        expect(duplicate.pendingAddRequest == nil && duplicate.selectedSilo == silo.slug, "Adding an existing folder must select it, not run a duplicate ingest")

        // Exercise real subprocess exits and the actual add -> watcher chain.
        for exitCode in [1, 75, 0] {
            try writeScript("echo \"$*\" >> calls.txt\nif [[ \"$1\" == pull ]]; then echo 'diagnostic from fixture' >&2; exit \(exitCode); fi\n")
            try Data().write(to: root.appendingPathComponent("calls.txt"))
            await world.set(Snapshot())
            let store = LibrarianStore(runtime: runtime(), autoRefresh: false)
            await store.refresh()
            store.addSilo(at: folder)
            await world.set(indexed())
            store.confirmAddSilos(imageVision: false)
            await wait(store)
            let calls = try String(contentsOf: root.appendingPathComponent("calls.txt"), encoding: .utf8)
            expect(calls.contains("daemon sync") == (exitCode == 0), "Busy/failed indexing must not run watcher setup")
            expect((store.lastActionMessage?.contains("Added") == true) == (exitCode == 0), "Only a completed chain may claim the folder was added")
            let job = store.jobs.last!
            expect(FileManager.default.fileExists(atPath: job.logPath!), "Every job must have a durable log")
            expect(job.output.contains("diagnostic from fixture"), "Stderr must remain visible after a job finishes")
        }

        try writeScript("echo \"$*\" >> calls.txt\nexit 0\n")
        await world.set(Snapshot())
        let noWatcher = LibrarianStore(runtime: runtime(), autoRefresh: false)
        await noWatcher.refresh(); noWatcher.addSilo(at: folder)
        var withoutWatcher = indexed(); withoutWatcher.watchers = []
        await world.set(withoutWatcher)
        noWatcher.confirmAddSilos(imageVision: false); await wait(noWatcher)
        expect(noWatcher.problem?.title.contains("watcher setup") == true, "Watcher setup requires observed running watchers, not just exit zero")
        for badSnapshot in [indexed(failures: 3), Snapshot(), Snapshot(silos: [silo], activityUnreadable: true)] {
            try Data().write(to: root.appendingPathComponent("calls.txt"))
            await world.set(Snapshot())
            let store = LibrarianStore(runtime: runtime(), autoRefresh: false)
            await store.refresh()
            store.addSilo(at: folder)
            await world.set(badSnapshot)
            store.confirmAddSilos(imageVision: false)
            await wait(store)
            expect(store.problem != nil, "Exit zero cannot hide missing index data, unresolved failures, or unreadable history")
            expect(store.jobs.count == 1, "Unverified indexing must stop before watcher setup")
        }

        // A launch failure must call completion and terminate the workflow.
        var broken = cfg; broken.python = root.path + "/missing-python"
        let launchFailure = LibrarianStore(runtime: runtime(broken), autoRefresh: false)
        launchFailure.addSilo(at: folder)
        launchFailure.confirmAddSilos(imageVision: false)
        await wait(launchFailure)
        expect(launchFailure.jobs.count == 1 && launchFailure.jobs[0].exitCode == -1, "Missing executable must leave a finished, inspectable failed job")

        // Large output exercises the old pipe deadlock / lost-tail failure mode.
        try writeScript("for ((i=0; i<9000; i++)); do echo 'large diagnostic line from isolated scenario'; done\necho 'FINAL STDERR MARKER' >&2\nexit 1\n")
        await world.set(indexed())
        let noisy = LibrarianStore(runtime: runtime(), autoRefresh: false)
        await noisy.refresh(); noisy.reindex(silo); noisy.reindex(silo)
        expect(noisy.jobs.count == 1, "Repeated reindex clicks must not spawn duplicate writers")
        await wait(noisy)
        let noisyJob = noisy.jobs[0]
        expect(noisyJob.output.contains("FINAL STDERR MARKER"), "The final diagnostic must survive large output")
        expect(noisyJob.output.utf8.count <= 128_000, "UI output must be bounded")
        let fullLog = try Data(contentsOf: URL(fileURLWithPath: noisyJob.logPath!))
        expect(fullLog.count > 128_000, "Full output must be retained on disk")

        // Removal is available for existing folders, asks first, and verifies cleanup.
        try writeScript("echo \"$*\" >> calls.txt\nexit 0\n")
        await world.set(indexed())
        let removal = LibrarianStore(runtime: runtime(), autoRefresh: false)
        await removal.refresh()
        removal.selectedSilo = silo.slug
        removal.requestRemoval(silo)
        expect(removal.pendingRemoval?.slug == silo.slug && removal.jobs.isEmpty, "Requesting removal must not delete before confirmation")
        removal.pendingRemoval = nil
        expect(removal.jobs.isEmpty, "Cancelling removal must not run pal")
        await world.set(Snapshot())
        removal.removeSilo(silo)
        removal.removeSilo(silo)
        expect(removal.jobs.count == 1, "Repeated removal must not spawn duplicate jobs")
        await wait(removal)
        expect(removal.jobs[0].arguments == ["remove", silo.slug], "Removal must target the exact slug")
        expect(removal.selectedSilo == nil && removal.lastActionMessage?.contains("Original files were kept") == true, "Verified removal clears selection and confirms files were kept")
        expect(FileManager.default.fileExists(atPath: folder.appendingPathComponent("note.txt").path), "Removing an index must preserve source files")
        for residual in [indexed(), Snapshot(watchers: indexed().watchers), Snapshot(registryError: "Unreadable registry"), Snapshot(unindexedBookmarks: [silo.path])] {
            await world.set(indexed())
            let incomplete = LibrarianStore(runtime: runtime(), autoRefresh: false)
            await incomplete.refresh()
            await world.set(residual)
            incomplete.removeSilo(silo); await wait(incomplete)
            expect(incomplete.jobs[0].outcome == "Removal could not be confirmed", "Remaining metadata or watchers must not report successful removal")
        }
        try writeScript("echo 'Removal incomplete: indexed chunks may remain' >&2\nexit 1\n")
        await world.set(indexed())
        let failedRemoval = LibrarianStore(runtime: runtime(), autoRefresh: false)
        await failedRemoval.refresh()
        await world.set(Snapshot())
        failedRemoval.removeSilo(silo); await wait(failedRemoval)
        expect(failedRemoval.problem?.silo?.slug == silo.slug && failedRemoval.problem?.logPath != nil, "Partial removal must preserve collection and log context after registry deletion")

        // A staged replacement never removes the original, even after success.
        try writeScript("echo \"$*\" >> calls.txt\nexit 0\n")
        try Data().write(to: root.appendingPathComponent("calls.txt"))
        let old = Silo(slug: "old", displayName: "Old", path: root.path + "/old-location", filesIndexed: 10, chunks: 100,
                       updated: Date(), imageVision: false, isPrivate: false, host: "test")
        let replacement = LibrarianStore(runtime: runtime(), autoRefresh: false)
        await world.set(indexed()); await replacement.refresh()
        replacement.prepareReplacement(old, at: folder); await wait(replacement)
        let replacementCalls = try String(contentsOf: root.appendingPathComponent("calls.txt"), encoding: .utf8)
        expect(!replacementCalls.contains("remove"),
               "Replacement must never automatically remove the old index")
        let privateOld = Silo(slug: "private", displayName: "Private", path: old.path, filesIndexed: 10, chunks: 100,
                              updated: Date(), imageVision: false, isPrivate: true, host: "test")
        let privateMove = LibrarianStore(runtime: runtime(), autoRefresh: false)
        privateMove.prepareReplacement(privateOld, at: folder)
        expect(privateMove.jobs.isEmpty && privateMove.problem != nil, "Private relocation must not create a shared replacement")

        let stopped = ServiceStatus(kind: .mcp, label: "test.never-a-real-job", title: "Test MCP", subtitle: "Fixture",
                                    installed: true, loaded: true, pid: nil, lastExit: 1, probeOK: false,
                                    logPath: root.path + "/service.log", plistPath: root.path + "/service.plist")
        await world.set(Snapshot(services: [stopped]))
        let noStart = LibrarianStore(runtime: runtime(), autoRefresh: false)
        noStart.start(stopped); await wait(noStart)
        expect(noStart.problem?.service?.label == stopped.label && noStart.problem?.logPath == stopped.logPath,
               "Accepted start followed by failed health must retain service and log context")
        expect(noStart.lastActionMessage == nil, "Accepted launchctl command alone is not startup success")
        var running = stopped; running.pid = 123; running.probeOK = true
        await world.set(Snapshot(services: [running]))
        let started = LibrarianStore(runtime: runtime(), autoRefresh: false)
        started.start(stopped); await wait(started)
        expect(started.problem == nil && started.lastActionMessage?.contains("is running") == true, "Healthy follow-up confirms startup")
        let refused = LibrarianStore(runtime: runtime(commandResult: 5), autoRefresh: false)
        refused.restart(running); await wait(refused)
        expect(refused.problem?.message.contains("refused") == true, "Denied service command must explain its failure")
        expect(!LibrarianStore.explainLaunchctl("Input/output error").contains("was restarted"), "Error text must not invent a restart")
        await world.set(indexed(failures: 3)); await started.refresh()
        expect(started.collectionStatus(silo).state == .attention && started.collectionStatus(silo).text.contains("3 files"),
               "A running watcher cannot hide indexing failures in collection status")
        await world.set(Snapshot(silos: [silo], activityUnreadable: true)); await started.refresh()
        expect(started.collectionStatus(silo).state == .unknown, "Unreadable history must not show healthy collection status")
        var brief = running; brief.uptime = "20m"; brief.memoryMB = 9
        var long = running; long.uptime = "2h 3m"; long.memoryMB = 100
        expect([long, brief].sorted(using: KeyPathComparator(\.uptimeSeconds)).first?.uptime == "20m", "Uptime must sort by duration, not text")
        expect([brief, long].sorted(using: KeyPathComparator(\.sortMemory, order: .reverse)).first?.memoryMB == 100, "Memory descending must sort numerically")
        let earlier = CollectionTableRow(silo: old, status: "Watching")
        let later = CollectionTableRow(silo: silo, status: "2 files failing")
        expect([earlier, later].sorted(using: KeyPathComparator(\.chunks)).first?.id == silo.id, "Counts must sort numerically")
        expect([earlier, later].sorted(using: KeyPathComparator(\.chunks, order: .reverse)).first?.id == old.id, "Descending counts must reverse ordering")
        expect([earlier, later].sorted(using: KeyPathComparator(\.status)).first?.id == silo.id, "Status sorting must use displayed status")
        print("FlowScenarioTests: passed (temporary folders, real subprocesses, simulated services; no live index or launchd writes)")
    }
}

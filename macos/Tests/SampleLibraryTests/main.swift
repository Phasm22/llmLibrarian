import Foundation

func expect(_ value: @autoclosure () -> Bool, _ message: String) {
    if !value() { fatalError(message) }
}

// A sample flag alone must never replace normal app data.
unsetenv("LLMLIBRARIAN_SNAPSHOT_DIR")
setenv("LLMLIBRARIAN_SNAPSHOT_SAMPLE", "1", 1)
expect(!SampleLibrary.enabled, "Sample mode requires an explicit snapshot destination")
setenv("LLMLIBRARIAN_SNAPSHOT_DIR", "/tmp/sample-test", 1)
expect(SampleLibrary.enabled, "Both flags enable isolated snapshots")
expect(PalConfig.load() == SampleLibrary.config, "Sample mode must not load the user's daemon configuration")
expect(PalConfig.load().python == "/usr/bin/false", "Fixture config cannot execute pal")
let now = Date(timeIntervalSince1970: 1_800_000_000)
let snapshot = SampleLibrary.snapshot(now: now)
expect(snapshot.silos.count == 3 && snapshot.queries.count == 3, "Sample library is populated")
expect(snapshot.silos.allSatisfy { $0.path.hasPrefix(SampleLibrary.root + "/") }, "No personal paths")
expect(snapshot.services.allSatisfy { $0.label.hasPrefix("sample.") }, "No real launchd labels")
expect(snapshot.watchers.allSatisfy { $0.label.hasPrefix("sample.") }, "No real watcher labels")
expect(snapshot.queries.allSatisfy { $0.sources.reduce(0) { $0 + $1.chunks } == $0.chunks }, "Audit totals match sources")
expect(snapshot.silos.filter(\.isPrivate).count == 1, "Show explicit privacy state")
print("SampleLibraryTests: passed")

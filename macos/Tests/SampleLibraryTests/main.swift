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
for window in ActivityWindow.allCases {
    let activity = SampleLibrary.snapshot(now: now, window: window).activity
    expect(activity?.window == window, "Sample activity follows the selected window")
    expect(activity?.timeline.count == (window == .today ? Calendar.current.component(.hour, from: now) + 1 : window.days),
           "Sample timeline matches the window's buckets")
    expect(activity?.fullSearchComparison == true && activity?.searchCount ?? 0 > 0, "Sample activity is comparable")
    expect(activity?.timeline.reduce(0) { $0 + $1.searches } == activity?.searchCount, "Search chart matches the total")
    expect(activity?.timeline.reduce(0) { $0 + $1.added + $1.updated + $1.removed } == activity?.changeTotal,
           "Change chart matches the total")
    expect(activity?.changeSilos.reduce(0) { $0 + $1.total } == activity?.changeTotal, "Per-collection rows add up")
    let paths = (activity?.changeSilos.flatMap { $0.files.map(\.path) } ?? []) + (activity?.topDocuments.map(\.path) ?? [])
    expect(paths.allSatisfy { $0.hasPrefix(SampleLibrary.root + "/") }, "Sample activity has no personal paths")
}
print("SampleLibraryTests: passed")

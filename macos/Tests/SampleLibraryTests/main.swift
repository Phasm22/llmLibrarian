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
    guard let activity = SampleLibrary.snapshot(now: now, window: window).activity else { fatalError("sample activity") }
    expect(activity.window == window, "Sample activity follows the selected window")
    expect(activity.buckets.count == (window == .today ? Calendar.current.component(.hour, from: now) + 1 : window.days),
           "Sample buckets match the window")
    expect(activity.fullSearchComparison && activity.searches > 0, "Sample activity is comparable")
    expect(activity.buckets.reduce(0) { $0 + $1.searches } == activity.searches, "Search sparkline matches the total")
    expect(activity.buckets.reduce(0) { $0 + $1.changes } == activity.changes, "Change sparkline matches the total")
    // Every Overview number matches what its click-through lists.
    let searches = SampleLibrary.detail(.init(window: window, tab: .searches, silo: nil, noSourceOnly: false), now: now)
    let misses = SampleLibrary.detail(.init(window: window, tab: .searches, silo: nil, noSourceOnly: true), now: now)
    let changes = SampleLibrary.detail(.init(window: window, tab: .changes, silo: nil, noSourceOnly: false), now: now)
    expect(searches.searches.count == activity.searches && misses.searches.count == activity.noSource, "Search tiles match their rows")
    expect(changes.changes.count == activity.changes, "Change tile matches its rows")
    for silo in snapshot.silos {
        let rows = SampleLibrary.detail(.init(window: window, tab: .changes, silo: silo.slug, noSourceOnly: false), now: now)
        expect(rows.changes.count == activity.stats(silo.slug).changes, "Collection row matches its changes")
    }
    let paths = changes.changes.map(\.path) + searches.searches.flatMap { $0.hits.map(\.path) }
    expect(paths.allSatisfy { $0.hasPrefix(SampleLibrary.root + "/") }, "Sample activity has no personal paths")
}
print("SampleLibraryTests: passed")

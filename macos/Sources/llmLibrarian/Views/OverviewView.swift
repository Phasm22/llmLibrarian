import Charts
import SwiftUI

/// Answers four questions in priority order (docs/plans/overview-information-design.md):
/// does anything need me, is the library changing / being used, which collection
/// is busiest, how is each collection doing. Every number clicks through to the
/// Activity page, where the rows behind it live; nothing on this page is a list
/// of files or queries.
struct OverviewView: View {
    @EnvironmentObject private var store: LibrarianStore

    var body: some View {
        Page {
            header
            if store.config.isInstalled {
                AttentionStrip()
                TileRow(summary: store.activity)
                CollectionsTable(summary: store.activity)
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            Text(headline).zfont(.title2, weight: .semibold).lineLimit(1)
            Spacer()
            if store.isRefreshing && store.hasLoaded { ProgressView().controlSize(.small) }
            Picker("Range", selection: $store.activityWindow) {
                ForEach(ActivityWindow.allCases) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
        }
    }

    private var headline: String {
        guard store.config.isInstalled else { return store.overallHeadline }
        guard let s = store.activity else { return store.activityUnreadable ? "Activity history needs an update" : "No activity history yet" }
        return s.headline(store.silos.map { ($0.slug, $0.displayName) })
    }
}

// MARK: attention — one row, only when something needs action

private struct AttentionStrip: View {
    @EnvironmentObject private var store: LibrarianStore

    var body: some View {
        let issues = store.issues
        let failures = store.activity?.failures ?? []
        let count = issues.count + failures.count
        if count > 0 {
            let lead: (title: String, detail: String, state: HealthState) =
                issues.first.map { ($0.title, $0.detail, $0.severity) }
                ?? failures.first.map { g in
                    ("\(g.files) file\(g.files == 1 ? "" : "s") in \(store.silo(slug: g.slug)?.displayName ?? g.slug) failed to index",
                     ActivityFailureGroup.reason(g.errorClass), .attention)
                }!
            Button { store.showActivity(.problems) } label: {
                HStack(spacing: 10) {
                    StatusDot(state: lead.state == .stopped ? .stopped : .attention, size: 9)
                    Text(count == 1 ? "1 thing needs you" : "\(count) things need you").zfont(.callout, weight: .semibold)
                    Text(lead.title).zfont(.callout).lineLimit(1)
                    Text(lead.detail).zfont(.callout).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                    Spacer()
                    Text("Review").zfont(.callout).foregroundStyle(Color.accentColor)
                    Image(systemName: "chevron.right").zfont(.caption).foregroundStyle(.secondary)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 9)
                .background(RoundedRectangle(cornerRadius: 8).fill(Color.orange.opacity(0.10)))
                .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(Color.orange.opacity(0.25)))
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help("Open Problems")
        }
    }
}

// MARK: three numbers

private struct TileRow: View {
    @EnvironmentObject private var store: LibrarianStore
    let summary: ActivitySummary?

    var body: some View {
        HStack(spacing: 12) {
            if let s = summary {
                ActivityTile(title: "Documents changed", value: s.changes, previous: s.previousChanges,
                             since: s.filesSince, window: s.window, series: s.buckets.map(\.changes), tint: .blue) {
                    store.showActivity(.changes)
                }
                ActivityTile(title: "Searches", value: s.searches, previous: s.previousSearches,
                             since: s.searchesSince, window: s.window, series: s.buckets.map(\.searches), tint: Theme.accent) {
                    store.showActivity(.searches)
                }
                ActivityTile(title: "Found no sources", value: s.noSource, previous: s.previousNoSource,
                             since: s.searchesSince, window: s.window, series: s.buckets.map(\.noSource), tint: .orange,
                             share: s.searches > 0 ? Double(s.noSource) / Double(s.searches) : nil, lowerIsBetter: true) {
                    store.showActivity(.searches, noSourceOnly: true)
                }
            } else {
                GroupBox {
                    VStack(alignment: .leading, spacing: 3) {
                        if store.activityUnreadable {
                            Text("The activity ledger was written by an older build and can't be read yet.")
                                .zfont(.callout)
                            Text("Run pal activity once (or restart the MCP server) to update it; no history is lost.")
                                .zfont(.callout).foregroundStyle(.secondary)
                        } else {
                            Text("Changes and searches appear here once the activity ledger has history.")
                                .zfont(.callout)
                            Text("Run pal activity backfill to import past searches from the query audit.")
                                .zfont(.callout).foregroundStyle(.secondary)
                        }
                    }
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .padding(4)
                }
            }
        }
    }
}

private struct ActivityTile: View {
    let title: String
    let value: Int
    let previous: Int?
    let since: Date?
    let window: ActivityWindow
    let series: [Int]
    let tint: Color
    var share: Double? = nil
    var lowerIsBetter = false
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            GroupBox {
                VStack(alignment: .leading, spacing: 6) {
                    Text(title).zfont(.caption).foregroundStyle(.secondary)
                    HStack(alignment: .lastTextBaseline, spacing: 6) {
                        Text(value.grouped).zfont(size: 26, weight: .semibold).monospacedDigit()
                        if let share {
                            Text(share.formatted(.percent.precision(.fractionLength(0))))
                                .zfont(.callout).foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 8)
                        Sparkline(values: series, tint: tint).frame(width: 96, height: 28)
                    }
                    delta
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(2)
                .contentShape(Rectangle())
            }
        }
        .buttonStyle(.plain)
        .help("Show the \(title.lowercased()) behind this number")
    }

    @ViewBuilder private var delta: some View {
        if let previous {
            let diff = value - previous
            let good = lowerIsBetter ? diff < 0 : diff > 0
            HStack(spacing: 3) {
                Image(systemName: diff > 0 ? "arrow.up.right" : diff < 0 ? "arrow.down.right" : "equal")
                    .foregroundStyle(diff == 0 ? Color.secondary : good ? Color.green : Color.orange)
                Text(deltaText(diff: diff, previous: previous))
            }
            .zfont(.caption).foregroundStyle(.secondary)
        } else {
            Text(since.map { "History since \($0.formatted(.dateTime.month(.abbreviated).day()))" } ?? "No history yet")
                .zfont(.caption).foregroundStyle(.tertiary)
        }
    }

    private func deltaText(diff: Int, previous: Int) -> String {
        if diff == 0 { return "Same as \(window.previous)" }
        if previous == 0 { return "Up from 0 \(window.previous)" }
        let pct = Double(abs(diff)) / Double(previous)
        return "\(pct.formatted(.percent.precision(.fractionLength(0)))) vs \(window.previous) (\(previous.grouped))"
    }
}

/// Bars without axes: shape over the window, not values.
private struct Sparkline: View {
    let values: [Int]
    let tint: Color
    var body: some View {
        Chart(Array(values.enumerated()), id: \.offset) { i, v in
            BarMark(x: .value("Bucket", i), y: .value("Count", v))
                .foregroundStyle(tint.opacity(0.75))
                .cornerRadius(1.5)
        }
        .chartXAxis(.hidden)
        .chartYAxis(.hidden)
        .chartYScale(domain: 0...max(1, values.max() ?? 1))
        .accessibilityHidden(true)
    }
}

// MARK: collections — every silo, searched or not, one row each

private struct CollectionsTable: View {
    @EnvironmentObject private var store: LibrarianStore
    let summary: ActivitySummary?

    var body: some View {
        let rows = ordered
        Section("Collections", detail: "\(rows.count)") {
            Grid(alignment: .leading, horizontalSpacing: 18, verticalSpacing: 0) {
                GridRow {
                    Text("Collection").frame(maxWidth: .infinity, alignment: .leading)
                    Text("Changed").gridColumnAlignment(.trailing)
                    Text("Searches").gridColumnAlignment(.trailing)
                    Text("Last activity")
                    Text("Status")
                }
                .zfont(.caption).foregroundStyle(.secondary)
                .padding(.bottom, 5)
                Divider().gridCellUnsizedAxes(.horizontal)
                ForEach(rows) { silo in
                    row(silo)
                    if silo.id != rows.last?.id { Divider().gridCellUnsizedAxes(.horizontal).opacity(0.5) }
                }
            }
        }
    }

    private var ordered: [Silo] {
        guard let s = summary else { return store.silos.sorted { $0.displayName.lowercased() < $1.displayName.lowercased() } }
        let order = s.ranked(store.silos.map { ($0.slug, $0.displayName) }).map(\.slug)
        let bySlug = Dictionary(uniqueKeysWithValues: store.silos.map { ($0.slug, $0) })
        return order.compactMap { bySlug[$0] }
    }

    private func row(_ silo: Silo) -> some View {
        let stats = summary?.stats(silo.slug) ?? ActivityCollectionStats()
        let status = status(silo, stats: stats)
        return GridRow {
            HStack(spacing: 5) {
                Text(silo.displayName).lineLimit(1)
                if silo.isPrivate {
                    Image(systemName: "lock.fill").zfont(.caption).foregroundStyle(.secondary)
                        .help("Private: excluded from unscoped retrieval")
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            count(stats.changes)
            count(stats.searches)
            Text(stats.lastActivity.map { Dates.relativeString($0) } ?? "Never").foregroundStyle(.secondary)
            HStack(spacing: 6) {
                StatusDot(state: status.state, size: 7)
                Text(status.text).foregroundStyle(status.state == .running ? .secondary : .primary).lineLimit(1)
            }
        }
        .zfont(.callout)
        .padding(.vertical, 6)
        .contentShape(Rectangle())
        .onTapGesture { store.showActivity(stats.changes > 0 || stats.searches == 0 ? .changes : .searches, silo: silo.slug) }
        .help("Show \(silo.displayName)'s activity")
        .contextMenu {
            Button("Show Changes") { store.showActivity(.changes, silo: silo.slug) }
            Button("Show Searches") { store.showActivity(.searches, silo: silo.slug) }
            Button("Show in Silos") { store.show(silo: silo) }
        }
    }

    private func count(_ n: Int) -> some View {
        Text(n == 0 ? "—" : n.grouped).monospacedDigit().foregroundStyle(n == 0 ? .tertiary : .primary)
    }

    private func status(_ silo: Silo, stats: ActivityCollectionStats) -> (state: HealthState, text: String) {
        if !silo.pathExists { return (.attention, "Folder missing") }
        if stats.openFailures > 0 { return (.attention, "\(stats.openFailures) failing") }
        guard let watcher = store.watcher(for: silo) else { return (.unknown, "Not watched") }
        if watcher.state != .running { return (.attention, "Watcher \(watcher.state == .stopped ? "stopped" : "needs attention")") }
        return (.running, "Watching")
    }
}

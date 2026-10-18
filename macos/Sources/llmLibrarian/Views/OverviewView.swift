import Charts
import SwiftUI

/// The story of the library over a window: what changed, what was searched,
/// and what needs attention, compared with the previous period. Numbers come
/// from the activity ledger (see ActivityLedger.swift); service health stays in
/// the header and the Attention list.
struct OverviewView: View {
    @EnvironmentObject private var store: LibrarianStore

    var body: some View {
        Page {
            header
            if store.config.isInstalled {
                if let a = store.activity {
                    SummaryCard(activity: a)
                    HStack(alignment: .top, spacing: 16) {
                        ChangesChart(activity: a)
                        SearchesChart(activity: a)
                    }
                    HStack(alignment: .top, spacing: 16) {
                        ChangesSection(activity: a)
                        UseSection(activity: a)
                    }
                    AttentionSection(activity: a)
                    HealthSection(activity: a)
                } else {
                    noHistory
                    if !store.issues.isEmpty { AttentionSection(activity: nil) }
                    HealthSection(activity: nil)
                }
                connectSection
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            StatusDot(state: store.overall, size: 10)
            Text(store.overallHeadline).zfont(.title2, weight: .semibold)
            Text(store.overallDetail).zfont(.callout).foregroundStyle(.secondary).lineLimit(1)
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

    private var noHistory: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 4) {
                Text("No activity history yet").zfont(.headline)
                Text("Changes and searches are recorded in \((ActivityLedger.path(dbPath: store.config.dbPath) as NSString).abbreviatingWithTildeInPath) once the MCP server and watchers run a build that writes it. Run pal activity backfill to import past searches from the query audit.")
                    .zfont(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var connectSection: some View {
        Section("Connect") {
            VStack(alignment: .leading, spacing: 4) {
                CopyableValueRow(key: "MCP endpoint", value: store.config.mcpURL) {
                    store.copy(store.config.mcpURL, note: "Copied MCP endpoint")
                }
                if let lite = store.mcpHealth?.litePath {
                    CopyableValueRow(key: "Lite endpoint", value: store.config.mcpURL(path: lite)) {
                        store.copy(store.config.mcpURL(path: lite), note: "Copied lite MCP endpoint")
                    }
                } else if let lite = store.config.mcpLitePath {
                    KeyValueRow(key: "Lite endpoint", value: "\(lite) is configured but not being served")
                        .foregroundStyle(.secondary)
                }
                KeyValueRow(key: "Index", value: (store.config.dbPath as NSString).abbreviatingWithTildeInPath, mono: true)
                KeyValueRow(key: "Checkout", value: (store.config.workdir as NSString).abbreviatingWithTildeInPath, mono: true)
                KeyValueRow(key: "Logs", value: (store.config.logsDir as NSString).abbreviatingWithTildeInPath, mono: true)
                if let v = store.mcpHealth?.version {
                    KeyValueRow(key: "Server", value: "llmLibrarian-mcp \(v), up since \(Dates.absoluteString(store.mcpHealth?.startedAt))")
                }
            }
        }
    }
}

// MARK: helpers

private extension LibrarianStore {
    func name(_ slug: String?) -> String {
        guard let slug else { return "All collections" }
        return silo(slug: slug)?.displayName ?? slug
    }
}

private func counted(_ n: Int, _ noun: String, _ plural: String? = nil) -> String {
    "\(n.grouped) \(n == 1 ? noun : plural ?? noun + "s")"
}

/// "+12 vs 8" style comparison; nil when the previous period isn't covered.
private struct Delta: View {
    let current: Int
    let previous: Int?
    var body: some View {
        if let previous {
            let diff = current - previous
            HStack(spacing: 2) {
                Image(systemName: diff > 0 ? "arrow.up.right" : diff < 0 ? "arrow.down.right" : "equal")
                Text("\(previous.grouped) before")
            }
            .zfont(.caption)
            .foregroundStyle(.secondary)
            .help(diff == 0 ? "Same as the previous period" : "\(diff > 0 ? "+" : "")\(diff) vs the previous period")
        }
    }
}

private struct Metric: View {
    let title: String
    let value: Int
    var previous: Int? = nil
    var body: some View {
        VStack(alignment: .leading, spacing: 1) {
            Text(title).zfont(.caption).foregroundStyle(.secondary)
            Text(value.grouped).zfont(.title3, weight: .semibold).monospacedDigit()
            Delta(current: value, previous: previous)
        }
    }
}

private func changeCounts(added: Int, updated: Int, removed: Int) -> some View {
    HStack(spacing: 8) {
        Text("+\(added)").foregroundStyle(.green)
        Text("~\(updated)").foregroundStyle(.blue)
        Text("−\(removed)").foregroundStyle(.red)
    }
    .monospacedDigit()
}

// MARK: summary

private struct SummaryCard: View {
    @EnvironmentObject private var store: LibrarianStore
    let activity: ActivityOverview

    var body: some View {
        let s = activity.summary { store.name($0) }
        GroupBox {
            VStack(alignment: .leading, spacing: 4) {
                Text(s.headline).zfont(.title3, weight: .semibold)
                Text(s.sentences.joined(separator: " ")).zfont(.callout).foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                if let note = historyNote {
                    Text(note).zfont(.caption).foregroundStyle(.tertiary)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(2)
        }
    }

    private var historyNote: String? {
        var parts: [String] = []
        if !activity.fullFileComparison, let d = activity.filesSince {
            parts.append("Document-change history available since \(d.formatted(date: .abbreviated, time: .omitted))")
        }
        if !activity.fullSearchComparison, let d = activity.searchesSince {
            parts.append("search history since \(d.formatted(date: .abbreviated, time: .omitted))")
        }
        guard !parts.isEmpty else { return nil }
        return parts.joined(separator: "; ") + ", so some comparisons are omitted."
    }
}

// MARK: charts — changes and retrieval stay on separate charts on purpose

private struct ChangesChart: View {
    let activity: ActivityOverview
    var body: some View {
        Section("Document changes", detail: activity.window == .today ? "by hour" : "by day") {
            Chart {
                ForEach(activity.timeline) { b in
                    BarMark(x: .value("Time", b.start, unit: unit), y: .value("Files", b.added))
                        .foregroundStyle(by: .value("Change", "Added"))
                    BarMark(x: .value("Time", b.start, unit: unit), y: .value("Files", b.updated))
                        .foregroundStyle(by: .value("Change", "Updated"))
                    BarMark(x: .value("Time", b.start, unit: unit), y: .value("Files", b.removed))
                        .foregroundStyle(by: .value("Change", "Removed"))
                }
            }
            .chartForegroundStyleScale(["Added": Color.green, "Updated": Color.blue, "Removed": Color.red])
            .chartLegend(position: .bottom, alignment: .leading)
            .frame(height: 130)
        }
    }
    private var unit: Calendar.Component { activity.window == .today ? .hour : .day }
}

private struct SearchesChart: View {
    let activity: ActivityOverview
    var body: some View {
        Section("Searches", detail: activity.window == .today ? "by hour" : "by day") {
            Chart {
                ForEach(activity.timeline) { b in
                    BarMark(x: .value("Time", b.start, unit: unit), y: .value("Searches", b.searches - b.withoutSources))
                        .foregroundStyle(by: .value("Result", "Sources found"))
                    BarMark(x: .value("Time", b.start, unit: unit), y: .value("Searches", b.withoutSources))
                        .foregroundStyle(by: .value("Result", "No sources"))
                }
            }
            .chartForegroundStyleScale(["Sources found": Theme.accent, "No sources": Color.orange.opacity(0.55)])
            .chartLegend(position: .bottom, alignment: .leading)
            .frame(height: 130)
        }
    }
    private var unit: Calendar.Component { activity.window == .today ? .hour : .day }
}

// MARK: library changes

private struct ChangesSection: View {
    @EnvironmentObject private var store: LibrarianStore
    let activity: ActivityOverview

    var body: some View {
        let t = activity.changeTotals
        Section("Library changes", detail: "files added, updated, or removed") {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 18) {
                    Metric(title: "Files changed", value: activity.changeTotal,
                           previous: activity.fullFileComparison ? activity.previousChangeTotal : nil)
                    VStack(alignment: .leading, spacing: 1) {
                        Text("Breakdown").zfont(.caption).foregroundStyle(.secondary)
                        changeCounts(added: t.added, updated: t.updated, removed: t.removed).zfont(.title3, weight: .semibold)
                    }
                }
                if activity.changeSilos.isEmpty && activity.collections.isEmpty {
                    Text(activity.fullFileComparison || activity.filesSince.map { $0 <= activity.periodStart } == true
                         ? "No documents changed \(activity.window.span)."
                         : "No document changes recorded yet; history began \(Dates.relativeString(activity.filesSince)).")
                        .zfont(.callout).foregroundStyle(.secondary)
                } else {
                    Divider()
                    ForEach(activity.changeSilos) { row in
                        DisclosureGroup {
                            VStack(alignment: .leading, spacing: 2) {
                                ForEach(row.files, id: \.self) { f in
                                    HStack(spacing: 6) {
                                        Image(systemName: f.kind == "added" ? "plus.circle" : f.kind == "removed" ? "minus.circle" : "pencil.circle")
                                            .foregroundStyle(f.kind == "added" ? .green : f.kind == "removed" ? .red : .blue)
                                        Text(f.name).lineLimit(1).truncationMode(.middle).help(f.path)
                                        Spacer()
                                        Text(Dates.relativeString(f.at)).foregroundStyle(.secondary)
                                    }
                                }
                                if row.total > row.files.count {
                                    Text("\(row.total - row.files.count) more").foregroundStyle(.tertiary)
                                }
                            }
                            .zfont(.callout)
                            .padding(.leading, 4)
                        } label: {
                            HStack {
                                Text(store.name(row.slug)).lineLimit(1)
                                Spacer()
                                changeCounts(added: row.added, updated: row.updated, removed: row.removed)
                                if activity.fullFileComparison {
                                    Text("\(row.previousTotal) before").foregroundStyle(.tertiary).frame(minWidth: 64, alignment: .trailing)
                                }
                            }
                            .zfont(.callout)
                        }
                    }
                    ForEach(activity.collections, id: \.self) { ev in
                        Label("\(store.name(ev.slug)) \(ev.added ? "added" : "removed") · \(counted(ev.files, "file"))",
                              systemImage: ev.added ? "folder.badge.plus" : "folder.badge.minus")
                            .zfont(.callout).foregroundStyle(.secondary)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity)
    }
}

// MARK: library use

private struct UseSection: View {
    @EnvironmentObject private var store: LibrarianStore
    let activity: ActivityOverview

    var body: some View {
        Section("Library use", trailing: {
            Button("Show all") { store.page = .activity }.buttonStyle(.link).zfont(.callout)
        }) {
            VStack(alignment: .leading, spacing: 8) {
                HStack(alignment: .top, spacing: 18) {
                    Metric(title: "Searches", value: activity.searchCount,
                           previous: activity.fullSearchComparison ? activity.previousSearches : nil)
                    Metric(title: "Sources found", value: activity.withSources)
                    Metric(title: "No sources", value: activity.withoutSources)
                }
                if activity.searches.isEmpty {
                    Text("Nothing was searched \(activity.window.span).").zfont(.callout).foregroundStyle(.secondary)
                } else {
                    Divider()
                    Text("Most-used collections").zfont(.caption).foregroundStyle(.secondary)
                    ForEach(activity.useSilos.prefix(5)) { row in
                        DisclosureGroup {
                            let rows = activity.searches.filter { $0.silo == row.slug }.prefix(8)
                            VStack(alignment: .leading, spacing: 2) {
                                if rows.isEmpty {
                                    Text("Surfaced by searches across all collections.").foregroundStyle(.secondary)
                                }
                                ForEach(Array(rows)) { s in SearchRow(search: s) }
                            }
                            .zfont(.callout)
                            .padding(.leading, 4)
                        } label: {
                            HStack {
                                Text(store.name(row.slug)).lineLimit(1)
                                Spacer()
                                Text("\(counted(row.searches, "search", "searches")) · \(counted(row.uniqueFiles, "file"))").foregroundStyle(.secondary)
                            }
                            .zfont(.callout)
                        }
                    }
                    if !activity.topDocuments.isEmpty {
                        Text("Documents surfaced most often").zfont(.caption).foregroundStyle(.secondary).padding(.top, 4)
                        ForEach(activity.topDocuments.prefix(5)) { d in
                            HStack {
                                Text(d.name).lineLimit(1).truncationMode(.middle).help(d.path)
                                Text(store.name(d.slug)).foregroundStyle(.tertiary).lineLimit(1)
                                Spacer()
                                Text(counted(d.searches, "search", "searches")).foregroundStyle(.secondary)
                            }
                            .zfont(.callout)
                        }
                    }
                    if !activity.clients.isEmpty {
                        Text("Assistants").zfont(.caption).foregroundStyle(.secondary).padding(.top, 4)
                        Text(activity.clients.prefix(6).map { "\($0.name) \($0.searches.grouped)" }.joined(separator: " · "))
                            .zfont(.callout)
                    }
                }
            }
        }
        .frame(maxWidth: .infinity)
    }
}

private struct SearchRow: View {
    @EnvironmentObject private var store: LibrarianStore
    let search: ActivitySearch
    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: search.sourcesFound ? "doc.text.magnifyingglass" : "questionmark.circle")
                .foregroundStyle(search.sourcesFound ? Color.secondary : Color.orange)
            Text(search.query ?? "(query text not kept)").lineLimit(1)
            Spacer()
            Text(search.client).foregroundStyle(.tertiary).lineLimit(1)
            Text(Dates.relativeString(search.at)).foregroundStyle(.secondary)
        }
    }
}

// MARK: attention

private struct AttentionSection: View {
    @EnvironmentObject private var store: LibrarianStore
    let activity: ActivityOverview?

    var body: some View {
        let failures = activity?.failureGroups ?? []
        let misses = activity?.noSourceGroups ?? []
        let issues = store.issues
        let count = issues.count + failures.count + misses.count
        Section("Attention needed", detail: count == 0 ? nil : "\(count)") {
            VStack(alignment: .leading, spacing: 6) {
                if count == 0 {
                    Label("Nothing needs attention.", systemImage: "checkmark.circle")
                        .zfont(.callout).foregroundStyle(.secondary)
                }
                ForEach(issues) { issue in IssueRow(issue: issue) }
                ForEach(failures) { g in
                    DisclosureGroup {
                        VStack(alignment: .leading, spacing: 2) {
                            if !g.sampleError.isEmpty {
                                Text(g.sampleError).foregroundStyle(.secondary).lineLimit(2)
                            }
                            ForEach(g.paths, id: \.self) { p in
                                Text((p as NSString).lastPathComponent).lineLimit(1).truncationMode(.middle).help(p)
                            }
                            if g.files > g.paths.count { Text("\(g.files - g.paths.count) more").foregroundStyle(.tertiary) }
                        }
                        .zfont(.callout)
                        .padding(.leading, 4)
                    } label: {
                        HStack(spacing: 10) {
                            StatusDot(state: .attention)
                            Text("\(g.files) file\(g.files == 1 ? "" : "s") in \(store.name(g.slug)) failed to index").lineLimit(1)
                            Text(Self.reason(g.errorClass)).foregroundStyle(.secondary).lineLimit(1)
                            Spacer()
                            if g.occurrences > g.files {
                                Text(counted(g.occurrences, "attempt")).foregroundStyle(.tertiary)
                            }
                            Text(Dates.relativeString(g.lastSeen)).foregroundStyle(.secondary)
                        }
                        .zfont(.callout)
                    }
                }
                ForEach(misses) { g in
                    DisclosureGroup {
                        let rows = (activity?.searches ?? []).filter { g.searchIDs.contains($0.id) }.prefix(8)
                        VStack(alignment: .leading, spacing: 2) {
                            ForEach(Array(rows)) { s in SearchRow(search: s) }
                        }
                        .zfont(.callout)
                        .padding(.leading, 4)
                    } label: {
                        HStack(spacing: 10) {
                            StatusDot(state: .unknown)
                            Text("No sources for “\(g.query)”").lineLimit(1)
                            Text(store.name(g.silo)).foregroundStyle(.secondary).lineLimit(1)
                            Spacer()
                            if g.count > 1 { Text("×\(g.count)").foregroundStyle(.secondary).monospacedDigit() }
                            Text(Dates.relativeString(g.lastSeen)).foregroundStyle(.secondary)
                        }
                        .zfont(.callout)
                    }
                }
            }
        }
    }

    static func reason(_ errorClass: String) -> String {
        switch errorClass {
        case "encrypted": return "encrypted or password-protected"
        case "too_large": return "over the size limit"
        case "unsupported": return "unsupported format"
        case "timeout": return "timed out"
        case "permission": return "permission denied"
        case "no_text": return "no extractable text"
        case "busy": return "index was busy"
        case "not_found": return "file not found"
        case "other": return "other error"
        default: return errorClass
        }
    }
}

private struct IssueRow: View {
    @EnvironmentObject private var store: LibrarianStore
    let issue: Issue

    var body: some View {
        HStack(spacing: 10) {
            StatusDot(state: issue.severity)
            Text(issue.title).zfont(.callout).lineLimit(1)
            Text(issue.detail).zfont(.callout).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
            Spacer()
            actions
        }
        .controlSize(.small)
        .contentShape(Rectangle())
        .onTapGesture(count: 2) { store.show(issue: issue) }
    }

    @ViewBuilder
    private var actions: some View {
        let service = store.service(label: issue.serviceLabel)
        let silo = store.silo(slug: issue.siloSlug)
        if let silo, !silo.pathExists {
            SiloFixButtons(silo: silo)
        } else if let service {
            if service.state == .stopped, service.installed {
                Button("Start") { store.start(service) }
            } else if service.loaded {
                Button("Restart") { store.restart(service) }
            }
            Button("Log") { store.openLog(service.logPath) }
        }
        Button("Show") { store.show(issue: issue) }
    }
}

// MARK: collection health — every silo, searched or not

private struct HealthSection: View {
    @EnvironmentObject private var store: LibrarianStore
    let activity: ActivityOverview?

    var body: some View {
        Section("Collection health", trailing: {
            Button("Show all") { store.page = .silos }.buttonStyle(.link).zfont(.callout)
        }) {
            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 0) {
                GridRow {
                    Text("").frame(width: 8)
                    ForEach(["Collection", "Files", "Last change", "Last search", "Watcher", "Failing"], id: \.self) { h in
                        Text(h).zfont(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(.bottom, 4)
                Divider().gridCellUnsizedAxes(.horizontal)
                ForEach(store.silos) { silo in
                    let watcher = store.watcher(for: silo)
                    let failing = activity?.openFailures[silo.slug] ?? 0
                    GridRow {
                        StatusDot(state: state(silo, watcher: watcher, failing: failing), size: 7)
                        HStack(spacing: 4) {
                            Text(silo.displayName).lineLimit(1)
                            if silo.isPrivate {
                                Image(systemName: "lock.fill").foregroundStyle(.secondary).help("Private: excluded from unscoped retrieval")
                            }
                        }
                        Text(silo.filesIndexed.grouped).monospacedDigit()
                        Text(activity.map { Dates.relativeString($0.lastChange[silo.slug]) } ?? "—").foregroundStyle(.secondary)
                        Text(activity.map { Dates.relativeString($0.lastSearch[silo.slug]) } ?? "—").foregroundStyle(.secondary)
                        Text(watcher.map(\.stateText) ?? "None").foregroundStyle(.secondary).lineLimit(1)
                        Text(failing > 0 ? failing.grouped : "—").monospacedDigit()
                            .foregroundStyle(failing > 0 ? Color.orange : Color.secondary)
                    }
                    .zfont(.callout)
                    .padding(.vertical, 3)
                    .contentShape(Rectangle())
                    .onTapGesture(count: 2) { store.show(silo: silo) }
                }
            }
        }
    }

    private func state(_ silo: Silo, watcher: ServiceStatus?, failing: Int) -> HealthState {
        if !silo.pathExists { return .attention }
        if let watcher, watcher.state != .running { return .attention }
        if failing > 0 { return .attention }
        return .running
    }
}

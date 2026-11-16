import SwiftUI

/// The rows behind the Overview's numbers: searches, document changes, and
/// problems for the selected window, optionally narrowed to one collection.
/// Reads the activity ledger on demand (see ActivityLedger detail reads).
struct ActivityView: View {
    @EnvironmentObject private var store: LibrarianStore
    @State private var detail = ActivityDetail()
    @State private var loadedRequest: ActivityDetailRequest?
    @State private var search = ""
    @State private var failureLimit = 200
    @State private var searchSort = [KeyPathComparator(\CollectionActivityRow<ActivitySearchRow>.record.at, order: .reverse)]
    @State private var changeSort = [KeyPathComparator(\CollectionActivityRow<ActivityChangeRow>.record.at, order: .reverse)]

    private var request: ActivityDetailRequest {
        ActivityDetailRequest(window: store.activityWindow, tab: store.activityTab,
                              silo: store.activitySilo, noSourceOnly: store.activityTab == .searches && store.activityNoSourceOnly,
                              failureLimit: failureLimit)
    }

    var body: some View {
        VStack(spacing: 0) {
            filterBar
            Divider()
            content
        }
        .searchable(text: $search, placement: .toolbar, prompt: store.activityTab == .searches ? "Filter queries" : "Filter files or errors")
        .task(id: TaskKey(request: request, refreshed: store.lastRefresh)) { await load() }
        .onChange(of: store.activitySilo) { _, _ in search = ""; failureLimit = 200 }
        .onChange(of: store.activityTab) { _, _ in search = "" }
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Button { store.showInspector.toggle() } label: { Label("Details", systemImage: "info.circle") }
                    .disabled(store.activityTab == .problems)
                    .help("Show or hide details (⌥⌘I)")
                    .labelStyle(ToolbarActionLabelStyle(showsTitle: store.showToolbarLabels))
            }
        }
        .inspector(isPresented: inspectorShown) { inspector }
        .onChange(of: store.selectedSearch) { _, id in
            store.activitySelectionText = id.flatMap { id in detail.searches.first { $0.id == id } }?.query
        }
        .onChange(of: store.selectedChange) { _, id in
            store.activitySelectionText = id.flatMap { id in detail.changes.first { $0.id == id } }?.path
        }
        .inspectorColumnWidth(min: 260, ideal: 340, max: 460)
    }

    private struct TaskKey: Hashable { let request: ActivityDetailRequest; let refreshed: Date? }

    private func load() async {
        let cfg = store.config, r = request
        let d = await Task.detached(priority: .userInitiated) { Collector.activityDetail(config: cfg, request: r) }.value
        guard !Task.isCancelled, r == request else { return }
        detail = d
        loadedRequest = r
        if SnapshotMode.directory != nil, store.selectedSearch == nil { store.selectedSearch = d.searches.first?.id }
    }

    // MARK: filters

    private var filterBar: some View {
        HStack(spacing: 12) {
            Picker("View", selection: $store.activityTab) {
                ForEach(ActivityTab.allCases) { tab in
                    Text(tab == .problems && store.attentionCount > 0 ? "Problems (\(store.attentionCount))" : tab.title).tag(tab)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            if store.activityTab != .problems {
                Picker("Range", selection: $store.activityWindow) {
                    ForEach(ActivityWindow.allCases) { Text($0.title).tag($0) }
                }
                .pickerStyle(.segmented)
                .labelsHidden()
                .fixedSize()
            }
            Picker("Collection", selection: $store.activitySilo) {
                Text("All collections").tag(String?.none)
                ForEach(store.silos) { s in Text(s.displayName).tag(Optional(s.slug)) }
            }
            .labelsHidden()
            .frame(maxWidth: 200)
            if store.activityTab == .searches {
                Toggle("Only searches with no sources", isOn: $store.activityNoSourceOnly)
                    .toggleStyle(.checkbox)
            }
            Spacer()
            if loadedRequest != request { ProgressView().controlSize(.small) }
        }
        .zfont(.callout)
        .padding(.horizontal, 14)
        .padding(.vertical, 8)
    }

    // MARK: content

    @ViewBuilder private var content: some View {
        if !detail.available && store.activityTab != .problems {
            EmptyState(symbol: "clock", title: "No activity history yet",
                       detail: "Searches and document changes are recorded in \((ActivityLedger.path(dbPath: store.config.dbPath) as NSString).abbreviatingWithTildeInPath). Run pal activity backfill to import past searches.")
        } else {
            switch store.activityTab {
            case .searches: searchesTable
            case .changes: changesTable
            case .problems:
                VStack(spacing: 0) {
                    if detail.failures.count >= failureLimit {
                        HStack {
                            Text("\(detail.failures.count) recent failures loaded; filter searches these rows.").font(.caption)
                            Button("Load More") { failureLimit += 200 }.disabled(loadedRequest != request)
                        }.padding(8)
                    }
                    ProblemsList(failures: detail.failures, filter: search, available: detail.available)
                }
            }
        }
    }

    private var searchRows: [CollectionActivityRow<ActivitySearchRow>] {
        let filtered = detail.searches.filter { q in
            search.isEmpty ||
            (q.query ?? "").localizedCaseInsensitiveContains(search) || q.client.localizedCaseInsensitiveContains(search)
                || q.hits.contains { $0.name.localizedCaseInsensitiveContains(search) }
        }
        return filtered.map { CollectionActivityRow(record: $0, collectionName: name($0.slug)) }.sorted(using: searchSort)
    }

    @ViewBuilder private var searchesTable: some View {
        if searchRows.isEmpty {
            EmptyState(symbol: "magnifyingglass", title: store.activityNoSourceOnly ? "Every search found sources" : "No searches",
                       detail: "Nothing matches \(filterDescription).")
        } else {
            Table(searchRows, selection: $store.selectedSearch, sortOrder: $searchSort) {
                TableColumn("When", value: \.record.at) { row in
                    let q = row.record
                    Text(q.at, format: .dateTime.month(.abbreviated).day().hour().minute()).foregroundStyle(.secondary) }
                    .width(min: 100, ideal: 120)
                TableColumn("Query", value: \.queryText) { row in
                    let q = row.record

                    Text(q.query ?? "(query text not kept)").lineLimit(1).help(q.query ?? "")
                        .foregroundStyle(q.query == nil ? .secondary : .primary)
                }
                .width(min: 220, ideal: 460)
                TableColumn("Collection", value: \.collectionName) { row in
                    let q = row.record
                    Text(name(q.slug)).foregroundStyle(q.slug == nil ? .secondary : .primary) }
                    .width(min: 90, ideal: 140)
                TableColumn("Sources", value: \.record.uniqueFiles) { row in
                    let q = row.record

                    Text(q.sourcesFound ? "\(q.uniqueFiles) file\(q.uniqueFiles == 1 ? "" : "s")" : "None")
                        .foregroundStyle(q.sourcesFound ? Color.secondary : Color.orange)
                }
                .width(min: 60, ideal: 80)
                TableColumn("Assistant", value: \.record.client) { row in
                    let q = row.record
                    Text(q.client).foregroundStyle(.secondary) }
                    .width(min: 80, ideal: 120)
            }
            .contextMenu(forSelectionType: Int.self) { ids in
                if let q = ids.first.flatMap({ id in detail.searches.first { $0.id == id } }), let text = q.query {
                    Button("Copy Query") { store.copy(text, note: "Copied query") }
                }
            }
        }
    }

    private var changeRows: [CollectionActivityRow<ActivityChangeRow>] {
        return detail.changes.filter { search.isEmpty || $0.path.localizedCaseInsensitiveContains(search) }
            .map { CollectionActivityRow(record: $0, collectionName: name($0.slug)) }.sorted(using: changeSort)
    }

    @ViewBuilder private var changesTable: some View {
        if changeRows.isEmpty {
            EmptyState(symbol: "doc.badge.clock", title: "No document changes",
                       detail: "Nothing changed for \(filterDescription). Changes are recorded as watchers and re-indexes update the manifest.")
        } else {
            Table(changeRows, selection: $store.selectedChange, sortOrder: $changeSort) {
                TableColumn("When", value: \.record.at) { row in
                    let c = row.record
                    Text(c.at, format: .dateTime.month(.abbreviated).day().hour().minute()).foregroundStyle(.secondary) }
                    .width(min: 100, ideal: 120)
                TableColumn("Change", value: \.record.kind) { row in
                    let c = row.record
                    ChangeKindLabel(kind: c.kind) }
                    .width(min: 70, ideal: 90)
                TableColumn("File", value: \.record.name) { row in
                    let c = row.record
                    Text(c.name).lineLimit(1).truncationMode(.middle).help(c.path) }
                    .width(min: 200, ideal: 420)
                TableColumn("Collection", value: \.collectionName) { row in
                    let c = row.record
                    Text(name(c.slug)) }
                    .width(min: 90, ideal: 140)
            }
            .contextMenu(forSelectionType: Int.self) { ids in
                if let c = ids.first.flatMap({ id in detail.changes.first { $0.id == id } }) {
                    Button("Reveal in Finder") { store.reveal(c.path) }
                        .disabled(!FileManager.default.fileExists(atPath: c.path))
                    Button("Copy Path") { store.copy(c.path, note: "Copied path") }
                }
            }
        }
    }

    private var filterDescription: String {
        let scope = store.activitySilo.map { name($0) } ?? "all collections"
        return store.activityTab == .problems ? scope : "\(scope), \(store.activityWindow.title.lowercased())"
    }

    private func name(_ slug: String?) -> String {
        guard let slug else { return "All collections" }
        return store.silo(slug: slug)?.displayName ?? slug
    }

    // MARK: inspector

    private var inspectorShown: Binding<Bool> {
        Binding(get: { store.showInspector && store.activityTab != .problems }, set: { store.showInspector = $0 })
    }

    @ViewBuilder private var inspector: some View {
        switch store.activityTab {
        case .searches:
            if let q = store.selectedSearch.flatMap({ id in detail.searches.first { $0.id == id } }) {
                SearchDetail(search: q)
            } else {
                SearchesOverview(documents: detail.documents, clients: detail.clients)
            }
        case .changes:
            if let c = store.selectedChange.flatMap({ id in detail.changes.first { $0.id == id } }) {
                ChangeDetail(change: c)
            } else {
                NothingSelected(text: "Select a change")
            }
        case .problems:
            EmptyView()
        }
    }
}

private struct ChangeKindLabel: View {
    let kind: String
    var body: some View {
        Label(kind.capitalized, systemImage: kind == "added" ? "plus.circle" : kind == "removed" ? "minus.circle" : "pencil.circle")
            .foregroundStyle(kind == "added" ? .green : kind == "removed" ? .red : .blue)
    }
}

/// Inspector when no search is selected: what the window's searches surfaced, and who asked.
private struct SearchesOverview: View {
    @EnvironmentObject private var store: LibrarianStore
    let documents: [ActivityDocumentRow]
    let clients: [(name: String, searches: Int)]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                InspectorHeading("Surfaced most often")
                if documents.isEmpty { Text("No sources surfaced.").zfont(.callout).foregroundStyle(.secondary) }
                ForEach(documents) { d in
                    HStack(alignment: .firstTextBaseline) {
                        VStack(alignment: .leading, spacing: 1) {
                            Text(d.name).lineLimit(1).truncationMode(.middle).help(d.path)
                            Text(store.silo(slug: d.slug)?.displayName ?? d.slug).zfont(.caption).foregroundStyle(.tertiary)
                        }
                        Spacer()
                        Text("\(d.searches)").monospacedDigit().foregroundStyle(.secondary)
                    }
                    .zfont(.callout)
                }
                InspectorHeading("Assistants")
                ForEach(clients, id: \.name) { c in
                    HStack {
                        Text(c.name)
                        Spacer()
                        Text("\(c.searches)").monospacedDigit().foregroundStyle(.secondary)
                    }
                    .zfont(.callout)
                }
                Text("Select a search for its sources.").zfont(.callout).foregroundStyle(.tertiary).padding(.top, 8)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

/// One search. When the query audit still holds the matching record, its richer
/// client attribution (app, session, transport) and chunk counts are shown.
private struct SearchDetail: View {
    @EnvironmentObject private var store: LibrarianStore
    let search: ActivitySearchRow

    private var auditRecord: QueryRecord? {
        guard let text = search.query else { return nil }
        return store.queries.first { r in
            guard let ts = r.timestamp, abs(ts.timeIntervalSince(search.at)) < 2 else { return false }
            return String(r.queries.joined(separator: " | ").prefix(500)) == text
        }
    }

    var body: some View {
        if let record = auditRecord {
            QueryDetail(record: record)
        } else {
            ScrollView {
                VStack(alignment: .leading, spacing: 10) {
                    Text(Dates.absoluteString(search.at)).zfont(.title3, weight: .semibold)
                    DetailRow("Collection", store.silo(slug: search.slug)?.displayName ?? search.slug ?? "All collections")
                    DetailRow("Assistant", search.client)
                    InspectorHeading("Query")
                    Text(search.query ?? "Query text was not kept (auditing is off).").zfont(.callout).textSelection(.enabled)
                    InspectorHeading("Sources (\(search.uniqueFiles))")
                    if !search.sourcesFound {
                        Text("No sources found.").zfont(.callout).foregroundStyle(.orange)
                    }
                    ForEach(search.hits, id: \.self) { h in
                        HStack(alignment: .firstTextBaseline) {
                            Text(h.name).zfont(.callout).lineLimit(1).truncationMode(.middle).help(h.path)
                            Spacer()
                            Button { store.reveal(h.path) } label: { Image(systemName: "magnifyingglass") }
                                .buttonStyle(.borderless)
                                .disabled(!FileManager.default.fileExists(atPath: h.path))
                                .help("Reveal in Finder")
                        }
                    }
                }
                .padding(14)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

private struct ChangeDetail: View {
    @EnvironmentObject private var store: LibrarianStore
    let change: ActivityChangeRow

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(change.name).zfont(.title3, weight: .semibold).lineLimit(2)
            ChangeKindLabel(kind: change.kind).zfont(.callout)
            DetailRow("When", Dates.absoluteString(change.at))
            DetailRow("Collection", store.silo(slug: change.slug)?.displayName ?? change.slug)
            Text(change.path).zfont(.callout).foregroundStyle(.secondary).textSelection(.enabled)
            HStack {
                Button("Reveal in Finder") { store.reveal(change.path) }
                    .disabled(!FileManager.default.fileExists(atPath: change.path))
                Button("Copy Path") { store.copy(change.path, note: "Copied path") }
            }
            .controlSize(.small)
            Spacer()
        }
        .padding(14)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// Everything that needs action: service, watcher and folder issues first, then
/// indexing failures grouped by collection and cause.
private struct ProblemsList: View {
    @EnvironmentObject private var store: LibrarianStore
    let failures: [ActivityFailureRow]
    let filter: String
    let available: Bool
    @State private var expandedGroups: Set<String> = []

    private var groups: [(key: String, slug: String, remedy: FailureRemedy, rows: [ActivityFailureRow])] {
        let grouped = Dictionary(grouping: failures.filter { $0.matches(filter) }) { $0.slug + "\u{0}" + $0.remedy.rawValue }
        return grouped.map { (key: $0.key, slug: $0.value[0].slug, remedy: $0.value[0].remedy, rows: $0.value) }
            .sorted { ($1.rows.count, $0.key) < ($0.rows.count, $1.key) }
    }

    private var issues: [Issue] {
        store.issues.filter { (store.activitySilo == nil || $0.siloSlug == store.activitySilo)
            && (filter.isEmpty || $0.title.localizedCaseInsensitiveContains(filter) || $0.detail.localizedCaseInsensitiveContains(filter)) }
    }

    var body: some View {
        if available && issues.isEmpty && groups.isEmpty && !filter.isEmpty {
            EmptyState(symbol: "magnifyingglass", title: "No matching problems", detail: "Try a file name, error message, or collection slug.")
        } else if available && issues.isEmpty && failures.isEmpty {
            EmptyState(symbol: "checkmark.circle", title: "Nothing needs attention",
                       detail: "No service issues or unresolved indexing failures are recorded for this selection.")
        } else {
            Page {
                if !available {
                    Warning("Indexing problems could not be read. Service issues below are still available; an unreadable history does not mean every file indexed.")
                    HStack {
                        Button("Open Services") { store.page = .services }
                        Button("Refresh") { Task { await store.refresh() } }
                    }
                }
                Text("Fix the cause, then reindex. Problems clear when indexing succeeds; repeated failures stay visible.")
                    .zfont(.callout).foregroundStyle(.secondary)
                if !issues.isEmpty {
                    Section("Services and folders", detail: "\(issues.count)") {
                        VStack(spacing: 0) {
                            ForEach(issues) { issue in
                                IssueRow(issue: issue)
                                if issue.id != issues.last?.id { Divider() }
                            }
                        }
                    }
                }
                ForEach(groups, id: \.key) { g in
                    let silo = store.silo(slug: g.slug)?.displayName ?? g.slug
                    Section(g.remedy.title, detail: "\(silo) · \(g.rows.count) files shown") {
                        VStack(alignment: .leading, spacing: 12) {
                            Text(g.remedy.nextStep).zfont(.callout).fixedSize(horizontal: false, vertical: true)
                                .frame(maxWidth: 780, alignment: .leading)
                            recoveryActions(slug: g.slug)
                            Divider()
                            ForEach(expandedGroups.contains(g.key) ? g.rows : Array(g.rows.prefix(5))) { f in
                                FailureFileRow(failure: f)
                            }
                            if g.rows.count > 5 {
                                Button(expandedGroups.contains(g.key) ? "Show fewer files" : "Show all \(g.rows.count) files") {
                                    if !expandedGroups.insert(g.key).inserted { expandedGroups.remove(g.key) }
                                }.buttonStyle(.link)
                            }
                        }
                    }
                }
            }
        }
    }

    @ViewBuilder private func recoveryActions(slug: String) -> some View {
        if let silo = store.silo(slug: slug) {
            HStack {
                if let watcher = store.watcher(for: silo) {
                    Button("Open Watcher Log") { store.openLog(watcher.logPath) }
                    if watcher.state == .running {
                        Button("Pause Watcher") { store.stop(watcher) }.disabled(SampleLibrary.enabled)
                    } else if watcher.installed {
                        Button("Resume Watcher") { store.start(watcher) }.disabled(SampleLibrary.enabled)
                    }
                }
                Button(store.activeJob(for: silo) == nil ? "Reindex Collection" : "Reindexing…") { store.reindex(silo) }
                    .disabled(!silo.pathExists || store.activeJob(for: silo) != nil || SampleLibrary.enabled)
                Button("Collection Details") { store.show(silo: silo) }
            }.controlSize(.small)
        } else {
            Button("Open Services") { store.page = .services }
        }
    }
}

private struct FailureFileRow: View {
    @EnvironmentObject private var store: LibrarianStore
    let failure: ActivityFailureRow

    var body: some View {
        DisclosureGroup {
            VStack(alignment: .leading, spacing: 8) {
                Text(failure.path).foregroundStyle(.secondary).textSelection(.enabled)
                Text(failure.error.isEmpty ? "No error detail was recorded. Check the watcher log." : failure.error)
                    .textSelection(.enabled)
                HStack {
                    Button("Reveal in Finder") { store.reveal(failure.path) }
                        .disabled(!FileManager.default.fileExists(atPath: failure.path))
                    Button("Copy Error Details") {
                        store.copy("Collection: \(failure.slug)\nFile: \(failure.path)\nError: \(failure.error)\nRecorded \(failure.occurrences) times\nLast seen: \(Dates.absoluteString(failure.lastSeen))",
                                   note: "Copied error details")
                    }
                }.controlSize(.small)
            }.padding(.vertical, 8)
        } label: {
            HStack {
                Text(failure.name).lineLimit(1).truncationMode(.middle).help(failure.path)
                Spacer()
                Text("Recorded \(failure.occurrences) times · \(Dates.relativeString(failure.lastSeen))")
                    .foregroundStyle(.secondary)
            }
        }.zfont(.callout)
    }
}

struct IssueRow: View {
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
        .padding(.vertical, 5)
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

struct QueryDetail: View {
    @EnvironmentObject private var store: LibrarianStore
    let record: QueryRecord

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                Text(Dates.absoluteString(record.timestamp)).zfont(.title3, weight: .semibold)
                DetailRow("Tool", record.tool)
                DetailRow("Collection", store.silo(slug: record.silo)?.displayName ?? record.silo ?? "All collections")
                DetailRow("Chunks", String(record.chunks))
                if record.errored { Warning("The retrieval reported errors.") }
                if record.truncated { Warning("The result was truncated.") }

                InspectorHeading("Client")
                if let c = record.client {
                    DetailRow("Submitted by", c.label)
                    if let name = c.name {
                        DetailRow("MCP client", [name, c.version].compactMap { $0 }.joined(separator: " "))
                    }
                    if let app = c.app {
                        DetailRow("App", c.pid.map { "\(app) · pid \($0)" } ?? app)
                    }
                    DetailRow("Transport", [c.transport, c.endpoint].compactMap { $0 }.joined(separator: " · "))
                    if let entry = c.entrypoint { DetailRow("Entrypoint", entry) }
                    if let agent = c.agent { DetailRow("Agent", agent) }
                    if let ua = c.userAgent { DetailRow("User-Agent", ua) }
                    if let session = c.session {
                        HStack(alignment: .firstTextBaseline) {
                            Text("Session").foregroundStyle(.secondary)
                            Spacer(minLength: 12)
                            Text(session).lineLimit(1).truncationMode(.middle).textSelection(.enabled)
                            Button { store.copy(session, note: "Copied session id") } label: { Image(systemName: "doc.on.doc") }
                                .buttonStyle(.borderless)
                                .help("Copy session id")
                        }
                        .zfont(.callout)
                    }
                } else {
                    Text("Not recorded — this query predates caller attribution.")
                        .zfont(.callout).foregroundStyle(.secondary)
                }

                InspectorHeading(record.queries.count == 1 ? "Query" : "Queries")
                ForEach(record.queries, id: \.self) { q in
                    Text(q).zfont(.callout).textSelection(.enabled)
                }

                InspectorHeading("Sources (\(record.sources.count))")
                if record.sources.isEmpty {
                    Text("No sources found.").zfont(.callout).foregroundStyle(.orange)
                }
                ForEach(Array(record.sources.enumerated()), id: \.offset) { _, s in
                    HStack(alignment: .firstTextBaseline) {
                        Text(s.file).zfont(.callout).lineLimit(1).truncationMode(.middle).help(s.path)
                        Spacer()
                        Text("×\(s.chunks)").zfont(.callout).monospacedDigit().foregroundStyle(.secondary)
                        Button { store.reveal(s.path) } label: { Image(systemName: "magnifyingglass") }
                            .buttonStyle(.borderless)
                            .disabled(s.path.isEmpty || !FileManager.default.fileExists(atPath: s.path))
                            .help("Reveal in Finder")
                    }
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

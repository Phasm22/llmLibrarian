import SwiftUI

struct ActivityView: View {
    @EnvironmentObject private var store: LibrarianStore
    @State private var siloFilter: String? = nil
    @State private var search = ""
    @SceneStorage("activityColumns") private var columns: TableColumnCustomization<QueryRecord>

    private var rows: [QueryRecord] {
        store.queries.filter { q in
            (siloFilter == nil || q.silo == siloFilter)
            && (search.isEmpty
                || q.queries.contains { $0.localizedCaseInsensitiveContains(search) }
                || q.sources.contains { $0.file.localizedCaseInsensitiveContains(search) })
        }
    }
    private var selected: QueryRecord? { store.selectedQuery.flatMap { id in store.queries.first { $0.id == id } } }

    var body: some View {
        Group {
            if store.queries.isEmpty {
                EmptyState(symbol: "clock",
                           title: "No queries yet",
                           detail: "Every MCP retrieval is appended to \((store.config.queryAuditPath as NSString).abbreviatingWithTildeInPath). Ask something through Claude or Cursor and it will appear here.")
            } else {
                Table(rows, selection: $store.selectedQuery, columnCustomization: $columns) {
                    TableColumn("When") { q in
                        Text(Dates.absoluteString(q.timestamp)).foregroundStyle(.secondary)
                    }
                    .width(min: 130, ideal: 150).customizationID("when")
                    TableColumn("Silo") { q in
                        Text(store.silo(slug: q.silo)?.displayName ?? q.silo ?? "All silos")
                    }
                    .width(min: 100, ideal: 140).customizationID("silo")
                    TableColumn("Query") { q in
                        Text(q.queries.joined(separator: "  |  ")).lineLimit(1).help(q.queries.joined(separator: "\n"))
                    }
                    .width(min: 240, ideal: 520).customizationID("query")
                    TableColumn("Tool") { q in
                        Text(q.tool == "multi_query_knowledge" ? "multi" : "query").foregroundStyle(.secondary)
                    }
                    .width(50).customizationID("tool")
                    TableColumn("Chunks") { q in Text(String(q.chunks)).monospacedDigit() }
                        .width(55).customizationID("chunks")
                    TableColumn("Sources") { q in Text(String(q.sources.count)).monospacedDigit() }
                        .width(60).customizationID("sources")
                    TableColumn("Flags") { q in
                        Text([q.errored ? "errors" : nil, q.truncated ? "truncated" : nil, q.chunks == 0 ? "empty" : nil]
                            .compactMap { $0 }.joined(separator: ", "))
                            .foregroundStyle(.secondary)
                    }
                    .width(min: 60, ideal: 110).customizationID("flags")
                }
                .contextMenu(forSelectionType: Int.self) { ids in
                    if let q = ids.first.flatMap({ id in store.queries.first { $0.id == id } }) {
                        Button("Copy Query") { store.copy(q.queries.joined(separator: "\n"), note: "Copied query") }
                        Button("Ask Claude") { store.selectedQuery = q.id; store.performService("Ask Claude") }
                        if let silo = store.silo(slug: q.silo) {
                            Button("Show Silo") { store.show(silo: silo) }
                        }
                    }
                }
            }
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Filter queries")
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Picker("Silo", selection: $siloFilter) {
                    Text("All silos").tag(String?.none)
                    ForEach(store.silos) { s in Text(s.displayName).tag(Optional(s.slug)) }
                }
                .frame(minWidth: 140)
                .help("Only queries scoped to this silo")
                Button { store.showInspector.toggle() } label: { Label("Details", systemImage: "info.circle") }
                    .help("Show or hide details (⌥⌘I)")
            }
        }
        .inspector(isPresented: $store.showInspector) {
            if let q = selected {
                QueryDetail(record: q)
            } else {
                NothingSelected(text: "Select a query")
            }
        }
        .inspectorColumnWidth(min: 260, ideal: 340, max: 460)
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
                DetailRow("Silo", store.silo(slug: record.silo)?.displayName ?? record.silo ?? "All silos")
                DetailRow("Chunks", String(record.chunks))
                if record.errored { Warning("The retrieval reported errors.") }
                if record.truncated { Warning("The result was truncated.") }

                InspectorHeading(record.queries.count == 1 ? "Query" : "Queries")
                ForEach(record.queries, id: \.self) { q in
                    Text(q).zfont(.callout).textSelection(.enabled)
                }

                InspectorHeading("Sources (\(record.sources.count))")
                if record.sources.isEmpty {
                    Text("No chunks returned.").zfont(.callout).foregroundStyle(.secondary)
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

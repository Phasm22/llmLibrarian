import SwiftUI

struct SilosView: View {
    @EnvironmentObject private var store: LibrarianStore
    @State private var sortOrder = [KeyPathComparator(\CollectionTableRow.name)]
    @State private var search = ""
    @SceneStorage("silosColumns") private var columns: TableColumnCustomization<CollectionTableRow>

    private var rows: [CollectionTableRow] {
        let filtered = search.isEmpty ? store.silos
            : store.silos.filter { $0.displayName.localizedCaseInsensitiveContains(search) || $0.path.localizedCaseInsensitiveContains(search) }
        return filtered.map { CollectionTableRow(silo: $0, status: store.collectionStatus($0).text) }.sorted(using: sortOrder)
    }
    private var selected: Silo? { store.silo(slug: store.selectedSilo) }

    var body: some View {
        Group {
            if store.silos.isEmpty && store.hasLoaded {
                VStack(spacing: 14) {
                    EmptyState(symbol: "folder",
                               title: store.registryError != nil && FileManager.default.fileExists(atPath: store.config.registryPath) ? "Could not read the collection registry" : "No silos indexed",
                               detail: store.registryError ?? "Add a folder and llmLibrarian will index it and keep it in sync.")
                    Button("Add Silo…") { store.chooseSiloFolder() }
                        .buttonStyle(.borderedProminent)
                }
            } else {
                Table(rows, selection: $store.selectedSilo, sortOrder: $sortOrder, columnCustomization: $columns) {
                    TableColumn("Name", value: \.name) { row in
                        let silo = row.silo
                        HStack(spacing: 7) {
                            StatusDot(state: rowState(silo), size: 7)
                            Text(silo.displayName)
                            if silo.isPrivate {
                                Image(systemName: "lock.fill")
                                    .foregroundStyle(.secondary)
                                    .imageScale(.small)
                                    .accessibilityLabel("Private")
                                    .help("Private — excluded from unscoped queries. Only `--in \(silo.slug)` reaches it.")
                            }
                            if store.activeJob(for: silo) != nil { ProgressView().controlSize(.mini) }
                        }
                    }
                    .width(min: 140, ideal: 200).customizationID("name")
                    TableColumn("Folder", value: \.folder) { row in
                        let silo = row.silo
                        Text(silo.tildePath).foregroundStyle(.secondary).truncationMode(.middle)
                            .help(silo.pathExists ? silo.path : "Missing: \(silo.path)")
                    }
                    .width(min: 160, ideal: 300).customizationID("folder")
                    TableColumn("Status", value: \.status) { row in
                        Text(row.status) }
                        .width(min: 110, ideal: 150).customizationID("status")
                    TableColumn("Files", value: \.files) { row in
                        let silo = row.silo
                        Text(silo.filesIndexed.grouped).monospacedDigit()
                    }
                    .width(55).customizationID("files")
                    TableColumn("Chunks", value: \.chunks) { row in
                        let silo = row.silo
                        Text(silo.chunks.grouped).monospacedDigit()
                    }
                    .width(65).customizationID("chunks")
                    TableColumn("Updated", value: \.updated) { row in
                        let silo = row.silo
                        Text(Dates.relativeString(silo.updated)).foregroundStyle(.secondary)
                            .help(Dates.absoluteString(silo.updated))
                    }
                    .width(min: 80, ideal: 100).customizationID("updated")
                    TableColumn("Scope", value: \.scope) { row in
                        let silo = row.silo
                        Text(silo.isPrivate ? "Private" : "Shared")
                            .foregroundStyle(silo.isPrivate ? .primary : .secondary)
                            .help(silo.isPrivate
                                  ? "Skipped by unscoped MCP queries. A cloud model cannot pull from it without naming the slug."
                                  : "Reachable by any unscoped query, including from a cloud model over MCP.")
                    }
                    .width(min: 60, ideal: 75).customizationID("scope")
                }
                .contextMenu(forSelectionType: Silo.ID.self) { ids in
                    if let silo = ids.first.flatMap({ store.silo(slug: $0) }) {
                        Button("Reindex Now") { store.reindex(silo) }.disabled(!silo.pathExists)
                        Button("Reveal in Finder") { store.reveal(silo.path) }.disabled(!silo.pathExists)
                        Button("Copy Path") { store.copy(silo.path, note: "Copied path") }
                        Button("Ask Claude") { store.selectedSilo = silo.slug; store.performService("Ask Claude") }
                        Divider()
                        Button("Remove Collection…", role: .destructive) { store.requestRemoval(silo) }
                            .disabled(!store.canRemove(silo))
                        Button(silo.isPrivate ? "Make Shared…" : "Make Private") { store.setPrivate(silo, !silo.isPrivate) }
                        if !silo.imageVision {
                            Button("Enable Image Vision…") { store.enableImageVision(silo) }
                        }
                        if let w = store.watcher(for: silo) {
                            Divider()
                            Button("Show Watcher") { store.show(service: w) }
                        }
                    }
                } primaryAction: { ids in
                    if let silo = ids.first.flatMap({ store.silo(slug: $0) }) { store.show(silo: silo) }
                }
            }
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Filter silos")
        .onChange(of: search) { _, _ in
            if !rows.contains(where: { $0.id == store.selectedSilo }) { store.selectedSilo = nil }
        }
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Group {
                    Button { store.chooseSiloFolder() } label: { Label("Add Silo", systemImage: "plus") }
                        .help("Choose a folder to index and watch")
                    Button { if let s = selected { store.reindex(s) } } label: { Label("Reindex", systemImage: "arrow.clockwise") }
                        .help("Reindex the selected silo now")
                        .disabled(!(selected.map { $0.pathExists && store.activeJob(for: $0) == nil } ?? false))
                    Button { if let s = selected { store.reveal(s.path) } } label: { Label("Reveal in Finder", systemImage: "folder") }
                        .help("Reveal the selected silo's folder in Finder")
                        .disabled(!(selected?.pathExists ?? false))
                    Button { if let s = selected { store.requestRemoval(s) } } label: { Label("Remove Collection", systemImage: "trash") }
                        .help("Remove the selected collection's index and watcher; keep its files")
                        .disabled(!(selected.map { store.canRemove($0) } ?? false))
                    Button { store.showInspector.toggle() } label: { Label("Details", systemImage: "info.circle") }
                        .help("Show or hide details (⌥⌘I)")
                }.labelStyle(ToolbarActionLabelStyle(showsTitle: store.showToolbarLabels))
            }
        }
        .inspector(isPresented: $store.showInspector) {
            if let silo = selected {
                SiloDetail(silo: silo)
            } else {
                NothingSelected(text: "Select a silo")
            }
        }
        .inspectorColumnWidth(min: 260, ideal: 320, max: 420)
    }

    private func rowState(_ silo: Silo) -> HealthState {
        store.collectionStatus(silo).state
    }

}

struct SiloDetail: View {
    @EnvironmentObject private var store: LibrarianStore
    let silo: Silo

    private var watcher: ServiceStatus? { store.watcher(for: silo) }
    private var job: Job? { store.activeJob(for: silo) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    Text(silo.displayName).zfont(.title3, weight: .semibold)
                    Text(silo.slug).zfont(.caption).foregroundStyle(.tertiary).textSelection(.enabled)
                }

                if silo.pathExists {
                    HStack(spacing: 6) {
                        Button(job != nil ? "Reindexing…" : "Reindex") { store.reindex(silo) }.disabled(job != nil)
                        Button("Reveal") { store.reveal(silo.path) }
                    }
                    .controlSize(.small)
                } else {
                    Warning(silo.isPrivate
                            ? "The private folder is missing. Restore it to the original path to preserve its scope. Its existing index is kept."
                            : "The folder is missing. Locate a replacement, review its index, then remove this old collection if it is no longer needed.")
                    HStack(spacing: 6) { SiloFixButtons(silo: silo, compact: true) }.controlSize(.small)
                }

                InspectorHeading("Scope")
                HStack(spacing: 8) {
                    Image(systemName: silo.isPrivate ? "lock.fill" : "lock.open")
                        .foregroundStyle(.secondary)
                    Text(silo.isPrivate ? "Private — local only" : "Shared with unscoped queries")
                        .zfont(.callout)
                }
                Text(silo.isPrivate
                     ? "Unscoped queries skip this silo entirely. It is reachable only by naming the slug — `pal ask --in \(silo.slug)`, or silo=\(silo.slug) over MCP."
                     : "Any unscoped query can return chunks from this silo, including one issued by a cloud model over MCP.")
                    .zfont(.caption).foregroundStyle(.secondary)
                Button(silo.isPrivate ? "Make Shared…" : "Make Private") { store.setPrivate(silo, !silo.isPrivate) }
                    .controlSize(.small)

                InspectorHeading("Index")
                if let failures = store.activity?.stats(silo.slug).openFailures, failures > 0 {
                    Button("Review \(failures) indexing failures") { store.showActivity(.problems, silo: silo.slug) }
                        .controlSize(.small)
                }
                DetailRow("Files", silo.filesIndexed.grouped)
                DetailRow("Chunks", silo.chunks.grouped)
                DetailRow("Updated", Dates.absoluteString(silo.updated))
                DetailRow("Image vision", silo.imageVision ? "On" : "Off")
                if !silo.imageVision, silo.pathExists {
                    Button("Enable Image Vision…") { store.enableImageVision(silo) }
                        .controlSize(.small)
                        .help("Reindex images with multimodal summaries; requires the optional image dependencies and a vision-capable Ollama model")
                }
                if !silo.host.isEmpty { DetailRow("Indexed on", silo.host) }

                Button("Remove Collection…", role: .destructive) { store.requestRemoval(silo) }
                    .disabled(!store.canRemove(silo))
                    .controlSize(.small)
                Text("Removes the index and watcher. Original files are kept.")
                    .zfont(.caption).foregroundStyle(.secondary)

                InspectorHeading("Folder")
                Text(silo.path)
                    .zfont(.callout, design: .monospaced)
                    .textSelection(.enabled)

                InspectorHeading("Watcher")
                if let w = watcher {
                    HStack(spacing: 8) {
                        StatusDot(state: w.state)
                        Text(w.stateText).zfont(.callout)
                    }
                    if let pid = w.pid {
                        DetailRow("Process", "pid \(pid)" + (w.uptime.map { " · up \($0)" } ?? ""))
                    }
                    if let e = w.lastExit, e != 0 { DetailRow("Last exit code", String(e)) }
                    ForEach(w.recentChanges.prefix(3)) { WatchChangeRow(change: $0) }
                    HStack {
                        Button("Show service") { store.show(service: w) }
                        Button("Log") { store.openLog(w.logPath) }
                    }
                    .controlSize(.small)
                } else {
                    Text("Not watched. Changes are picked up only when you reindex.")
                        .zfont(.callout).foregroundStyle(.secondary)
                    Button("Set Up Watchers") { store.syncDaemon() }.controlSize(.small)
                }

                if let latest = store.latestJob(for: silo) {
                    InspectorHeading("Latest job")
                    Text(latest.isRunning ? "Running" : latest.outcome ?? "Finished").zfont(.caption)
                    JobOutput(job: latest)
                    Button("All Jobs and Logs") { store.showJobs = true }
                }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

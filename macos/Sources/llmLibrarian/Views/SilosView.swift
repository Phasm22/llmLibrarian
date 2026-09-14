import SwiftUI

struct SilosView: View {
    @EnvironmentObject private var store: LibrarianStore
    @State private var sortOrder = [KeyPathComparator(\Silo.displayName)]
    @State private var search = ""
    @SceneStorage("silosColumns") private var columns: TableColumnCustomization<Silo>

    private var rows: [Silo] {
        let filtered = search.isEmpty ? store.silos
            : store.silos.filter { $0.displayName.localizedCaseInsensitiveContains(search) || $0.path.localizedCaseInsensitiveContains(search) }
        return filtered.sorted(using: sortOrder)
    }
    private var selected: Silo? { store.silo(slug: store.selectedSilo) }

    var body: some View {
        Group {
            if store.silos.isEmpty && store.hasLoaded {
                EmptyState(symbol: "folder",
                           title: "No silos indexed",
                           detail: store.registryError ?? "Index a folder with `pal pull <folder>` and it will show up here.")
            } else {
                Table(rows, selection: $store.selectedSilo, sortOrder: $sortOrder, columnCustomization: $columns) {
                    TableColumn("Name", value: \.displayName) { silo in
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
                    TableColumn("Folder") { silo in
                        Text(silo.tildePath).foregroundStyle(.secondary).truncationMode(.middle)
                            .help(silo.pathExists ? silo.path : "Missing: \(silo.path)")
                    }
                    .width(min: 160, ideal: 300).customizationID("folder")
                    TableColumn("Status") { silo in Text(statusText(silo)) }
                        .width(min: 110, ideal: 150).customizationID("status")
                    TableColumn("Files", value: \.filesIndexed) { silo in
                        Text(silo.filesIndexed.grouped).monospacedDigit()
                    }
                    .width(55).customizationID("files")
                    TableColumn("Chunks", value: \.chunks) { silo in
                        Text(silo.chunks.grouped).monospacedDigit()
                    }
                    .width(65).customizationID("chunks")
                    TableColumn("Updated") { silo in
                        Text(Dates.relativeString(silo.updated)).foregroundStyle(.secondary)
                            .help(Dates.absoluteString(silo.updated))
                    }
                    .width(min: 80, ideal: 100).customizationID("updated")
                    TableColumn("Scope") { silo in
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
                        Button(silo.isPrivate ? "Make Shared…" : "Make Private") { store.setPrivate(silo, !silo.isPrivate) }
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
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Button { if let s = selected { store.reindex(s) } } label: { Label("Reindex", systemImage: "arrow.clockwise") }
                    .help("Reindex the selected silo now")
                    .disabled(!(selected.map { $0.pathExists && store.activeJob(for: $0) == nil } ?? false))
                Button { if let s = selected { store.reveal(s.path) } } label: { Label("Reveal in Finder", systemImage: "folder") }
                    .help("Reveal the selected silo's folder in Finder")
                    .disabled(!(selected?.pathExists ?? false))
                Button { store.showInspector.toggle() } label: { Label("Details", systemImage: "info.circle") }
                    .help("Show or hide details (⌥⌘I)")
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
        if !silo.pathExists { return .stopped }
        return store.watcher(for: silo)?.state ?? .unknown
    }

    private func statusText(_ silo: Silo) -> String {
        if !silo.pathExists { return "Folder missing" }
        guard let w = store.watcher(for: silo) else { return "Not watched" }
        switch w.state {
        case .running: return "Watching"
        case .stopped: return "Watcher stopped"
        case .attention: return "Watcher needs attention"
        case .unknown: return "Not watched"
        }
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
                    Warning("The folder is missing on disk. If it moved, point the silo at the new location; otherwise remove it.")
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
                DetailRow("Files", silo.filesIndexed.grouped)
                DetailRow("Chunks", silo.chunks.grouped)
                DetailRow("Updated", Dates.absoluteString(silo.updated))
                DetailRow("Image vision", silo.imageVision ? "On" : "Off")
                if !silo.host.isEmpty { DetailRow("Indexed on", silo.host) }

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
                    HStack {
                        Button("Show service") { store.show(service: w) }
                        Button("Log") { store.openLog(w.logPath) }
                    }
                    .controlSize(.small)
                } else {
                    Text("Not watched. Changes are picked up only when you reindex.")
                        .zfont(.callout).foregroundStyle(.secondary)
                }

                if let job { JobOutput(job: job) }
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

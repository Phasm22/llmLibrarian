import SwiftUI

struct ServicesView: View {
    @EnvironmentObject private var store: LibrarianStore
    @State private var search = ""
    @SceneStorage("servicesColumns") private var columns: TableColumnCustomization<ServiceStatus>
    @State private var confirmStop: ServiceStatus?

    private var rows: [ServiceStatus] {
        guard !search.isEmpty else { return store.allServices }
        return store.allServices.filter {
            $0.title.localizedCaseInsensitiveContains(search) || $0.subtitle.localizedCaseInsensitiveContains(search)
                || $0.label.localizedCaseInsensitiveContains(search)
        }
    }
    private var selected: ServiceStatus? { store.service(label: store.selectedService) }
    private var selectedFolderMissing: Bool { store.silo(slug: selected?.siloSlug).map { !$0.pathExists } ?? false }

    var body: some View {
        Group {
            if !store.config.isInstalled {
                EmptyState(symbol: "gearshape.2", title: "Not installed",
                           detail: "No pal installation found at \((store.config.palPath as NSString).abbreviatingWithTildeInPath).")
            } else {
                Table(rows, selection: $store.selectedService, columnCustomization: $columns) {
                    TableColumn("Service") { s in
                        HStack(spacing: 7) {
                            StatusDot(state: s.state, size: 7)
                            Text(s.title)
                        }
                    }
                    .width(min: 150, ideal: 200).customizationID("service")
                    TableColumn("Kind") { s in Text(s.kindText).foregroundStyle(.secondary) }
                        .width(min: 80, ideal: 90).customizationID("kind")
                    TableColumn("State") { s in Text(s.stateText) }
                        .width(min: 120, ideal: 170).customizationID("state")
                    TableColumn("PID") { s in Text(s.pid.map(String.init) ?? "—").monospacedDigit() }
                        .width(55).customizationID("pid")
                    TableColumn("Uptime") { s in Text(s.uptime ?? "—").monospacedDigit() }
                        .width(65).customizationID("uptime")
                    TableColumn("Memory") { s in Text(s.memoryText).monospacedDigit() }
                        .width(75).customizationID("memory")
                    TableColumn("Endpoint") { s in Text(s.endpointText).foregroundStyle(.secondary) }
                    .width(90).customizationID("endpoint")
                    TableColumn("Last exit") { s in
                        Text(s.lastExit.map(String.init) ?? "—").monospacedDigit().foregroundStyle(.secondary)
                    }
                    .width(60).customizationID("lastExit")
                    TableColumn("Address / folder") { s in
                        Text(s.subtitle).foregroundStyle(.secondary).truncationMode(.middle).help(s.subtitle)
                    }
                    .width(min: 180, ideal: 300).customizationID("address")
                }
                .contextMenu(forSelectionType: String.self) { ids in
                    if let s = ids.first.flatMap({ store.service(label: $0) }) {
                        actionButtons(for: s)
                        Divider()
                        Button("Open Log") { store.openLog(s.logPath) }
                        Button("Reveal launchd Plist") { store.reveal(s.plistPath) }
                        Button("Copy Label") { store.copy(s.label, note: "Copied \(s.label)") }
                        Divider()
                        Button("Ask Claude") { store.selectedService = s.label; store.performService("Ask Claude") }
                    }
                } primaryAction: { ids in
                    if let s = ids.first.flatMap({ store.service(label: $0) }) { store.show(service: s) }
                }
            }
        }
        .searchable(text: $search, placement: .toolbar, prompt: "Filter services")
        .toolbar {
            ToolbarItemGroup(placement: .primaryAction) {
                Button { if let s = selected { store.start(s) } } label: { Label("Start", systemImage: "play.fill") }
                    .help(selectedFolderMissing ? "This watcher's folder is missing; use Locate Folder or Remove Silo in the details" : "Start the selected service")
                    .disabled(!(selected.map { $0.state == .stopped && $0.installed } ?? false) || selectedFolderMissing)
                Button { if let s = selected { requestStop(s) } } label: { Label("Stop", systemImage: "stop.fill") }
                    .help("Stop the selected service")
                    .disabled(!(selected?.loaded ?? false))
                Button { if let s = selected { store.restart(s) } } label: { Label("Restart", systemImage: "arrow.clockwise") }
                    .help("Restart the selected service")
                    .disabled(!(selected?.loaded ?? false))
                Button { store.showInspector.toggle() } label: { Label("Details", systemImage: "info.circle") }
                    .help("Show or hide details (⌥⌘I)")
            }
        }
        .inspector(isPresented: $store.showInspector) {
            if let s = selected {
                ServiceDetail(service: s, requestStop: requestStop)
            } else {
                NothingSelected(text: "Select a service")
            }
        }
        .inspectorColumnWidth(min: 260, ideal: 320, max: 420)
        .confirmationDialog("Stop \(confirmStop?.title ?? "")?", isPresented: Binding(get: { confirmStop != nil }, set: { if !$0 { confirmStop = nil } }),
                            titleVisibility: .visible, presenting: confirmStop) { s in
            Button("Stop", role: .destructive) { store.stop(s) }
        } message: { s in
            Text(s.kind == .chroma
                 ? "The MCP server and every watcher depend on Chroma. Queries will fail until it is started again."
                 : "Claude and other MCP clients will lose the endpoint until it is started again.")
        }
    }

    private func requestStop(_ s: ServiceStatus) {
        if s.kind == .watcher { store.stop(s) } else { confirmStop = s }
    }

    @ViewBuilder
    private func actionButtons(for s: ServiceStatus) -> some View {
        if s.state == .stopped, s.installed { Button("Start") { store.start(s) } }
        if s.loaded {
            Button("Restart") { store.restart(s) }
            Button("Stop") { requestStop(s) }
        }
    }
}

struct ServiceDetail: View {
    @EnvironmentObject private var store: LibrarianStore
    let service: ServiceStatus
    let requestStop: (ServiceStatus) -> Void

    private var silo: Silo? { store.silo(slug: service.siloSlug) }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 10) {
                VStack(alignment: .leading, spacing: 2) {
                    HStack(spacing: 8) {
                        StatusDot(state: service.state)
                        Text(service.title).zfont(.title3, weight: .semibold)
                    }
                    Text(service.stateText).zfont(.callout).foregroundStyle(.secondary)
                }

                if let silo, !silo.pathExists {
                    Warning("Folder \(silo.tildePath) is missing, so this watcher exits immediately. Point it at the folder's new location or remove the silo.")
                    HStack(spacing: 6) {
                        SiloFixButtons(silo: silo, compact: true)
                        Button("Log") { store.openLog(service.logPath) }
                    }
                    .controlSize(.small)
                } else {
                    HStack(spacing: 6) {
                        if service.state == .stopped, service.installed { Button("Start") { store.start(service) } }
                        if service.loaded {
                            Button("Restart") { store.restart(service) }
                            Button("Stop") { requestStop(service) }
                        }
                        Button("Log") { store.openLog(service.logPath) }
                    }
                    .controlSize(.small)
                }

                if let d = service.probeDetail, service.probeOK == false {
                    Warning(d)
                }

                InspectorHeading("Process")
                DetailRow("Kind", service.kindText)
                DetailRow("PID", service.pid.map(String.init) ?? "—")
                DetailRow("Uptime", service.uptime ?? "—")
                DetailRow("Memory", service.memoryText)
                DetailRow("Last exit code", service.lastExit.map(String.init) ?? "—")
                DetailRow("launchd", service.loaded ? "Loaded" : (service.installed ? "Not loaded" : "No plist"))
                if service.kind != .watcher {
                    DetailRow("Endpoint", service.endpointText)
                }

                InspectorHeading(service.kind == .watcher ? "Folder" : "Address")
                Text(service.kind == .watcher ? (silo?.path ?? service.subtitle) : service.subtitle)
                    .zfont(.callout, design: .monospaced).textSelection(.enabled)
                if let silo {
                    DetailRow("Files", silo.filesIndexed.grouped)
                    DetailRow("Chunks", silo.chunks.grouped)
                    DetailRow("Last indexed", Dates.absoluteString(silo.updated))
                    HStack {
                        Button("Show silo") { store.show(silo: silo) }
                        Button("Reindex") { store.reindex(silo) }.disabled(!silo.pathExists || store.activeJob(for: silo) != nil)
                    }
                    .controlSize(.small)
                }

                InspectorHeading("Files")
                DetailRow("Label", service.label)
                HStack {
                    Button("Reveal log") { store.reveal(service.logPath) }
                    Button("Reveal plist") { store.reveal(service.plistPath) }
                        .disabled(!FileManager.default.fileExists(atPath: service.plistPath))
                }
                .controlSize(.small)
            }
            .padding(14)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

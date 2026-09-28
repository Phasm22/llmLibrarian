import SwiftUI

struct MainWindow: View {
    @EnvironmentObject private var store: LibrarianStore
    @Environment(\.openWindow) private var openWindow
    @State private var columns: NavigationSplitViewVisibility = .all

    private var pageBinding: Binding<SidebarItem?> {
        Binding(get: { store.page }, set: { if let v = $0 { store.page = v } })
    }

    var body: some View {
        NavigationSplitView(columnVisibility: $columns) {
            VStack(spacing: 0) {
                List(SidebarItem.allCases, selection: pageBinding) { item in
                    Label(item.title, systemImage: item.symbol)
                        .zfont(.body)
                        .badge(badge(for: item))
                        .tag(item)
                }
                .listStyle(.sidebar)
                sidebarFooter
            }
            .navigationSplitViewColumnWidth(min: 160, ideal: 180, max: 240)
        } detail: {
            Group {
                switch store.page {
                case .overview: OverviewView()
                case .silos: SilosView()
                case .services: ServicesView()
                case .activity: ActivityView()
                }
            }
            .background(Color(nsColor: .windowBackgroundColor))
            .navigationTitle("\(store.page.title) — llmLibrarian")
        }
        .toolbar {
            ToolbarItem(placement: .principal) { StatusStrip() }
        }
        .environment(\.zoom, store.zoom)
        .font(Font.scaled(.body, zoom: store.zoom))
        .onAppear {
            AppDelegate.openMainWindow = { openWindow(id: AppDelegate.mainWindowID) }
        }
        .task { await SnapshotMode.run(store: store) }
        .overlay(alignment: .bottom) { toast }
        .modifier(ProblemAlert())
    }

    private func badge(for item: SidebarItem) -> Int {
        switch item {
        case .services:
            return store.allServices.filter { $0.state == .stopped || $0.state == .attention }.count
        case .silos:
            return store.silos.filter { !$0.pathExists }.count
        default:
            return 0
        }
    }

    private var sidebarFooter: some View {
        VStack(alignment: .leading, spacing: 3) {
            Divider()
            HStack(spacing: 6) {
                StatusDot(state: store.overall, size: 7)
                Text(store.overallHeadline)
                    .zfont(.caption, weight: .medium)
                    .lineLimit(2)
            }
            .padding(.top, 8)
            Text(store.lastRefresh.map { "Updated \(Dates.relativeString($0))" } ?? "Loading…")
                .zfont(.caption2)
                .foregroundStyle(.tertiary)
        }
        .padding(.horizontal, 12)
        .padding(.bottom, 10)
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    @ViewBuilder
    private var toast: some View {
        if let msg = store.lastActionMessage {
            Text(msg)
                .zfont(.callout)
                .padding(.horizontal, 14)
                .padding(.vertical, 8)
                .background(.regularMaterial, in: Capsule())
                .overlay(Capsule().strokeBorder(Color(nsColor: .separatorColor)))
                .shadow(color: .black.opacity(0.12), radius: 8, y: 2)
                .padding(.bottom, 16)
                .transition(.move(edge: .bottom).combined(with: .opacity))
                .task(id: msg) {
                    try? await Task.sleep(for: .seconds(4))
                    if store.lastActionMessage == msg {
                        withAnimation { store.lastActionMessage = nil }
                    }
                }
        }
    }
}

/// Live status in the toolbar: core services and the watcher count.
struct StatusStrip: View {
    @EnvironmentObject private var store: LibrarianStore
    var body: some View {
        HStack(spacing: 16) {
            ForEach(store.services) { s in
                HStack(spacing: 5) {
                    StatusDot(state: s.state, size: 7)
                    Text(s.kind == .chroma ? "Chroma" : "MCP")
                }
                .help("\(s.title): \(s.stateText)")
                .onTapGesture { store.show(service: s) }
            }
            if !store.watchers.isEmpty {
                HStack(spacing: 5) {
                    StatusDot(state: store.watchersRunning == store.watchers.count ? .running : .attention, size: 7)
                    Text("Watchers \(store.watchersRunning) of \(store.watchers.count)")
                }
                .help("Folder watchers running")
                .onTapGesture { store.page = .services }
            }
            if store.jobs.contains(where: { $0.isRunning }) {
                HStack(spacing: 5) {
                    ProgressView().controlSize(.mini)
                    Text(store.jobs.first { $0.isRunning }?.title ?? "Working")
                }
            }
        }
        .zfont(.callout)
        .foregroundStyle(.secondary)
        .padding(.horizontal, 8)
    }
}

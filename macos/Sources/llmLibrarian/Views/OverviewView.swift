import SwiftUI

struct OverviewView: View {
    @EnvironmentObject private var store: LibrarianStore

    var body: some View {
        Page {
            header
            if store.config.isInstalled {
                stats
                if !store.issues.isEmpty { issuesSection }
                HStack(alignment: .top, spacing: 16) {
                    servicesSection
                    queriesSection
                }
                connectSection
            }
        }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline, spacing: 10) {
            StatusDot(state: store.overall, size: 10)
            Text(store.overallHeadline).zfont(.title2, weight: .semibold)
            Text(store.overallDetail).zfont(.callout).foregroundStyle(.secondary)
            Spacer()
            if store.isRefreshing && store.hasLoaded { ProgressView().controlSize(.small) }
        }
    }

    private var stats: some View {
        HStack(spacing: 10) {
            StatTile(title: "Silos", value: store.silos.count.grouped,
                     footnote: "\(store.silos.filter { !$0.pathExists }.count) with missing folders")
            StatTile(title: "Files", value: store.totalFiles.grouped, footnote: "in the manifest")
            StatTile(title: "Chunks", value: store.totalChunks.grouped, footnote: "in the vector store")
            StatTile(title: "Watchers", value: "\(store.watchersRunning) of \(store.watchers.count)",
                     footnote: "running", state: store.watchersRunning == store.watchers.count ? .running : .attention)
            StatTile(title: "Queries", value: store.queries.count.grouped,
                     footnote: store.queries.first.map { "last \(Dates.relativeString($0.timestamp))" } ?? "none recorded")
        }
    }

    private var issuesSection: some View {
        Section("Issues", detail: "\(store.issues.count)") {
            VStack(spacing: 0) {
                ForEach(Array(store.issues.enumerated()), id: \.element.id) { i, issue in
                    HStack(spacing: 10) {
                        StatusDot(state: issue.severity)
                        Text(issue.title).zfont(.callout).lineLimit(1)
                        Text(issue.detail).zfont(.callout).foregroundStyle(.secondary).lineLimit(1).truncationMode(.middle)
                        Spacer()
                        issueActions(issue)
                    }
                    .controlSize(.small)
                    .padding(.vertical, 5)
                    .contentShape(Rectangle())
                    .onTapGesture(count: 2) { store.show(issue: issue) }
                    if i < store.issues.count - 1 { Divider() }
                }
            }
        }
    }

    @ViewBuilder
    private func issueActions(_ issue: Issue) -> some View {
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

    private var servicesSection: some View {
        Section("Services", trailing: {
            Button("Show all") { store.page = .services }.buttonStyle(.link).zfont(.callout)
        }) {
            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 0) {
                GridRow {
                    Text("").frame(width: 8)
                    ForEach(["Service", "State", "PID", "Uptime", "Memory"], id: \.self) { h in
                        Text(h).zfont(.caption).foregroundStyle(.secondary)
                    }
                }
                .padding(.bottom, 4)
                Divider().gridCellUnsizedAxes(.horizontal)
                ForEach(store.allServices) { s in
                    GridRow {
                        StatusDot(state: s.state, size: 7)
                        Text(s.title).lineLimit(1)
                        Text(s.stateText).foregroundStyle(.secondary).lineLimit(1)
                        Text(s.pid.map(String.init) ?? "—").monospacedDigit()
                        Text(s.uptime ?? "—").monospacedDigit()
                        Text(s.memoryText).monospacedDigit()
                    }
                    .zfont(.callout)
                    .padding(.vertical, 3)
                    .contentShape(Rectangle())
                    .onTapGesture(count: 2) { store.show(service: s) }
                }
            }
        }
    }

    private var queriesSection: some View {
        Section("Recent queries", trailing: {
            Button("Show all") { store.page = .activity }.buttonStyle(.link).zfont(.callout)
        }) {
            if store.queries.isEmpty {
                Text("No MCP queries recorded yet.").zfont(.callout).foregroundStyle(.secondary)
                    .frame(maxWidth: .infinity, minHeight: 60)
            } else {
                Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 0) {
                    GridRow {
                        ForEach(["When", "Silo", "Query", "Chunks"], id: \.self) { h in
                            Text(h).zfont(.caption).foregroundStyle(.secondary)
                        }
                    }
                    .padding(.bottom, 4)
                    Divider().gridCellUnsizedAxes(.horizontal)
                    ForEach(store.queries.prefix(8)) { q in
                        GridRow {
                            Text(Dates.relativeString(q.timestamp)).foregroundStyle(.secondary).lineLimit(1)
                            Text(store.silo(slug: q.silo)?.displayName ?? q.silo ?? "All").lineLimit(1)
                            Text(q.queries.first ?? "—").lineLimit(1).frame(maxWidth: .infinity, alignment: .leading)
                            Text(String(q.chunks)).monospacedDigit()
                        }
                        .zfont(.callout)
                        .padding(.vertical, 3)
                        .contentShape(Rectangle())
                        .onTapGesture(count: 2) { store.selectedQuery = q.id; store.page = .activity; store.showInspector = true }
                    }
                }
            }
        }
    }

    private var connectSection: some View {
        Section("Connect") {
            VStack(alignment: .leading, spacing: 4) {
                HStack {
                    KeyValueRow(key: "MCP endpoint", value: store.config.mcpURL, mono: true)
                    Button("Copy") { store.copy(store.config.mcpURL, note: "Copied MCP endpoint") }.controlSize(.small)
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

import SwiftUI
import AppKit

struct AboutView: View {
    @EnvironmentObject private var store: LibrarianStore
    @State private var copied = false
    private let build = AppBuildInfo()

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            HStack(spacing: 16) {
                Image(nsImage: NSApplication.shared.applicationIconImage)
                    .resizable().frame(width: 72, height: 72)
                VStack(alignment: .leading, spacing: 4) {
                    Text("llmLibrarian").font(.title.bold())
                    Text("Version \(build.version) · Build \(build.build)").foregroundStyle(.secondary)
                    Text("Your folders, searchable by your assistants.").font(.callout)
                }
            }
            Text("Indexes local files and keeps them up to date. This app shows index health, search activity, and background services.")
                .font(.callout).foregroundStyle(.secondary)
            Divider()
            VStack(alignment: .leading, spacing: 12) {
                LabeledContent("Built") {
                    Text(build.date.map { $0.formatted(date: .abbreviated, time: .standard) } ?? "Not recorded")
                }
                LabeledContent("Commit") { Text(build.commit).monospaced().textSelection(.enabled) }
                LabeledContent("Branch") { Text(build.branch).textSelection(.enabled) }
                LabeledContent("Source state") { Text(build.sourceState) }
                VStack(alignment: .leading, spacing: 4) {
                    Text("Built from").foregroundStyle(.secondary)
                    Text(build.source ?? "Source checkout not recorded")
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
            }.font(.callout)
            Text("Build details describe this app. Python services load the checkout separately when they start.")
                .font(.caption).foregroundStyle(.secondary)
            Divider()
            HStack {
                Button(copied ? "Copied" : "Copy Build Details") {
                    store.copy(build.report, note: "Copied build details")
                    copied = true
                }
                Button("Open Build Log") { store.openLog(store.rebuildLogPath) }
                Spacer()
                Button(store.isRebuildingApp ? "Rebuilding…" : "Rebuild and Relaunch") { store.rebuildApp() }
                    .disabled(!store.canRebuildApp)
            }
        }
        .padding(24)
        .frame(width: 560)
        .background(Color(nsColor: .windowBackgroundColor))
    }
}

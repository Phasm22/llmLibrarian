import SwiftUI

struct JobsView: View {
    @EnvironmentObject private var store: LibrarianStore
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text("Recent Jobs").font(.title2.bold())
                Spacer()
                Button("Open Saved Logs") { store.openJobLogs() }
                Button("Done") { store.showJobs = false }.keyboardShortcut(.cancelAction)
            }
            Text("Jobs from this session. Full logs remain on disk after the app closes.")
                .font(.callout).foregroundStyle(.secondary)
            if store.jobs.isEmpty {
                EmptyState(symbol: "list.bullet.rectangle", title: "No jobs started in this session",
                           detail: "Indexing, privacy changes, and watcher setup will appear here. Logs from earlier sessions are available in Saved Logs.")
            } else {
                ScrollView { LazyVStack(spacing: 16) { ForEach(store.jobs) { job in JobCard(job: job) } } }
            }
        }.padding(20).frame(width: 720, height: 560)
            .background(Color(nsColor: .windowBackgroundColor))
    }
}

private struct JobCard: View {
    @EnvironmentObject private var store: LibrarianStore
    @ObservedObject var job: Job
    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Text(job.title).font(.headline)
                    Spacer()
                    if job.isRunning { ProgressView().controlSize(.small) }
                    Text(job.isRunning ? "Running" : job.outcome ?? "Finished").foregroundStyle(.secondary)
                }
                Text(job.startedAt, format: .dateTime.month(.abbreviated).day().hour().minute().second())
                    .font(.caption).foregroundStyle(.secondary)
                JobOutput(job: job)
                HStack {
                    if let log = job.logPath { Button("Open Full Log") { store.openLog(log) } }
                    Button("Copy Details") { store.copy("\(job.title)\n\(job.outcome ?? "Running")\n\(job.output)", note: "Copied job details") }
                    if let slug = job.siloSlug, store.silo(slug: slug) != nil {
                        Button("Review Problems") { store.showJobs = false; store.showActivity(.problems, silo: slug) }
                    }
                    Spacer()
                    if let target = job.removalTarget, job.outcome != "Finished" {
                        Button("Retry Removal…", role: .destructive) { store.requestRemoval(target) }
                            .disabled(!store.canRemove(target))
                    }
                    if job.arguments.first == "pull" || job.arguments == ["daemon", "sync"] {
                        Button("Retry") { store.retry(job) }.disabled(job.isRunning)
                    }
                }.controlSize(.small)
            }.padding(6)
        }
    }
}

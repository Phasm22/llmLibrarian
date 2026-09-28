import Foundation
import AppKit
import Combine
import SwiftUI

/// A long-running pal subprocess (reindex, daemon sync) with live output.
@MainActor
final class Job: ObservableObject, Identifiable {
    let id = UUID()
    let title: String
    let siloSlug: String?
    @Published var output = ""
    @Published var isRunning = true
    @Published var exitCode: Int32?
    let startedAt = Date()

    init(title: String, siloSlug: String?) {
        self.title = title
        self.siloSlug = siloSlug
    }
}

/// A failed or impossible action, explained with the fix attached.
struct ActionProblem: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    var service: ServiceStatus? = nil
    var silo: Silo? = nil
    var logPath: String? = nil
    /// True when the fix is to relocate or remove the silo whose folder is gone.
    var folderMissing: Bool { silo.map { !$0.pathExists } ?? false }
}

@MainActor
final class LibrarianStore: ObservableObject {
    // Data
    @Published private(set) var config = PalConfig.load()
    @Published private(set) var silos: [Silo] = []
    @Published private(set) var services: [ServiceStatus] = []
    @Published private(set) var watchers: [ServiceStatus] = []
    @Published private(set) var queries: [QueryRecord] = []
    @Published private(set) var mcpHealth: MCPHealth?
    @Published private(set) var unindexedBookmarks: [String] = []
    @Published private(set) var registryError: String?
    @Published private(set) var lastRefresh: Date?
    @Published private(set) var isRefreshing = false
    @Published private(set) var hasLoaded = false
    @Published var jobs: [Job] = []
    @Published var lastActionMessage: String?
    @Published var problem: ActionProblem?
    /// Set when the user asks to un-private a silo. Widening exposure is confirmed;
    /// narrowing it is not — the safe direction should not need a dialog.
    @Published var pendingUnprivate: Silo?

    // Navigation (single window, so it lives here; the View menu and the
    // menu bar item drive it too).
    @Published var page: SidebarItem = .overview
    @Published var selectedService: String?
    @Published var selectedSilo: String?
    @Published var selectedQuery: Int?
    @Published var showInspector = true
    @Published private(set) var zoomStep: Int = UserDefaults.standard.integer(forKey: "zoomStep")

    private var loopTask: Task<Void, Never>?
    private var pendingRefresh = false

    init() {
        loopTask = Task { [weak self] in
            while !Task.isCancelled {
                await self?.refresh()
                try? await Task.sleep(for: .seconds(12))
            }
        }
    }

    // MARK: zoom (⌘+ / ⌘− / ⌘0)

    static let zoomLevels: [CGFloat] = [0.8, 0.9, 1.0, 1.15, 1.3, 1.5, 1.75]
    private static let defaultZoomIndex = 2

    var zoom: CGFloat {
        let i = min(max(Self.defaultZoomIndex + zoomStep, 0), Self.zoomLevels.count - 1)
        return Self.zoomLevels[i]
    }
    var canZoomIn: Bool { Self.defaultZoomIndex + zoomStep < Self.zoomLevels.count - 1 }
    var canZoomOut: Bool { Self.defaultZoomIndex + zoomStep > 0 }

    func zoomIn() { if canZoomIn { setZoom(zoomStep + 1) } }
    func zoomOut() { if canZoomOut { setZoom(zoomStep - 1) } }
    func zoomReset() { setZoom(0) }
    private func setZoom(_ v: Int) {
        zoomStep = v
        UserDefaults.standard.set(v, forKey: "zoomStep")
    }

    // MARK: derived

    var allServices: [ServiceStatus] { services + watchers }
    var totalFiles: Int { silos.reduce(0) { $0 + $1.filesIndexed } }
    var totalChunks: Int { silos.reduce(0) { $0 + $1.chunks } }
    var watchersRunning: Int { watchers.filter { $0.state == .running }.count }

    var overall: HealthState {
        if !config.isInstalled { return .unknown }
        if services.contains(where: { $0.state == .stopped }) { return .stopped }
        if allServices.contains(where: { $0.state == .attention }) { return .attention }
        if watchers.contains(where: { $0.state == .stopped }) { return .attention }
        if silos.contains(where: { !$0.pathExists }) { return .attention }
        return .running
    }

    /// Specific, not "something needs attention".
    var overallHeadline: String {
        if !config.isInstalled { return "llmLibrarian is not installed" }
        let stoppedCore = services.filter { $0.state == .stopped }
        if stoppedCore.count == services.count, !services.isEmpty { return "Chroma and the MCP server are not running" }
        if let s = stoppedCore.first { return "\(s.title) is not running" }
        if let s = services.first(where: { $0.state == .attention }) { return "\(s.title) is up but not responding" }
        let down = watchers.count - watchersRunning
        if down > 0 { return "\(down) of \(watchers.count) watchers are not running" }
        let missing = silos.filter { !$0.pathExists }.count
        if missing > 0 { return missing == 1 ? "1 silo folder is missing" : "\(missing) silo folders are missing" }
        return "All services running"
    }

    var overallDetail: String {
        if !config.isInstalled {
            return "Run pal daemon install from a checkout to set up the services, then reopen this window."
        }
        var parts: [String] = []
        let core = services.filter { $0.state == .running }.count
        parts.append(core == services.count ? "Chroma and MCP up" : "\(core) of \(services.count) core services up")
        parts.append("\(watchersRunning) of \(watchers.count) watchers")
        parts.append("\(silos.count) silos, \(totalFiles.grouped) files, \(totalChunks.grouped) chunks")
        return parts.joined(separator: " · ")
    }

    var issues: [Issue] {
        var out: [Issue] = []
        for s in services where s.state != .running {
            var detail: [String] = []
            if !s.installed { detail.append("no launchd plist in ~/Library/LaunchAgents") }
            if let e = s.lastExit, e != 0 { detail.append("last exit code \(e)") }
            if s.probeOK == false, let d = s.probeDetail { detail.append(d) }
            out.append(Issue(id: s.label, severity: s.state == .stopped ? .stopped : .attention,
                             title: s.state == .stopped ? "\(s.title) is not running" : "\(s.title) is not responding",
                             detail: detail.isEmpty ? "Open the log for details" : detail.joined(separator: " · "),
                             page: .services, serviceLabel: s.label))
        }
        for w in watchers where w.state != .running {
            let silo = silo(slug: w.siloSlug)
            var detail: [String] = []
            if let silo, !silo.pathExists { detail.append("folder \(silo.tildePath) is missing") }
            if let e = w.lastExit, e != 0 { detail.append("last exit code \(e)") }
            if w.lockStale { detail.append("stale lock file") }
            if !w.installed { detail.append("no launchd plist") }
            out.append(Issue(id: w.label, severity: .attention,
                             title: "Watcher for \(w.title) is not running",
                             detail: detail.isEmpty ? "Open the log for details" : detail.joined(separator: " · "),
                             page: .services, serviceLabel: w.label, siloSlug: w.siloSlug))
        }
        for silo in silos where !silo.pathExists && watcher(for: silo) == nil {
            out.append(Issue(id: "silo-" + silo.slug, severity: .attention,
                             title: "Folder for \(silo.displayName) is missing",
                             detail: "\(silo.tildePath) · \(silo.chunks.grouped) chunks still indexed",
                             page: .silos, siloSlug: silo.slug))
        }
        if !unindexedBookmarks.isEmpty {
            out.append(Issue(id: "bookmarks", severity: .unknown,
                             title: "\(unindexedBookmarks.count) bookmarked folder\(unindexedBookmarks.count == 1 ? "" : "s") not indexed",
                             detail: unindexedBookmarks.map { ($0 as NSString).abbreviatingWithTildeInPath }.joined(separator: ", "),
                             page: .services))
        }
        return out
    }

    func watcher(for silo: Silo) -> ServiceStatus? { watchers.first { $0.siloSlug == silo.slug } }
    func silo(slug: String?) -> Silo? { slug.flatMap { s in silos.first { $0.slug == s } } }
    func service(label: String?) -> ServiceStatus? { label.flatMap { l in allServices.first { $0.label == l } } }
    func activeJob(for silo: Silo) -> Job? { jobs.first { $0.siloSlug == silo.slug && $0.isRunning } }

    /// The selected row as plain text, for the Services menu and copying.
    var selectionText: String? {
        switch page {
        case .silos:
            guard let s = silo(slug: selectedSilo) else { return nil }
            return "\(s.displayName) — \(s.path) (\(s.filesIndexed.grouped) files, \(s.chunks.grouped) chunks)"
        case .services, .overview:
            guard let s = service(label: selectedService) else { return nil }
            return "\(s.title) — \(s.stateText) — \(s.subtitle)"
        case .activity:
            guard let q = selectedQuery.flatMap({ id in queries.first { $0.id == id } }) else { return nil }
            return q.queries.joined(separator: "\n")
        }
    }

    /// Hand the selected row to a macOS Service by name (e.g. the "Ask Claude"
    /// quick action). Goes straight to NSPerformService, so it works even when
    /// the service's own Info.plist forgets to declare NSSendTypes.
    func performService(_ name: String) {
        guard let text = selectionText else { return }
        let pb = NSPasteboard(name: .init("llmLibrarianService"))
        pb.clearContents()
        pb.declareTypes([.string], owner: nil)
        pb.setString(text, forType: .string)
        if !NSPerformService(name, pb) {
            problem = ActionProblem(
                title: "Couldn't run “\(name)”",
                message: "macOS has no Service by that name, or it refused the request. Check System Settings → Keyboard → Keyboard Shortcuts → Services.")
        }
    }

    // MARK: navigation

    func show(service: ServiceStatus) {
        page = .services
        selectedService = service.label
        showInspector = true
    }

    func show(silo: Silo) {
        page = .silos
        selectedSilo = silo.slug
        showInspector = true
    }

    func show(issue: Issue) {
        page = issue.page
        if let l = issue.serviceLabel { selectedService = l }
        if let s = issue.siloSlug, issue.page == .silos { selectedSilo = s }
        showInspector = true
    }

    // MARK: refresh

    func refresh() async {
        if isRefreshing { pendingRefresh = true; return }
        isRefreshing = true
        config = PalConfig.load()
        let cfg = config
        let snap = await Task.detached(priority: .userInitiated) { await Collector.collect(config: cfg) }.value
        silos = snap.silos
        services = snap.services
        watchers = snap.watchers
        queries = snap.queries
        mcpHealth = snap.mcpHealth
        unindexedBookmarks = snap.unindexedBookmarks
        registryError = snap.registryError
        lastRefresh = Date()
        hasLoaded = true
        isRefreshing = false
        if SnapshotMode.directory != nil {
            if selectedSilo == nil { selectedSilo = silos.first?.slug }
            if selectedService == nil { selectedService = watchers.first { $0.state != .running }?.label ?? services.first?.label }
            if selectedQuery == nil { selectedQuery = queries.first?.id }
        }
        if pendingRefresh { pendingRefresh = false; await refresh() }
    }

    /// Refresh a couple of times after an action so launchd state catches up.
    func refreshSoon() {
        Task {
            try? await Task.sleep(for: .milliseconds(600))
            await refresh()
            try? await Task.sleep(for: .seconds(3))
            await refresh()
        }
    }

    // MARK: service actions

    func restart(_ s: ServiceStatus) {
        Task.detached { [weak self] in
            let r = Launchctl.kickstart(s.label)
            await self?.finishAction(r, success: "Restarted \(s.title)", failure: "Could not restart \(s.title)")
        }
    }

    func stop(_ s: ServiceStatus) {
        Task.detached { [weak self] in
            let r = Launchctl.bootout(s.label)
            await self?.finishAction(r, success: "Stopped \(s.title)", failure: "Could not stop \(s.title)")
        }
    }

    func start(_ s: ServiceStatus) {
        if let silo = silo(slug: s.siloSlug), !silo.pathExists {
            problem = ActionProblem(
                title: "The watcher for \(s.title) can't start",
                message: "Its folder \(silo.tildePath) no longer exists, so the watcher exits as soon as launchd starts it (last exit code \(s.lastExit ?? 1)). Point the silo at the folder's new location, or remove the silo.",
                service: s, silo: silo, logPath: s.logPath)
            return
        }
        guard s.installed else {
            problem = ActionProblem(title: "\(s.title) is not installed",
                                    message: "There is no launchd plist for \(s.label). Run `pal daemon sync` (Services → Sync with registry) to recreate watcher jobs, or `pal mcp install` / `pal chroma install` for the core services.",
                                    service: s)
            return
        }
        let plist = s.plistPath
        let loaded = s.loaded
        Task.detached { [weak self] in
            // A job launchd already knows about must be kickstarted; bootstrap
            // on a loaded job fails with "5: Input/output error".
            let r = loaded ? Launchctl.kickstart(s.label) : Launchctl.bootstrap(plist: plist)
            await self?.finishAction(r, service: s, success: "Started \(s.title)", failure: "Couldn't start \(s.title)")
        }
    }

    private func finishAction(_ r: Shell.Result, service: ServiceStatus? = nil, success: String, failure: String) {
        if r.ok {
            lastActionMessage = success
        } else {
            let raw = r.combined.isEmpty ? "launchctl exited with status \(r.status)." : r.combined
            problem = ActionProblem(title: failure, message: Self.explainLaunchctl(raw),
                                    service: service, silo: silo(slug: service?.siloSlug), logPath: service?.logPath)
        }
        refreshSoon()
    }

    /// Translate launchctl's terse errors into something a person can act on.
    static func explainLaunchctl(_ raw: String) -> String {
        if raw.contains("Input/output error") {
            return "launchd already has this job loaded, so it can't be bootstrapped again. It was restarted instead; if it is still down, the process is exiting right after launch. Open the log for the reason."
        }
        if raw.contains("Operation not permitted") || raw.contains("Permission denied") {
            return "launchd refused the request. \(raw)"
        }
        if raw.contains("No such process") || raw.contains("Could not find service") {
            return "launchd has no job with this label. Run `pal daemon sync` (Services → Sync with registry) to recreate it."
        }
        return raw
    }

    // MARK: fixing a silo whose folder moved

    /// Ask for the folder's new location, index it, then remove the old silo.
    func relocate(_ silo: Silo) {
        let panel = NSOpenPanel()
        panel.title = "Locate \(silo.displayName)"
        panel.message = "Choose where \(silo.tildePath) is now. It will be indexed as a new silo and the old one removed."
        panel.prompt = "Use This Folder"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        let parent = (silo.path as NSString).deletingLastPathComponent
        panel.directoryURL = URL(fileURLWithPath: FileManager.default.fileExists(atPath: parent) ? parent : NSHomeDirectory())
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        let newPath = url.path
        problem = nil
        runPal(title: "Index \(url.lastPathComponent)", siloSlug: silo.slug, args: ["pull", newPath]) { [weak self] code in
            guard code == 0 else { return }
            self?.runPal(title: "Remove old silo \(silo.displayName)", siloSlug: silo.slug, args: ["remove", silo.slug])
        }
    }

    func removeSilo(_ silo: Silo) {
        problem = nil
        runPal(title: "Remove silo \(silo.displayName)", siloSlug: silo.slug, args: ["remove", silo.slug])
    }

    // MARK: pal jobs

    func reindex(_ silo: Silo) {
        guard activeJob(for: silo) == nil else { return }
        runPal(title: "Reindex \(silo.displayName)", siloSlug: silo.slug, args: ["pull", silo.path])
    }

    /// Toggle a silo's private flag. Going private applies immediately; going
    /// shared routes through a confirmation, because it is the step that exposes
    /// a corpus to unscoped queries.
    func setPrivate(_ silo: Silo, _ makePrivate: Bool) {
        if makePrivate {
            applyPrivate(silo, true)
        } else {
            pendingUnprivate = silo
        }
    }

    func applyPrivate(_ silo: Silo, _ makePrivate: Bool) {
        pendingUnprivate = nil
        runPal(
            title: makePrivate ? "Make \(silo.displayName) private" : "Make \(silo.displayName) shared",
            siloSlug: silo.slug,
            args: makePrivate ? ["private", silo.slug] : ["private", silo.slug, "--off"]
        ) { [weak self] code in
            guard let self else { return }
            if code == 0 {
                self.lastActionMessage = makePrivate
                    ? "\(silo.displayName) is private — unscoped queries skip it."
                    : "\(silo.displayName) is shared — unscoped queries can return it."
            }
            self.refreshSoon()
        }
    }

    func syncDaemon() {
        runPal(title: "Sync daemon jobs", siloSlug: nil, args: ["daemon", "sync"])
    }

    private func runPal(title: String, siloSlug: String?, args: [String], completion: ((Int32) -> Void)? = nil) {
        let job = Job(title: title, siloSlug: siloSlug)
        jobs.insert(job, at: 0)
        if jobs.count > 20 { jobs.removeLast(jobs.count - 20) }
        let cfg = config
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cfg.python)
        p.arguments = [cfg.palPath] + args
        p.currentDirectoryURL = URL(fileURLWithPath: cfg.workdir)
        p.environment = cfg.subprocessEnvironment()
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        pipe.fileHandleForReading.readabilityHandler = { handle in
            let data = handle.availableData
            guard !data.isEmpty else { return }
            let text = String(decoding: data, as: UTF8.self)
            Task { @MainActor in job.output += text }
        }
        p.terminationHandler = { proc in
            pipe.fileHandleForReading.readabilityHandler = nil
            let rest = pipe.fileHandleForReading.readDataToEndOfFile()
            let code = proc.terminationStatus
            Task { @MainActor [weak self] in
                if !rest.isEmpty { job.output += String(decoding: rest, as: UTF8.self) }
                job.isRunning = false
                job.exitCode = code
                if code == 0 {
                    self?.lastActionMessage = "\(title) finished"
                } else {
                    let tail = job.output.split(whereSeparator: \.isNewline).suffix(6).joined(separator: "\n")
                    self?.problem = ActionProblem(title: "\(title) failed (exit \(code))",
                                                  message: tail.isEmpty ? "pal exited with status \(code) and no output." : tail)
                }
                await self?.refresh()
                completion?(code)
            }
        }
        do {
            try p.run()
        } catch {
            job.output = "Could not start pal: \(error.localizedDescription)"
            job.isRunning = false
            job.exitCode = -1
            problem = ActionProblem(title: "Couldn't run pal", message: "\(error.localizedDescription)\n\(cfg.python) \(cfg.palPath)")
        }
    }

    // MARK: helpers

    func reveal(_ path: String) {
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func openLog(_ path: String) {
        let url = URL(fileURLWithPath: path)
        if FileManager.default.fileExists(atPath: path) {
            NSWorkspace.shared.open(url)
        } else {
            lastActionMessage = "No log yet at \((path as NSString).abbreviatingWithTildeInPath)"
        }
    }

    func copy(_ text: String, note: String) {
        let pb = NSPasteboard.general
        pb.clearContents()
        pb.setString(text, forType: .string)
        lastActionMessage = note
    }
}

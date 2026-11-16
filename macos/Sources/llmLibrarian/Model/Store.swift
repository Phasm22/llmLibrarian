import Foundation
import AppKit
import Combine
import SwiftUI

/// A long-running pal subprocess (reindex, daemon sync) with live output.
@MainActor
final class Job: ObservableObject, Identifiable {
    let id = UUID()
    let title: String
    var siloSlug: String?
    let arguments: [String]
    @Published var output = ""
    @Published var isRunning = true
    @Published var exitCode: Int32?
    @Published var outcome: String?
    var logPath: String?
    let startedAt = Date()

    var removalTarget: Silo?

    init(title: String, siloSlug: String?, arguments: [String]) {
        self.title = title
        self.siloSlug = siloSlug
        self.arguments = arguments
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

struct AddSiloRequest: Identifiable {
    let id = UUID()
    let folderURLs: [URL]

    var displayName: String {
        folderURLs.count == 1 ? folderURLs[0].lastPathComponent : "\(folderURLs.count) folders"
    }
}

@MainActor
final class LibrarianStore: ObservableObject {
    // Data
    @Published private(set) var config: PalConfig
    private let runtime: StoreRuntime
    @Published private(set) var silos: [Silo] = []
    @Published private(set) var services: [ServiceStatus] = []
    @Published private(set) var watchers: [ServiceStatus] = []
    @Published private(set) var queries: [QueryRecord] = []
    @Published private(set) var mcpHealth: MCPHealth?
    @Published private(set) var unindexedBookmarks: [String] = []
    @Published private(set) var registryError: String?
    @Published private(set) var activity: ActivitySummary?
    @Published private(set) var activityUnreadable = false
    @Published var activityWindow = ActivityWindow(rawValue: UserDefaults.standard.string(forKey: "activityWindow") ?? "") ?? .week {
        didSet {
            UserDefaults.standard.set(activityWindow.rawValue, forKey: "activityWindow")
            if oldValue != activityWindow { Task { await refresh() } }
        }
    }
    @Published private(set) var lastRefresh: Date?
    @Published private(set) var isRefreshing = false
    @Published private(set) var hasLoaded = false
    @Published private(set) var isRebuildingApp = false
    private var rebuildProcess: Process?
    @Published var jobs: [Job] = []
    @Published var showJobs = false
    @Published var pendingStop: ServiceStatus?
    @Published var pendingRemoval: Silo?
    @Published private(set) var pendingServiceActions: Set<String> = []
    @Published var lastActionMessage: String?
    @Published var problem: ActionProblem?
    /// Set when the user asks to un-private a silo. Widening exposure is confirmed;
    /// narrowing it is not — the safe direction should not need a dialog.
    @Published var pendingUnprivate: Silo?
    @Published var pendingAddRequest: AddSiloRequest?
    @Published var pendingImageVision: Silo?

    // Navigation (single window, so it lives here; the View menu and the
    // menu bar item drive it too).
    @Published var page: SidebarItem = .overview
    @Published var selectedService: String?
    @Published var selectedSilo: String?
    @Published var selectedQuery: Int?
    // Activity page: tab + filters, set by Overview click-throughs.
    @Published var activityTab: ActivityTab = .searches
    @Published var activitySilo: String?
    @Published var activityNoSourceOnly = false
    @Published var selectedSearch: Int?
    @Published var selectedChange: Int?
    /// The selected Activity row as text (query or path), for Services and copying.
    @Published var activitySelectionText: String?
    @Published var showInspector = true
    @Published var showToolbarLabels = UserDefaults.standard.bool(forKey: "showToolbarLabels") {
        didSet { UserDefaults.standard.set(showToolbarLabels, forKey: "showToolbarLabels") }
    }
    @Published private(set) var zoomStep: Int = UserDefaults.standard.integer(forKey: "zoomStep")

    private var loopTask: Task<Void, Never>?

    init(runtime: StoreRuntime = .live, autoRefresh: Bool = true) {
        self.runtime = runtime
        config = runtime.config()
        if SampleLibrary.enabled {
            do { try SampleLibrary.prepare() }
            catch { fatalError("Cannot prepare screenshot fixtures: \(error)") }
            zoomStep = 0
        }
        guard autoRefresh else { return }
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
    func latestJob(for silo: Silo) -> Job? { jobs.first { $0.siloSlug == silo.slug } }

    /// One collection health rule for Overview, the table, and recovery links.
    func collectionStatus(_ silo: Silo) -> (state: HealthState, text: String) {
        if !silo.pathExists { return (.attention, "Folder missing") }
        if let failures = activity?.stats(silo.slug).openFailures, failures > 0 {
            return (.attention, "\(failures) files failing")
        }
        if activityUnreadable { return (.unknown, "Index status unavailable") }
        guard let watcher = watcher(for: silo) else { return (.unknown, "Not watched") }
        if watcher.state != .running {
            return (.attention, watcher.state == .stopped ? "Watcher stopped" : "Watcher needs attention")
        }
        return (.running, "Watching")
    }

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
            return activitySelectionText
        }
    }

    /// Hand the selected row to a macOS Service by name (e.g. the "Ask Claude"
    /// quick action). Goes straight to NSPerformService, so it works even when
    /// the service's own Info.plist forgets to declare NSSendTypes.
    func performService(_ name: String) {
        guard !SampleLibrary.enabled else { return }
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

    /// Open the Activity page on the rows behind an Overview number.
    func showActivity(_ tab: ActivityTab, silo: String? = nil, noSourceOnly: Bool = false) {
        activityTab = tab
        activitySilo = silo
        activityNoSourceOnly = noSourceOnly
        page = .activity
    }

    /// Action items for the Overview strip and the Problems tab: service, watcher and
    /// folder issues, plus indexing failures from the ledger. No-source searches are a
    /// usage signal (an Overview tile), not an action item.
    var attentionCount: Int { issues.count + (activity?.failures.count ?? 0) }

    func show(issue: Issue) {
        page = issue.page
        if let l = issue.serviceLabel { selectedService = l }
        if let s = issue.siloSlug, issue.page == .silos { selectedSilo = s }
        showInspector = true
    }

    // MARK: refresh

    func refresh() async {
        while isRefreshing {
            guard !Task.isCancelled else { return }
            try? await Task.sleep(for: .milliseconds(30))
        }
        isRefreshing = true
        config = runtime.config()
        let cfg = config
        let window = activityWindow
        let collect = runtime.collect
        let snap = await Task.detached(priority: .userInitiated) { await collect(cfg, window) }.value
        silos = snap.silos
        services = snap.services
        watchers = snap.watchers
        queries = snap.queries
        mcpHealth = snap.mcpHealth
        unindexedBookmarks = snap.unindexedBookmarks
        registryError = snap.registryError
        activity = snap.activity
        activityUnreadable = snap.activityUnreadable
        lastRefresh = Date()
        hasLoaded = true
        isRefreshing = false
        if SnapshotMode.directory != nil {
            if selectedSilo == nil { selectedSilo = silos.first?.slug }
            if selectedService == nil { selectedService = watchers.first { $0.state != .running }?.label ?? services.first?.label }
            if selectedQuery == nil { selectedQuery = queries.first?.id }
        }
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

    // MARK: app rebuild

    var rebuildLogPath: String { config.logsDir + "/app-build.log" }
    var canRebuildApp: Bool { !SampleLibrary.enabled && !isRebuildingApp && !jobs.contains(where: \.isRunning) }

    func rebuildApp() {
        guard canRebuildApp else { return }
        AppDelegate.showMainWindow()
        let script = config.workdir + "/macos/build.sh"
        guard FileManager.default.fileExists(atPath: script) else {
            problem = ActionProblem(title: "Cannot rebuild the app",
                                    message: "The build script is missing at \(script). Restore the local checkout before rebuilding.")
            return
        }
        let logPath = rebuildLogPath
        isRebuildingApp = true
        do {
            rebuildProcess = try AppRebuild.start(workdir: config.workdir, logPath: logPath,
                                                  environment: config.subprocessEnvironment()) { [weak self] status in
                Task { @MainActor in
                    guard let self else { return }
                    self.isRebuildingApp = false
                    self.rebuildProcess = nil
                    if status != 0 {
                        self.problem = ActionProblem(title: "App rebuild failed",
                                                     message: "The build exited with status \(status). Open the build log for details.",
                                                     logPath: logPath)
                    } else {
                        self.lastActionMessage = "App rebuilt and relaunched"
                    }
                }
            }
        } catch {
            isRebuildingApp = false
            problem = ActionProblem(title: "Could not start app rebuild", message: error.localizedDescription,
                                    logPath: logPath)
        }
    }

    // MARK: service actions

    func restart(_ s: ServiceStatus) {
        changeService(s, command: .restart)
    }

    func stop(_ s: ServiceStatus) {
        changeService(s, command: .stop)
    }

    func requestStop(_ s: ServiceStatus) {
        if s.kind == .watcher { stop(s) }
        else { AppDelegate.showMainWindow(); pendingStop = s }
    }

    func start(_ s: ServiceStatus) { changeService(s, command: .start) }

    private func changeService(_ s: ServiceStatus, command: ServiceCommand) {
        guard !SampleLibrary.enabled, !pendingServiceActions.contains(s.label) else { return }
        if command != .stop, let silo = silo(slug: s.siloSlug), !silo.pathExists {
            problem = ActionProblem(
                title: "The watcher for \(s.title) can't start",
                message: "Its folder \(silo.tildePath) no longer exists, so the watcher exits as soon as launchd starts it (last exit code \(s.lastExit ?? 1)). Point the silo at the folder's new location, or remove the silo.",
                service: s, silo: silo, logPath: s.logPath)
            return
        }
        guard s.installed || command == .stop else {
            problem = ActionProblem(title: "\(s.title) is not installed",
                                    message: "There is no launchd plist for \(s.label). Use Sync Watchers in Services to recreate watcher jobs, or run `pal mcp install` / `pal chroma install` for the core services.",
                                    service: s)
            return
        }
        pendingServiceActions.insert(s.label)
        let run = runtime.serviceCommand, cfg = config
        Task {
            defer { pendingServiceActions.remove(s.label) }
            let result = await Task.detached { run(command, s, cfg) }.value
            guard result.ok else {
                let raw = result.combined.isEmpty ? "launchctl exited with status \(result.status)." : result.combined
                problem = ActionProblem(title: "Could not change \(s.title)", message: Self.explainLaunchctl(raw),
                                        service: s, silo: silo(slug: s.siloSlug), logPath: s.logPath)
                return
            }
            for delay in runtime.settleDelays {
                try? await Task.sleep(for: delay)
                await refresh()
                let current = service(label: s.label)
                let reached = command == .stop ? (current == nil || current?.loaded == false)
                    : current?.state == .running
                if reached {
                    lastActionMessage = command == .stop ? "Stopped \(s.title)" : "\(s.title) is running"
                    return
                }
            }
            problem = ActionProblem(title: "\(s.title) has not reached the requested state",
                                    message: "The service command was accepted, but the follow-up check did not confirm \(command == .stop ? "that it stopped" : "a running, healthy service"). It may still be starting or may have exited. Open its log and check Services before retrying.",
                                    service: service(label: s.label) ?? s, silo: silo(slug: s.siloSlug), logPath: s.logPath)
        }
    }

    /// Translate launchctl's terse errors into something a person can act on.
    static func explainLaunchctl(_ raw: String) -> String {
        if raw.contains("Input/output error") {
            return "launchd could not complete the request. A conflicting loaded job or a startup failure can cause this. Check the service state and log before retrying.\n\(raw)"
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

    /// Choose a folder, index it as a silo, then reconcile watcher services.
    /// The watcher step runs only after indexing succeeds.
    func chooseSiloFolder() {
        let panel = NSOpenPanel()
        panel.title = "Add Silo"
        panel.message = "Choose a folder to index and keep in sync with llmLibrarian."
        panel.prompt = "Add Silo"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = false
        panel.allowsMultipleSelection = false
        panel.resolvesAliases = true
        panel.directoryURL = URL(fileURLWithPath: NSHomeDirectory())
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        addSilo(at: url)
    }

    func addSilo(at folderURL: URL) {
        addSilos(at: [folderURL])
    }

    /// Validate folders received from the app or Finder Service, then present
    /// the ingest choices before any indexing work starts.
    func addSilos(at folderURLs: [URL]) {
        page = .silos
        problem = nil
        var seen = Set<String>()
        var newFolders: [URL] = []
        var existingSilo: Silo?
        for url in folderURLs {
            let path = SiloAddWorkflow.normalizedPath(for: url)
            guard seen.insert(path).inserted else { continue }
            var isDirectory: ObjCBool = false
            guard FileManager.default.fileExists(atPath: path, isDirectory: &isDirectory), isDirectory.boolValue else {
                problem = ActionProblem(title: "Couldn't add silo", message: "The selected folder no longer exists: \(path)")
                return
            }
            if let existing = silos.first(where: {
                SiloAddWorkflow.normalizedPath(for: URL(fileURLWithPath: $0.path)) == path
            }) {
                existingSilo = existing
            } else {
                newFolders.append(URL(fileURLWithPath: path, isDirectory: true))
            }
        }

        guard !newFolders.isEmpty else {
            if let existingSilo {
                selectedSilo = existingSilo.slug
                showInspector = true
                lastActionMessage = "\(existingSilo.displayName) is already a silo."
            }
            return
        }

        pendingAddRequest = AddSiloRequest(folderURLs: newFolders)
    }

    func confirmAddSilos(imageVision: Bool) {
        guard let request = pendingAddRequest else { return }
        pendingAddRequest = nil
        let newFolders = request.folderURLs

        let jobs = SiloAddWorkflow.jobs(for: newFolders, imageVision: imageVision)
        runPalJobs(jobs[...]) { [weak self] succeeded in
            guard succeeded, let self else { return }
            let added = newFolders.compactMap { folder in
                self.silos.first { SiloAddWorkflow.normalizedPath(for: URL(fileURLWithPath: $0.path)) == SiloAddWorkflow.normalizedPath(for: folder) }
            }
            guard added.count == newFolders.count, added.allSatisfy({ self.watcher(for: $0)?.state == .running }) else {
                self.problem = ActionProblem(title: "Indexed, but watcher setup needs attention",
                                             message: "The indexing steps completed, but running watchers were not confirmed for every folder. Open Services, use Sync Watchers, and inspect the watcher logs. Job logs are available from Recent Jobs.")
                return
            }
            let lastPath = SiloAddWorkflow.normalizedPath(for: newFolders.last!)
            self.selectedSilo = self.silos.first(where: {
                SiloAddWorkflow.normalizedPath(for: URL(fileURLWithPath: $0.path)) == lastPath
            })?.slug
            self.showInspector = true
            self.lastActionMessage = newFolders.count == 1
                ? "Added \(newFolders[0].lastPathComponent)\(imageVision ? " with image vision" : "") and started its watcher."
                : "Added \(newFolders.count) silos\(imageVision ? " with image vision" : "") and started their watchers."
        }
    }

    /// Ask for the folder's new location, index it, then remove the old silo.
    func relocate(_ silo: Silo) {
        guard !silo.isPrivate else {
            problem = ActionProblem(title: "Restore the private collection's folder",
                                    message: "Restore this folder to \(silo.path). Creating a replacement through Add Silo would create a shared collection, so this action cannot safely move a private collection. The existing private index has been kept.")
            return
        }
        let panel = NSOpenPanel()
        panel.title = "Locate \(silo.displayName)"
        panel.message = "Index the new location as a separate collection. The old index is kept until you review the replacement and explicitly remove the old collection."
        panel.prompt = "Use This Folder"
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        let parent = (silo.path as NSString).deletingLastPathComponent
        panel.directoryURL = URL(fileURLWithPath: FileManager.default.fileExists(atPath: parent) ? parent : NSHomeDirectory())
        NSApp.activate(ignoringOtherApps: true)
        guard panel.runModal() == .OK, let url = panel.url else { return }
        prepareReplacement(silo, at: url)
    }

    /// Keep the old index throughout staging. Removal is a separate, explicit
    /// action after the user has inspected the replacement's files and scope.
    func prepareReplacement(_ silo: Silo, at url: URL) {
        guard !silo.isPrivate else {
            problem = ActionProblem(title: "Private relocation is unavailable", message: "Restore the folder to \(silo.path) to preserve its private scope. The old index is unchanged.")
            return
        }
        let newPath = SiloAddWorkflow.normalizedPath(for: url)
        guard newPath != SiloAddWorkflow.normalizedPath(for: URL(fileURLWithPath: silo.path)) else {
            reindex(silo)
            return
        }
        problem = nil
        var args = ["pull", newPath]
        if silo.imageVision { args.append("--image-vision") }
        runPal(title: "Index replacement for \(silo.displayName)", siloSlug: nil, args: args) { [weak self] code in
            guard code == 0, let self else { return }
            if let replacement = self.silos.first(where: { SiloAddWorkflow.normalizedPath(for: URL(fileURLWithPath: $0.path)) == newPath }) {
                self.show(silo: replacement)
            }
            self.lastActionMessage = "Replacement indexed. Review it before removing the old collection; use Sync Watchers to watch the new folder."
        }
    }

    func canRemove(_ silo: Silo) -> Bool {
        !isRebuildingApp && activeJob(for: silo) == nil
            && !jobs.contains { $0.isRunning && ($0.arguments == ["daemon", "sync"] || SiloAddWorkflow.pullTarget(of: $0.arguments) == silo.path) }
            && !(watcher(for: silo).map { pendingServiceActions.contains($0.label) } ?? false)
    }

    func requestRemoval(_ silo: Silo) {
        guard canRemove(silo) else { return }
        AppDelegate.showMainWindow()
        showJobs = false
        pendingRemoval = silo
    }

    func removeSilo(_ silo: Silo) {
        pendingRemoval = nil
        guard canRemove(silo) else {
            problem = ActionProblem(title: "Collection is busy", message: "Wait for its current job to finish before removing this collection.", silo: silo)
            return
        }
        problem = nil
        showJobs = true
        runPal(title: "Remove collection \(silo.displayName)", siloSlug: silo.slug, args: ["remove", silo.slug], removing: silo) { [weak self] code in
            guard code == 0, let self else { return }
            if self.selectedSilo == silo.slug { self.selectedSilo = nil }
            self.lastActionMessage = "Removed \(silo.displayName). Original files were kept."
        }
    }

    // MARK: pal jobs

    func reindex(_ silo: Silo) {
        guard activeJob(for: silo) == nil else { return }
        guard silo.pathExists else {
            problem = ActionProblem(title: "Collection folder is missing", message: "Restore or locate \(silo.path) before reindexing.", silo: silo)
            return
        }
        runPal(title: "Reindex \(silo.displayName)", siloSlug: silo.slug, args: ["pull", silo.path])
    }

    func enableImageVision(_ silo: Silo) {
        pendingImageVision = silo
    }

    func applyImageVision(_ silo: Silo) {
        pendingImageVision = nil
        guard activeJob(for: silo) == nil else { return }
        runPal(
            title: "Enable image vision for \(silo.displayName)",
            siloSlug: silo.slug,
            args: ["pull", silo.path, "--image-vision"]
        ) { [weak self] code in
            guard code == 0, let self else { return }
            self.lastActionMessage = "Image vision is enabled for \(silo.displayName)."
            self.refreshSoon()
        }
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
        guard !jobs.contains(where: { $0.isRunning && $0.arguments == ["daemon", "sync"] }) else { return }
        runPal(title: "Sync daemon jobs", siloSlug: nil, args: ["daemon", "sync"])
    }

    private func runPal(title: String, siloSlug: String?, args: [String], removing: Silo? = nil, completion: ((Int32) -> Void)? = nil) {
        guard !SampleLibrary.enabled else { return }
        // One index job per folder. pal also refuses (exit 75) via a folder lock,
        // which covers pulls this app instance did not start, e.g. one orphaned
        // when the app was relaunched; this check just avoids spawning at all.
        if let target = SiloAddWorkflow.pullTarget(of: args),
           jobs.contains(where: { $0.isRunning && SiloAddWorkflow.pullTarget(of: $0.arguments) == target }) {
            problem = ActionProblem(title: "Already being indexed",
                                    message: "\(target) is already being indexed by a job in this window. Wait for it to finish.")
            completion?(SiloAddWorkflow.alreadyIndexingExit)
            return
        }
        let removalTarget = removing
        let job = Job(title: title, siloSlug: siloSlug, arguments: args)
        job.removalTarget = removalTarget
        jobs.insert(job, at: 0)
        // Keep running jobs, even when many folders were added together.
        let completed = jobs.filter { !$0.isRunning }.dropFirst(20).map(\.id)
        jobs.removeAll { completed.contains($0.id) }
        let cfg = config
        let logPath = cfg.logsDir + "/jobs/" + job.id.uuidString + ".log"
        job.logPath = logPath
        let p = Process()
        p.executableURL = URL(fileURLWithPath: cfg.python)
        p.arguments = [cfg.palPath] + args
        p.currentDirectoryURL = URL(fileURLWithPath: cfg.workdir)
        p.environment = cfg.subprocessEnvironment()
        p.standardInput = FileHandle.nullDevice
        do {
            try FileManager.default.createDirectory(atPath: cfg.logsDir + "/jobs", withIntermediateDirectories: true)
            let header = "\(title)\nStarted: \(ISO8601DateFormatter().string(from: job.startedAt))\nArguments: \(args.description)\n\(AppBuildInfo().report)\n\n"
            try Data(header.utf8).write(to: URL(fileURLWithPath: logPath))
            let handle = try FileHandle(forWritingTo: URL(fileURLWithPath: logPath))
            defer { try? handle.close() }
            try handle.seekToEnd()
            p.standardOutput = handle
            p.standardError = handle
            try p.run()
        } catch {
            job.output = "Could not start pal: \(error.localizedDescription)"
            job.isRunning = false
            job.exitCode = -1
            job.outcome = "Could not start"
            problem = ActionProblem(title: "Couldn't run pal", message: "\(error.localizedDescription)\n\(cfg.python) \(cfg.palPath)", logPath: logPath)
            completion?(-1)
            return
        }
        // A file survives app relaunch and cannot deadlock when compiler/model
        // output exceeds a pipe buffer. Only its tail is kept in the UI.
        Task { [weak self] in
            while p.isRunning {
                job.output = Self.logTail(logPath)
                try? await Task.sleep(for: .milliseconds(200))
            }
            p.waitUntilExit()
            job.output = Self.logTail(logPath)
            job.exitCode = p.terminationStatus
            guard let self else { job.isRunning = false; return }
            await refresh()
            var completionCode = p.terminationStatus
            if let target = SiloAddWorkflow.pullTarget(of: args) {
                job.siloSlug = silos.first { SiloAddWorkflow.normalizedPath(for: URL(fileURLWithPath: $0.path)) == target }?.slug ?? siloSlug
            }
            if p.terminationStatus == 0, let target = SiloAddWorkflow.pullTarget(of: args) {
                let indexed = silos.first { SiloAddWorkflow.normalizedPath(for: URL(fileURLWithPath: $0.path)) == target }
                if indexed == nil || indexed?.filesIndexed == 0 {
                    completionCode = 1
                    job.outcome = "No indexed files confirmed"
                    problem = ActionProblem(title: "Indexing needs attention",
                                            message: "The command finished, but no indexed files were confirmed for \(target). Check the job log for exclusions, unsupported files, or indexing errors before continuing.", logPath: logPath)
                } else if let slug = indexed?.slug, activity?.stats(slug).openFailures ?? 0 > 0 {
                    completionCode = 1
                    job.outcome = "Finished with unresolved indexing problems"
                    problem = ActionProblem(title: "Indexing still has problems", message: "The collection still has recorded failures. Review Problems before retrying; the command exiting successfully does not mean every file indexed.",
                                            silo: indexed, logPath: logPath)
                } else if activityUnreadable {
                    completionCode = 1
                    job.outcome = "Index updated; problem status unavailable"
                    problem = ActionProblem(title: "Could not verify indexing problems", message: "The command finished, but the activity history could not be read. Open the log and review the collection before continuing.", silo: indexed, logPath: logPath)
                }
            }
            if p.terminationStatus == 0, args.first == "remove", let slug = siloSlug {
                let bookmarkRemains = removalTarget.map { self.unindexedBookmarks.contains($0.path) } ?? false
                if registryError != nil || silos.contains(where: { $0.slug == slug }) || watchers.contains(where: { $0.siloSlug == slug }) || bookmarkRemains {
                    completionCode = 1
                    job.outcome = "Removal could not be confirmed"
                    problem = ActionProblem(title: "Removal needs attention",
                                            message: "The collection or its watcher is still listed, or the registry could not be read. Review the log and refresh. If the watcher remains, use Sync Watchers in Services. Original files were kept.",
                                            silo: removalTarget, logPath: logPath)
                }
            }
            if completionCode == 0 {
                job.outcome = "Finished"
                lastActionMessage = "\(title) finished"
            } else if p.terminationStatus != 0 {
                job.outcome = p.terminationStatus == SiloAddWorkflow.alreadyIndexingExit ? "Another job is indexing this folder" : "Failed (exit \(p.terminationStatus))"
                let tail = job.output.split(whereSeparator: \.isNewline).suffix(8).joined(separator: "\n")
                problem = ActionProblem(title: job.outcome!, message: tail.isEmpty ? "No output was recorded. Check the installation and retry." : tail,
                                        silo: removalTarget ?? silo(slug: job.siloSlug), logPath: logPath)
            }
            job.isRunning = false
            if let log = FileHandle(forWritingAtPath: logPath) {
                _ = try? log.seekToEnd()
                try? log.write(contentsOf: Data("\nFinished: \(ISO8601DateFormatter().string(from: Date()))\nExit: \(p.terminationStatus)\nOutcome: \(job.outcome ?? "Unknown")\n".utf8))
                try? log.close()
                job.output = Self.logTail(logPath)
            }
            completion?(completionCode)
        }
    }

    private static func logTail(_ path: String) -> String {
        guard let file = FileHandle(forReadingAtPath: path) else { return "" }
        defer { try? file.close() }
        let size = (try? file.seekToEnd()) ?? 0
        try? file.seek(toOffset: size > 128_000 ? size - 128_000 : 0)
        return String(decoding: (try? file.readToEnd()) ?? Data(), as: UTF8.self)
    }

    func retry(_ job: Job) {
        guard !job.isRunning, job.arguments.first == "pull" || job.arguments == ["daemon", "sync"] else { return }
        runPal(title: job.title, siloSlug: job.siloSlug, args: job.arguments)
    }

    private func runPalJobs(_ pending: ArraySlice<PalJobSpec>, completion: @escaping (Bool) -> Void) {
        guard let next = pending.first else { completion(true); return }
        runPal(title: next.title, siloSlug: next.siloSlug, args: next.arguments) { [weak self] code in
            // Busy, partial success, and launch failure all stop the chain.
            // Never claim a folder was added because another process owns it.
            guard code == 0, let self else { completion(false); return }
            self.runPalJobs(pending.dropFirst(), completion: completion)
        }
    }

    // MARK: helpers

    func reveal(_ path: String) {
        guard FileManager.default.fileExists(atPath: path) else {
            lastActionMessage = "Path not found: \(path)"
            return
        }
        NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
    }

    func openJobLogs() {
        let path = config.logsDir + "/jobs"
        do {
            try FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: true)
            reveal(path)
        } catch {
            problem = ActionProblem(title: "Cannot open saved logs", message: error.localizedDescription)
        }
    }

    func openLog(_ path: String) {
        let url = URL(fileURLWithPath: path)
        if FileManager.default.fileExists(atPath: path) {
            if !NSWorkspace.shared.open(url) {
                problem = ActionProblem(title: "Cannot open log", message: "Choose an app to open \(path) from Finder.")
                reveal(path)
            }
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

import AppKit
import SwiftUI

/// Developer aid: with LLMLIBRARIAN_SNAPSHOT_DIR set, the app walks every page,
/// writes a PNG of its own window for each (light and dark), then quits.
/// Uses AppKit's display cache, so it needs no screen-recording permission.
enum SnapshotMode {
    static var directory: String? { ProcessInfo.processInfo.environment["LLMLIBRARIAN_SNAPSHOT_DIR"] }

    @MainActor
    static func run(store: LibrarianStore) async {
        guard let dir = directory else { return }
        try? FileManager.default.createDirectory(atPath: dir, withIntermediateDirectories: true)
        try? await Task.sleep(for: .seconds(2.5))
        if let window = NSApp.windows.first(where: { AppDelegate.isMainWindow($0) }) {
            window.setContentSize(NSSize(width: 1440, height: 860))
            window.center()
        }
        for appearance in [NSAppearance.Name.aqua, .darkAqua] {
            NSApp.appearance = NSAppearance(named: appearance)
            for item in SidebarItem.allCases {
                AppDelegate.showMainWindow()
                store.page = item
                store.showInspector = item != .overview
                if item == .silos {
                    store.selectedSilo = store.silos.first { store.watcher(for: $0)?.recentChanges.isEmpty == false }?.slug
                    store.showInspector = store.selectedSilo != nil
                }
                if item == .services {
                    // Show a watcher's details so the inspector is in the shot too.
                    let watcher = store.watchers.first { !$0.recentChanges.isEmpty } ?? store.watchers.first
                    store.selectedService = watcher?.label
                    store.showInspector = watcher != nil
                }
                try? await Task.sleep(for: .seconds(1.2))
                capture(to: "\(dir)/\(item.rawValue)-\(appearance == .aqua ? "light" : "dark").png")
            }
            for tab in [ActivityTab.changes, .problems] {
                store.showActivity(tab)
                try? await Task.sleep(for: .seconds(1.2))
                capture(to: "\(dir)/activity-\(tab.rawValue)-\(appearance == .aqua ? "light" : "dark").png")
            }
            await capturePanel(AboutView().environmentObject(store), to: "\(dir)/about-\(appearance == .aqua ? "light" : "dark").png")
            await capturePanel(JobsView().environmentObject(store), to: "\(dir)/jobs-empty-\(appearance == .aqua ? "light" : "dark").png")
            if let window = NSApp.windows.first(where: { AppDelegate.isMainWindow($0) }) {
                window.setContentSize(NSSize(width: 900, height: 600))
                try? await Task.sleep(for: .milliseconds(300))
                capture(to: "\(dir)/problems-compact-\(appearance == .aqua ? "light" : "dark").png")
                window.setContentSize(NSSize(width: 1440, height: 860))
            }
        }
        NSApp.terminate(nil)
    }

    @MainActor
    private static func capturePanel<V: View>(_ view: V, to path: String) async {
        let host = NSHostingView(rootView: view)
        let window = NSWindow(contentRect: CGRect(origin: .zero, size: host.fittingSize),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.isReleasedWhenClosed = false
        window.contentView = host
        window.appearance = NSApp.appearance
        window.orderFront(nil)
        try? await Task.sleep(for: .milliseconds(300))
        capture(view: host.superview ?? host, to: path)
        window.close()
    }

    @MainActor
    static func capture(to path: String) {
        guard let window = NSApp.windows.first(where: { AppDelegate.isMainWindow($0) && $0.isVisible }),
              let content = window.contentView else { return }
        let view = content.superview ?? content
        capture(view: view, to: path)
    }

    @MainActor
    static func capture(view: NSView, to path: String) {
        view.layoutSubtreeIfNeeded()
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else { return }
        try? data.write(to: URL(fileURLWithPath: path))
    }
}

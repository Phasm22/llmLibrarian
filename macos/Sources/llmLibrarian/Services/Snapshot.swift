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
        for appearance in [NSAppearance.Name.aqua, .darkAqua] {
            NSApp.appearance = NSAppearance(named: appearance)
            for item in SidebarItem.allCases {
                store.page = item
                try? await Task.sleep(for: .seconds(1.2))
                capture(to: "\(dir)/\(item.rawValue)-\(appearance == .aqua ? "light" : "dark").png")
            }
        }
        NSApp.terminate(nil)
    }

    @MainActor
    static func capture(to path: String) {
        guard let window = NSApp.windows.first(where: { AppDelegate.isMainWindow($0) && $0.isVisible }),
              let content = window.contentView else { return }
        let view = content.superview ?? content
        guard let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { return }
        view.cacheDisplay(in: view.bounds, to: rep)
        guard let data = rep.representation(using: .png, properties: [:]) else { return }
        try? data.write(to: URL(fileURLWithPath: path))
    }
}

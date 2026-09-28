import AppKit
import Combine

/// The menu bar item, built with AppKit so items can carry colored status dots
/// and submenus exactly like the system's own status menus.
@MainActor
final class StatusMenuController: NSObject, NSMenuDelegate {
    private let store: LibrarianStore
    private let statusItem: NSStatusItem
    private let menu = NSMenu()

    init(store: LibrarianStore) {
        self.store = store
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        super.init()
        if let button = statusItem.button {
            button.image = NSImage(systemSymbolName: "books.vertical", accessibilityDescription: "llmLibrarian")
            button.image?.isTemplate = true
            button.toolTip = "llmLibrarian"
        }
        menu.delegate = self
        menu.autoenablesItems = false
        statusItem.menu = menu
    }

    nonisolated func menuNeedsUpdate(_ menu: NSMenu) {
        MainActor.assumeIsolated { rebuild() }
    }

    private func rebuild() {
        menu.removeAllItems()

        menu.addItem(disabled(store.overallHeadline))
        menu.addItem(disabled("\(store.silos.count) silos · \(store.totalChunks.grouped) chunks"))
        menu.addItem(.separator())

        for s in store.services {
            let item = NSMenuItem(title: "\(s.title) — \(s.stateText)", action: nil, keyEquivalent: "")
            item.image = StatusDotImage.nsImage(for: s.state)
            item.submenu = serviceMenu(for: s)
            menu.addItem(item)
        }
        if !store.watchers.isEmpty {
            let all = store.watchersRunning == store.watchers.count
            let item = NSMenuItem(title: "Watchers — \(store.watchersRunning) of \(store.watchers.count) running", action: nil, keyEquivalent: "")
            item.image = StatusDotImage.nsImage(for: all ? .running : .attention)
            let sub = NSMenu()
            for w in store.watchers {
                let wi = ClosureMenuItem(title: w.title) { [store] in
                    AppDelegate.showMainWindow()
                    store.show(service: w)
                }
                wi.image = StatusDotImage.nsImage(for: w.state)
                sub.addItem(wi)
            }
            sub.addItem(.separator())
            sub.addItem(ClosureMenuItem(title: "Show All Watchers") { [store] in
                AppDelegate.showMainWindow()
                store.page = .services
            })
            item.submenu = sub
            menu.addItem(item)
        }
        menu.addItem(.separator())

        menu.addItem(ClosureMenuItem(title: "Open llmLibrarian", key: "o") { AppDelegate.showMainWindow() })
        menu.addItem(ClosureMenuItem(title: "Copy MCP Endpoint") { [store] in
            store.copy(store.config.mcpURL, note: "Copied MCP endpoint")
        })
        menu.addItem(.separator())
        menu.addItem(ClosureMenuItem(title: "Quit llmLibrarian", key: "q") { NSApp.terminate(nil) })
    }

    private func serviceMenu(for s: ServiceStatus) -> NSMenu {
        let sub = NSMenu()
        sub.addItem(ClosureMenuItem(title: "Show in llmLibrarian") { [store] in
            AppDelegate.showMainWindow()
            store.show(service: s)
        })
        sub.addItem(.separator())
        if s.state == .stopped, s.installed {
            sub.addItem(ClosureMenuItem(title: "Start") { [store] in store.start(s) })
        }
        if s.loaded {
            sub.addItem(ClosureMenuItem(title: "Restart") { [store] in store.restart(s) })
        }
        sub.addItem(ClosureMenuItem(title: "Open Log") { [store] in store.openLog(s.logPath) })
        return sub
    }

    private func disabled(_ title: String) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        item.isEnabled = false
        return item
    }
}

final class ClosureMenuItem: NSMenuItem {
    private let handler: () -> Void

    init(title: String, key: String = "", handler: @escaping () -> Void) {
        self.handler = handler
        super.init(title: title, action: #selector(fire), keyEquivalent: key)
        target = self
    }

    required init(coder: NSCoder) { fatalError("not supported") }

    @objc private func fire() { handler() }
}

/// Colored status dots sized for menu items (non-template so color survives).
enum StatusDotImage {
    private static var cache: [HealthState: NSImage] = [:]

    static func nsImage(for state: HealthState) -> NSImage {
        if let cached = cache[state] { return cached }
        let color: NSColor
        switch state {
        case .running: color = .systemGreen
        case .attention: color = .systemOrange
        case .stopped: color = .systemRed
        case .unknown: color = .tertiaryLabelColor
        }
        let size = NSSize(width: 14, height: 14)
        let img = NSImage(size: size, flipped: false) { _ in
            let dot = NSRect(x: 3, y: 3, width: 8, height: 8)
            color.setFill()
            NSBezierPath(ovalIn: dot).fill()
            NSColor.black.withAlphaComponent(0.12).setStroke()
            NSBezierPath(ovalIn: dot.insetBy(dx: 0.25, dy: 0.25)).stroke()
            return true
        }
        img.isTemplate = false
        cache[state] = img
        return img
    }
}

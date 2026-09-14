import SwiftUI
import AppKit

@main
struct LibrarianApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var appDelegate
    @StateObject private var store = LibrarianStore()

    var body: some Scene {
        Window("llmLibrarian", id: AppDelegate.mainWindowID) {
            MainWindow()
                .environmentObject(store)
                .frame(minWidth: 900, minHeight: 560)
                .onAppear { appDelegate.attach(store) }
        }
        .windowToolbarStyle(.unified)
        .defaultSize(width: 1320, height: 820)
        .commands {
            CommandGroup(replacing: .newItem) {}
            SidebarCommands()
            CommandGroup(after: .sidebar) {
                Divider()
                ForEach(SidebarItem.allCases) { item in
                    Button(item.title) { store.page = item }
                        .keyboardShortcut(KeyEquivalent(item.shortcut), modifiers: .command)
                }
                Divider()
                Button("Show Details") { store.showInspector.toggle() }
                    .keyboardShortcut("i", modifiers: [.command, .option])
                Divider()
                Button("Zoom In") { store.zoomIn() }
                    .keyboardShortcut("=", modifiers: .command)
                    .disabled(!store.canZoomIn)
                Button("Zoom Out") { store.zoomOut() }
                    .keyboardShortcut("-", modifiers: .command)
                    .disabled(!store.canZoomOut)
                Button("Actual Size") { store.zoomReset() }
                    .keyboardShortcut("0", modifiers: .command)
                    .disabled(store.zoomStep == 0)
                Divider()
                Button("Refresh") { Task { await store.refresh() } }
                    .keyboardShortcut("r", modifiers: .command)
            }
        }
    }
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate, @preconcurrency NSServicesMenuRequestor {
    static let mainWindowID = "main"
    /// Set by MainWindow once SwiftUI hands us an openWindow action.
    static var openMainWindow: (() -> Void)?
    var store: LibrarianStore?
    private var statusMenu: StatusMenuController?

    /// Called once the SwiftUI scene exists; sets up the AppKit status item.
    func attach(_ store: LibrarianStore) {
        guard self.store == nil else { return }
        self.store = store
        statusMenu = StatusMenuController(store: store)
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        // LSUIElement keeps us out of the Dock while idle; behave like a regular
        // app whenever the main window is showing.
        NSApp.activate(ignoringOtherApps: true)
        // Let the Services submenu (e.g. an "Ask Claude" quick action) act on
        // the selected row as text.
        NSApp.registerServicesMenuSendTypes([.string], returnTypes: [])
        NotificationCenter.default.addObserver(
            self, selector: #selector(windowWillClose(_:)), name: NSWindow.willCloseNotification, object: nil)
        // ⌘⇧= is what a US keyboard sends for "⌘+"; the menu item itself is ⌘=.
        NSEvent.addLocalMonitorForEvents(matching: .keyDown) { [weak self] event in
            let flags = event.modifierFlags.intersection(.deviceIndependentFlagsMask)
            if flags == [.command, .shift], event.charactersIgnoringModifiers == "=" || event.characters == "+" {
                Task { @MainActor in self?.store?.zoomIn() }
                return nil
            }
            return event
        }
    }

    func applicationShouldHandleReopen(_ sender: NSApplication, hasVisibleWindows flag: Bool) -> Bool {
        Self.showMainWindow()
        return false
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { false }

    @objc private func windowWillClose(_ note: Notification) {
        guard let w = note.object as? NSWindow, Self.isMainWindow(w) else { return }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.25) {
            let stillOpen = NSApp.windows.contains { Self.isMainWindow($0) && $0.isVisible }
            if !stillOpen { NSApp.setActivationPolicy(.accessory) }
        }
    }

    // MARK: Services menu (NSServicesMenuRequestor)

    @objc func validRequestor(forSendType sendType: NSPasteboard.PasteboardType?,
                              returnType: NSPasteboard.PasteboardType?) -> Any? {
        guard returnType == nil, sendType == .string, store?.selectionText != nil else { return nil }
        return self
    }

    @objc func writeSelection(to pboard: NSPasteboard, types: [NSPasteboard.PasteboardType]) -> Bool {
        guard types.contains(.string), let text = store?.selectionText else { return false }
        pboard.clearContents()
        return pboard.setString(text, forType: .string)
    }

    @objc func readSelection(from pboard: NSPasteboard) -> Bool { false }

    static func isMainWindow(_ w: NSWindow) -> Bool {
        (w.identifier?.rawValue ?? "").contains(mainWindowID)
    }

    static func showMainWindow() {
        if NSApp.activationPolicy() != .regular {
            NSApp.setActivationPolicy(.regular)
            // AppKit leaves Hide/Quit disabled after an accessory→regular
            // switch until the menu bar is rebuilt.
            let menu = NSApp.mainMenu
            NSApp.mainMenu = nil
            NSApp.mainMenu = menu
        }
        if let w = NSApp.windows.first(where: { isMainWindow($0) }) {
            w.makeKeyAndOrderFront(nil)
        } else {
            openMainWindow?()
        }
        NSApp.activate(ignoringOtherApps: true)
    }
}

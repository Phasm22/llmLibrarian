import SwiftUI
import AppKit

/// Keep menu actions on the same store paths as toolbar and contextual actions.
struct LibrarianCommands: Commands {
    @ObservedObject var store: LibrarianStore

    private var selectedSilo: Silo? {
        store.page == .silos ? store.silo(slug: store.selectedSilo) : nil
    }
    private var selectedService: ServiceStatus? {
        store.page == .services ? store.service(label: store.selectedService) : nil
    }

    var body: some Commands {
        CommandGroup(replacing: .newItem) {
            Button("Add Silo…") { AppDelegate.showMainWindow(); store.chooseSiloFolder() }
                .keyboardShortcut("o", modifiers: [.command, .shift])
            Button("Reveal Selected Folder in Finder") {
                if let silo = selectedSilo { store.reveal(silo.path) }
            }
            .keyboardShortcut("r", modifiers: [.command, .shift])
            .disabled(selectedSilo?.pathExists != true)
            Button("Open Selected Service Log") {
                if let service = selectedService { store.openLog(service.logPath) }
            }
            .disabled(selectedService == nil)
        }
        // Preserve native Cut/Copy/Paste/Select All for the focused text field.
        CommandGroup(after: .pasteboard) {
            Button("Copy Selected Item") {
                if let text = store.selectionText { store.copy(text, note: "Copied selection") }
            }
            .keyboardShortcut("c", modifiers: [.command, .shift])
            .disabled(store.selectionText == nil)
            Button("Copy MCP Endpoint") { store.copy(store.config.mcpURL, note: "Copied MCP endpoint") }
        }
        SidebarCommands()
        CommandGroup(after: .sidebar) {
            Toggle("Show Toolbar Labels", isOn: $store.showToolbarLabels)
            Divider()
            ForEach(SidebarItem.allCases) { item in
                Button(item.title) { AppDelegate.showMainWindow(); store.page = item }
                    .keyboardShortcut(KeyEquivalent(item.shortcut), modifiers: .command)
            }
            Divider()
            Button(store.showInspector ? "Hide Details" : "Show Details") { store.showInspector.toggle() }
                .keyboardShortcut("i", modifiers: [.command, .option])
                .disabled(store.page == .overview)
            Divider()
            Button("Zoom In") { store.zoomIn() }.keyboardShortcut("=", modifiers: .command)
                .disabled(!store.canZoomIn)
            Button("Zoom Out") { store.zoomOut() }.keyboardShortcut("-", modifiers: .command)
                .disabled(!store.canZoomOut)
            Button("Actual Size") { store.zoomReset() }.keyboardShortcut("0", modifiers: .command)
                .disabled(store.zoomStep == 0)
            Divider()
            Button("Refresh") { Task { await store.refresh() } }.keyboardShortcut("r", modifiers: .command)
                .disabled(store.isRefreshing)
        }
        CommandMenu("Silo") {
            Button("Reindex Selected Silo") { if let silo = selectedSilo { store.reindex(silo) } }
                .disabled(selectedSilo.map { !$0.pathExists || store.activeJob(for: $0) != nil } ?? true)
            Button(selectedSilo?.isPrivate == true ? "Make Shared…" : "Make Private") {
                if let silo = selectedSilo { store.setPrivate(silo, !silo.isPrivate) }
            }.disabled(selectedSilo == nil)
            Button("Enable Image Vision…") { if let silo = selectedSilo { store.enableImageVision(silo) } }
                .disabled(selectedSilo.map { $0.imageVision || !$0.pathExists || store.activeJob(for: $0) != nil } ?? true)
        }
        CommandGroup(before: .windowList) {
            Button("Show llmLibrarian") { AppDelegate.showMainWindow() }
                .keyboardShortcut("l", modifiers: [.command, .shift])
        }
        CommandGroup(replacing: .help) {
            Button("llmLibrarian Guide") { openDocumentation("docs/GUIDE.md") }
                .keyboardShortcut("?", modifiers: .command)
            Button("Setup and Troubleshooting") { openDocumentation("docs/CHROMA_AND_STACK.md") }
            Button("CLI Reference") { openDocumentation("docs/cli-for-agents.md") }
            Divider()
            Button("Report an Issue…") {
                NSWorkspace.shared.open(URL(string: "https://github.com/Phasm22/llmLibrarian/issues/new")!)
            }
        }
    }

    private func openDocumentation(_ path: String) {
        NSWorkspace.shared.open(URL(string: "https://github.com/Phasm22/llmLibrarian/blob/dev/\(path)")!)
    }
}

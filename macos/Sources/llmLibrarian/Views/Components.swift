import SwiftUI

// MARK: zoom (⌘+ / ⌘−): macOS ignores dynamic type, so scale fonts ourselves.

private struct ZoomKey: EnvironmentKey { static let defaultValue: CGFloat = 1 }
extension EnvironmentValues {
    var zoom: CGFloat {
        get { self[ZoomKey.self] }
        set { self[ZoomKey.self] = newValue }
    }
}

extension Font {
    static func baseSize(_ style: Font.TextStyle) -> CGFloat {
        switch style {
        case .largeTitle: return 26
        case .title: return 22
        case .title2: return 17
        case .title3: return 15
        case .headline: return 13
        case .body: return 13
        case .callout: return 12
        case .subheadline: return 11
        case .footnote: return 10
        case .caption: return 10
        case .caption2: return 10
        @unknown default: return 13
        }
    }
    static func scaled(_ style: Font.TextStyle, zoom: CGFloat, weight: Font.Weight? = nil, design: Font.Design = .default) -> Font {
        let w = weight ?? (style == .headline ? .semibold : .regular)
        return .system(size: (baseSize(style) * zoom).rounded(), weight: w, design: design)
    }
}

private struct ZFont: ViewModifier {
    @Environment(\.zoom) private var zoom
    let size: CGFloat
    let weight: Font.Weight
    let design: Font.Design
    func body(content: Content) -> some View {
        content.font(.system(size: (size * zoom).rounded(), weight: weight, design: design))
    }
}

extension View {
    func zfont(_ style: Font.TextStyle = .body, weight: Font.Weight? = nil, design: Font.Design = .default) -> some View {
        modifier(ZFont(size: Font.baseSize(style), weight: weight ?? (style == .headline ? .semibold : .regular), design: design))
    }
    func zfont(size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default) -> some View {
        modifier(ZFont(size: size, weight: weight, design: design))
    }
}

enum Theme {
    static let accent = Color(red: 0.85, green: 0.47, blue: 0.34)   // warm terracotta

    static func color(for state: HealthState) -> Color {
        switch state {
        case .running: return .green
        case .attention: return .orange
        case .stopped: return .red
        case .unknown: return .secondary
        }
    }
}

struct StatusDot: View {
    let state: HealthState
    var size: CGFloat = 8
    var body: some View {
        Circle()
            .fill(Theme.color(for: state))
            .frame(width: size, height: size)
            .overlay(Circle().strokeBorder(.black.opacity(0.08)))
    }
}

/// Native grouped section: bold header line, then content in a GroupBox.
struct Section<Content: View>: View {
    let title: String
    var detail: String? = nil
    var trailing: AnyView? = nil
    @ViewBuilder var content: Content

    init(_ title: String, detail: String? = nil, @ViewBuilder content: () -> Content) {
        self.title = title
        self.detail = detail
        self.content = content()
    }

    init<T: View>(_ title: String, detail: String? = nil, @ViewBuilder trailing: () -> T, @ViewBuilder content: () -> Content) {
        self.title = title
        self.detail = detail
        self.trailing = AnyView(trailing())
        self.content = content()
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(title).zfont(.headline)
                if let detail {
                    Text(detail).zfont(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                trailing
            }
            GroupBox {
                content
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }
}

/// Compact metric: label, big number, one-line note.
struct StatTile: View {
    let title: String
    let value: String
    var footnote: String? = nil
    var state: HealthState? = nil

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: 2) {
                HStack(spacing: 5) {
                    if let state { StatusDot(state: state, size: 7) }
                    Text(title).zfont(.caption).foregroundStyle(.secondary)
                }
                Text(value).zfont(.title2, weight: .semibold).monospacedDigit()
                if let footnote {
                    Text(footnote).zfont(.caption).foregroundStyle(.tertiary).lineLimit(1)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct KeyValueRow: View {
    let key: String
    let value: String
    var mono = false
    var width: CGFloat = 110
    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 8) {
            Text(key).foregroundStyle(.secondary).frame(width: width, alignment: .trailing)
            Text(value)
                .zfont(.callout, design: mono ? .monospaced : .default)
                .textSelection(.enabled)
                .lineLimit(2)
                .truncationMode(.middle)
            Spacer(minLength: 0)
        }
        .zfont(.callout)
    }
}

/// Inspector-style field: label left, value right-aligned.
struct DetailRow: View {
    let key: String
    let value: String
    init(_ key: String, _ value: String) { self.key = key; self.value = value }
    var body: some View {
        HStack(alignment: .firstTextBaseline) {
            Text(key).foregroundStyle(.secondary)
            Spacer(minLength: 12)
            Text(value).monospacedDigit().textSelection(.enabled).multilineTextAlignment(.trailing)
        }
        .zfont(.callout)
    }
}

struct InspectorHeading: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Text(text).zfont(.caption, weight: .semibold).foregroundStyle(.secondary).textCase(.uppercase)
            .padding(.top, 6)
    }
}

struct EmptyState: View {
    let symbol: String
    let title: String
    let detail: String
    var body: some View {
        VStack(spacing: 10) {
            Image(systemName: symbol)
                .zfont(size: 34, weight: .light)
                .foregroundStyle(.tertiary)
            Text(title).zfont(.headline)
            Text(detail)
                .zfont(.callout)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 420)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .padding(40)
    }
}

struct NothingSelected: View {
    let text: String
    var body: some View {
        VStack(spacing: 8) {
            Image(systemName: "sidebar.trailing").zfont(.title2).foregroundStyle(.tertiary)
            Text(text).zfont(.callout).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

/// Page body: full width, modest margins, scrolls.
struct Page<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) { content }
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

struct JobOutput: View {
    @ObservedObject var job: Job
    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                Text(job.output.isEmpty ? "Starting…" : job.output)
                    .zfont(size: 11, design: .monospaced)
                    .textSelection(.enabled)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .id("end")
            }
            .frame(height: 140)
            .padding(8)
            .background(Color.primary.opacity(0.04), in: RoundedRectangle(cornerRadius: 6))
            .onChange(of: job.output) { _, _ in proxy.scrollTo("end", anchor: .bottom) }
        }
    }
}

extension ServiceStatus {
    var kindText: String {
        switch kind {
        case .chroma: return "Vector store"
        case .mcp: return "MCP server"
        case .watcher: return "Watcher"
        }
    }
    var memoryText: String { memoryMB.map { String(format: "%.0f MB", $0) } ?? "—" }
    var endpointText: String {
        if kind == .watcher { return "—" }
        guard let ok = probeOK else { return "—" }
        return ok ? "Responding" : "No response"
    }
}


/// The two ways out of a silo whose folder is gone. Used on the Overview issue
/// row, both inspectors, and the error dialog, so the fix is wherever the problem is.
struct SiloFixButtons: View {
    @EnvironmentObject private var store: LibrarianStore
    let silo: Silo
    var compact = false
    @State private var confirmRemove = false

    var body: some View {
        Button(compact ? "Locate…" : "Locate Folder…") { store.relocate(silo) }
            .help("Choose the folder's new location; it is indexed as a new silo and this one is removed")
        Button(compact ? "Remove…" : "Remove Silo…") { confirmRemove = true }
            .help("Delete this silo's \(silo.chunks.grouped) chunks, its bookmark, and its watcher")
            .confirmationDialog("Remove \(silo.displayName)?", isPresented: $confirmRemove, titleVisibility: .visible) {
                Button("Remove Silo", role: .destructive) { store.removeSilo(silo) }
            } message: {
                Text("Deletes \(silo.chunks.grouped) indexed chunks for \(silo.tildePath), the bookmark, and the watcher job. The folder itself is not touched.")
            }
    }
}

/// Error dialog with the fix attached, driven by `store.problem`.
struct ProblemAlert: ViewModifier {
    @EnvironmentObject private var store: LibrarianStore
    @State private var confirmRemove = false

    func body(content: Content) -> some View {
        content
            .alert(store.problem?.title ?? "", isPresented: Binding(get: { store.problem != nil }, set: { if !$0 { store.problem = nil } }),
                   presenting: store.problem) { p in
                if let silo = p.silo, p.folderMissing {
                    Button("Locate Folder…") { store.relocate(silo) }
                    Button("Remove Silo…") { lastProblemSilo = silo; store.problem = nil; confirmRemove = true }
                }
                if let log = p.logPath, FileManager.default.fileExists(atPath: log) {
                    Button("Open Log") { store.problem = nil; store.openLog(log) }
                }
                if let s = p.service {
                    Button("Show Service") { store.problem = nil; store.show(service: s) }
                }
                Button("OK", role: .cancel) { store.problem = nil }
            } message: { p in
                Text(p.message)
            }
            .confirmationDialog("Remove \(pendingSilo?.displayName ?? "silo")?", isPresented: $confirmRemove, titleVisibility: .visible, presenting: pendingSilo) { silo in
                Button("Remove Silo", role: .destructive) { store.removeSilo(silo) }
            } message: { silo in
                Text("Deletes \(silo.chunks.grouped) indexed chunks for \(silo.tildePath), the bookmark, and the watcher job. The folder itself is not touched.")
            }
            .confirmationDialog(
                "Make \(store.pendingUnprivate?.displayName ?? "silo") shared?",
                isPresented: Binding(get: { store.pendingUnprivate != nil },
                                     set: { if !$0 { store.pendingUnprivate = nil } }),
                titleVisibility: .visible,
                presenting: store.pendingUnprivate
            ) { silo in
                Button("Make Shared", role: .destructive) { store.applyPrivate(silo, false) }
                Button("Cancel", role: .cancel) { store.pendingUnprivate = nil }
            } message: { silo in
                Text("Any unscoped query will be able to return chunks from \(silo.tildePath) — including a query issued by a cloud model over MCP. \(silo.chunks.grouped) chunks become reachable without anyone naming the silo.")
            }
    }

    private var pendingSilo: Silo? { lastProblemSilo }
    @State private var lastProblemSilo: Silo?
}


/// Inline warning: one orange icon, calm secondary text. No red text anywhere.
struct Warning: View {
    let text: String
    init(_ text: String) { self.text = text }
    var body: some View {
        Label {
            Text(text).foregroundStyle(.secondary)
        } icon: {
            Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
        }
        .zfont(.caption)
    }
}

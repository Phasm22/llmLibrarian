import Foundation

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}

let folder = URL(fileURLWithPath: "/tmp/Research Notes", isDirectory: true)
let jobs = SiloAddWorkflow.jobs(for: folder)

expect(jobs == [
    PalJobSpec(title: "Index Research Notes", siloSlug: nil,
               arguments: ["pull", "/tmp/Research Notes"]),
    PalJobSpec(title: "Start watcher for Research Notes", siloSlug: nil,
               arguments: ["daemon", "sync"]),
], "The add workflow must index the selected folder before synchronizing watcher services")

let trailingSlash = URL(fileURLWithPath: "/tmp/Research Notes/", isDirectory: true)
expect(SiloAddWorkflow.normalizedPath(for: trailingSlash) == "/tmp/Research Notes",
       "Folder paths should be normalized")
expect(SiloAddWorkflow.jobs(for: trailingSlash)[0].arguments.count == 2,
       "A path containing spaces must remain one Process argument")

let multi = SiloAddWorkflow.jobs(for: [
    URL(fileURLWithPath: "/tmp/Alpha", isDirectory: true),
    URL(fileURLWithPath: "/tmp/Beta", isDirectory: true),
])
expect(multi.map(\.arguments) == [
    ["pull", "/tmp/Alpha"],
    ["pull", "/tmp/Beta"],
    ["daemon", "sync"],
], "Finder multi-selections must index sequentially and synchronize watchers once")

let vision = SiloAddWorkflow.jobs(for: folder, imageVision: true)
expect(vision[0].arguments == ["pull", "/tmp/Research Notes", "--image-vision"],
       "Image vision must be forwarded to pal as an explicit opt-in")
expect(vision[1].arguments == ["daemon", "sync"],
       "Image vision must not change watcher reconciliation")

print("SiloAddWorkflowTests: passed")

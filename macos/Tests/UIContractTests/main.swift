import Foundation

func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else { fatalError(message) }
}
let unknown = AppBuildInfo(info: [:])
expect(unknown.date == nil && unknown.state == "unknown", "Unstamped builds must not claim to be current or clean")
let stamped = AppBuildInfo(info: ["LibrarianBuildDate": "2026-11-11T21:00:00Z", "LibrarianBuildCommit": "abc123",
                                "LibrarianBuildState": "modified", "LibrarianBuildSource": "/tmp/Checkout with spaces"])
expect(stamped.date != nil && stamped.report.contains("abc123") && stamped.report.contains("uncommitted"),
       "Build reports must distinguish local edits from a clean commit")
expect(stamped.source == "/tmp/Checkout with spaces", "Build provenance must preserve the full checkout path")
expect(AppBuildInfo(info: ["LibrarianBuildDate": "invalid"]).date == nil, "Invalid timestamps must remain unknown")

let imageFailure = ActivityFailureRow(id: 1, slug: "photos", path: "/tmp/test photos/IMG.jpeg", errorClass: "other",
                                    error: "image model not ready", lastSeen: Date(), occurrences: 1953)
expect(imageFailure.remedy == .imageModel, "The actionable image error must not be buried in other errors")
expect(imageFailure.matches("IMG") && imageFailure.matches("MODEL") && !imageFailure.matches("missing pdf"),
       "Problems filtering must search paths and errors, case insensitively")
expect(FailureRemedy.classify(errorClass: "other", error: "error") == .unknown,
       "A generic error must not invent a cause")
expect(FailureRemedy.classify(errorClass: "encrypted", error: "locked") == .encrypted,
       "Protected files need an unlock action, not blind retry")
for kind in ["encrypted", "too_large", "unsupported", "timeout", "permission", "no_text", "busy", "not_found", "other"] {
    expect(!FailureRemedy.classify(errorClass: kind, error: "").nextStep.isEmpty, "Every problem must name a next step")
}
print("UI contract tests passed")

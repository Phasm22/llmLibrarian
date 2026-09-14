// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "llmLibrarian",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "llmLibrarian",
            path: "Sources/llmLibrarian"
        )
    ]
)

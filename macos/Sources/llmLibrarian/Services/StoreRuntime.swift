import Foundation

enum ServiceCommand: Sendable { case start, stop, restart }

/// Internal dependency seam for exercising real UI flows against temporary
/// folders and simulated service responses, without touching launchd or the index.
struct StoreRuntime {
    var config: () -> PalConfig
    var collect: @Sendable (PalConfig, ActivityWindow) async -> Snapshot
    var serviceCommand: @Sendable (ServiceCommand, ServiceStatus, PalConfig) -> Shell.Result
    var settleDelays: [Duration] = [.milliseconds(600), .seconds(2), .seconds(3)]

    static var live: Self {
        Self(config: PalConfig.load, collect: { await Collector.collect(config: $0, window: $1) },
             serviceCommand: { command, service, _ in
                 switch command {
                 case .stop: return Launchctl.bootout(service.label)
                 case .restart: return Launchctl.kickstart(service.label)
                 case .start: return service.loaded ? Launchctl.kickstart(service.label) : Launchctl.bootstrap(plist: service.plistPath)
                 }
             })
    }
}

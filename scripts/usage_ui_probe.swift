import SwiftUI
import AppKit

/// Renders the production native view in its own process. No engine, microphone,
/// preferences, credentials, network calls, event taps or installed app access.
@main enum UsageUIProbe {
    @MainActor static func main() throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        let mode = CommandLine.arguments.count > 2 ? CommandLine.arguments[2] : "normal"
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let url = root.appendingPathComponent("\(mode)-profile/usage.json")
        let store = UsageMetricsStore(url: url)
        if mode != "empty" && store.snapshot().archive.dictations.isEmpty {
            let now = Date()
            let providers = ["parakeet", "whisper", "cloudflare", "huggingface", "openrouter"]
            for i in 0..<20 {
                let provider = providers[i % providers.count]
                let at = Calendar.current.date(byAdding: .day, value: -(i * 2), to: now)!
                let id = UUID()
                let dictation = UsageDictation(id: id, recordedAt: at, originalSeconds: Double(90 + i * 15), engine: provider, provider: provider, model: "sample-model")
                let run = store.begin(dictation, at: at, engine: provider, provider: provider, model: "sample-model")
                store.finishRun(run, outcome: i == 9 ? "failure" : "success", at: at)
                if provider == "parakeet" || provider == "whisper" { continue }
                let request = UsageRequest(id: UUID(), dictationID: id, runID: run, startedAt: at, provider: provider, model: "sample-model", phase: "transcription", uploadedSeconds: Double(90 + i * 15) / 1.5)
                store.beginRequest(request)
                let cost = provider == "openrouter" ? UsageCost(kind: .actual, usd: 0.002 * Double(i + 1)) : provider == "cloudflare" ? UsageCost.audioEstimate(provider: provider, model: "@cf/deepgram/nova-3", seconds: request.uploadedSeconds) : UsageCost()
                store.finishRequest(request.id, outcome: "httpSuccess", cost: cost, at: at)
                if i == 4 {
                    let retry = store.begin(dictation, at: at.addingTimeInterval(10), engine: provider, provider: provider, model: "sample-model")
                    let cleanup = UsageRequest(id: UUID(), dictationID: id, runID: retry, startedAt: at.addingTimeInterval(10), provider: provider, model: "sample-cleanup", phase: "cleanup", uploadedSeconds: nil)
                    store.beginRequest(cleanup)
                    store.finishRequest(cleanup.id, outcome: "httpSuccess", cost: UsageCost(kind: .actual, usd: 0.015), at: at.addingTimeInterval(11))
                    store.finishRun(retry, outcome: "success", at: at.addingTimeInterval(11))
                }
            }
        }
        let application = NSApplication.shared
        application.setActivationPolicy(.accessory)
        let width: CGFloat = mode == "narrow" ? 460 : 800
        let window = NSWindow(contentRect: NSRect(x: 100, y: 100, width: width, height: 840),
                              styleMask: [.titled, .closable, .resizable], backing: .buffered, defer: false)
        window.title = "OSW Cloud usage UI probe, isolated sample profile"
        let view = UsageDashboard(store: store, initialProvider: mode == "filtered" ? "openrouter" : "all",
                                  initialPeriod: mode == "monthly" ? .monthly : mode == "weekly" ? .weekly : .daily,
                                  initialRange: mode == "monthly" ? "all" : "30")
        let host = NSHostingView(rootView: view)
        window.contentView = host
        window.makeKeyAndOrderFront(nil)
        DispatchQueue.main.asyncAfter(deadline: .now() + 3) {
            host.layoutSubtreeIfNeeded()
            guard let bitmap = host.bitmapImageRepForCachingDisplay(in: host.bounds) else { exit(1) }
            host.cacheDisplay(in: host.bounds, to: bitmap)
            guard let data = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
            do { try data.write(to: root.appendingPathComponent("\(mode).png")) }
            catch { print(error); exit(1) }
            print("Rendered \(mode) native window \(Int(width))x840 from production UsageDashboard.swift")
            window.close()
            application.terminate(nil)
        }
        application.run()
    }
}

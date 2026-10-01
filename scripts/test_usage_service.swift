import SwiftUI
import Foundation

// Only external engines/preferences are doubled. TranscriptionService is
// compiled verbatim from the generated app and writes to an injected profile.
struct Settings {}
@MainActor final class AppPreferences {
    static let shared = AppPreferences()
    var selectedEngine = "cloudflare"
    var selectedWhisperModelPath: String?
}
struct UsageSelection {
    let engine: String
    let provider: String
    let model: String
    @MainActor static func capture(engine: String) -> Self {
        Self(engine: engine, provider: engine == "cloudflare" ? "openrouter" : engine, model: "sample-model")
    }
}
protocol TranscriptionEngine: AnyObject {
    func initialize() async throws
    func transcribeAudio(url: URL, settings: Settings) async throws -> String
    func cancelTranscription()
}
actor EngineConcurrency {
    var active = 0
    var maximum = 0
    func enter() { active += 1; maximum = max(maximum, active) }
    func leave() { active -= 1 }
}
class FakeEngine: TranscriptionEngine {
    static let concurrency = EngineConcurrency()
    var onProgressUpdate: ((Float) -> Void)?
    static var fail = false
    func initialize() async throws {}
    func cancelTranscription() {}
    func transcribeAudio(url: URL, settings: Settings) async throws -> String {
        await Self.concurrency.enter()
        try await Task.sleep(nanoseconds: 30_000_000)
        await Self.concurrency.leave()
        if self is CloudflareEngine {
            let seconds = await UsageTracking.audioSeconds(url)
            _ = try await UsageTracking.send(provider: "openrouter", model: "sample-model", phase: "transcription", seconds: seconds.map { $0 / 1.5 }) {
                (Data(#"{"usage":{"cost":0.02}}"#.utf8), HTTPURLResponse(url: URL(string: "https://invalid.test")!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
            }
        }
        if Self.fail { throw NSError(domain: "sample", code: 1) }
        return "Not saved to metrics"
    }
}
final class CloudflareEngine: FakeEngine {}
final class WhisperEngine: FakeEngine {}
final class FluidAudioEngine: FakeEngine {}

@main enum UsageServiceTests {
    @MainActor static func main() async throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1])
        let store = UsageMetricsStore(url: root.appendingPathComponent("service-profile/usage.json"))
        let service = TranscriptionService(metricsStore: store)
        while service.isLoading { try await Task.sleep(nanoseconds: 10_000_000) }
        let wav = root.appendingPathComponent("service-sample.wav")
        // 3 seconds of 16kHz PCM. No microphone or provider calls.
        var data = Data()
        func text(_ text: String) { data.append(contentsOf: text.utf8) }
        func n32(_ n: Int) { data.append(contentsOf: (0..<4).map { UInt8((n >> (8 * $0)) & 255) }) }
        func n16(_ n: Int) { data.append(contentsOf: (0..<2).map { UInt8((n >> (8 * $0)) & 255) }) }
        text("RIFF"); n32(36 + 96000); text("WAVEfmt "); n32(16); n16(1); n16(1)
        n32(16000); n32(32000); n16(2); n16(16); text("data"); n32(96000); data.append(Data(count: 96000))
        try data.write(to: wav)
        let id = UUID()
        let recordedAt = Date(timeIntervalSince1970: 1_700_000_000)
        FakeEngine.fail = true
        _ = try? await service.transcribeAudio(url: wav, settings: Settings(), metricID: id, recordedAt: recordedAt)
        await Task.yield()
        try await Task.sleep(nanoseconds: 10_000_000)
        FakeEngine.fail = false
        _ = try await service.transcribeAudio(url: wav, settings: Settings(), metricID: id, recordedAt: recordedAt)
        await Task.yield()
        let snapshot = store.snapshot().archive
        precondition(snapshot.dictations.count == 1 && snapshot.runs.count == 2 && snapshot.requests.count == 2)
        precondition(snapshot.dictations[0].originalSeconds == 3 && snapshot.dictations[0].recordedAt == recordedAt)
        precondition(snapshot.requests.allSatisfy { $0.uploadedSeconds == 2 && $0.dictationID == id })
        precondition(snapshot.runs[0].outcome == "failure" && snapshot.runs[1].outcome == "success")
        print("PASS actual service captures 3 original seconds before 1.5x upload, timestamps, failures and retry ID, no repeated minutes")
        AppPreferences.shared.selectedEngine = "fluidaudio"
        let local = TranscriptionService(metricsStore: store)
        while local.isLoading { try await Task.sleep(nanoseconds: 10_000_000) }
        _ = try await local.transcribeAudio(url: wav, settings: Settings(), metricID: UUID(), recordedAt: recordedAt)
        let final = store.snapshot().archive
        precondition(final.dictations.count == 2 && final.dictations[1].originalSeconds == 3 && final.requests.count == 2)
        precondition(final.dictations[1].provider == "fluidaudio" && final.runs.last?.outcome == "success")
        print("PASS actual service local inference adds original duration and no cloud API request")
        async let first = service.transcribeAudio(url: wav, settings: Settings(), metricID: UUID(), recordedAt: recordedAt)
        async let second = service.transcribeAudio(url: wav, settings: Settings(), metricID: UUID(), recordedAt: recordedAt)
        _ = try await (first, second)
        let maximum = await FakeEngine.concurrency.maximum
        precondition(maximum == 1 && store.snapshot().archive.dictations.count == 4)
        print("PASS original-duration await does not let concurrent queue/indicator decodes overlap")
    }
}

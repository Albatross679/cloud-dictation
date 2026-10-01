import Foundation
import AVFoundation

final class UsageHTTPStub: URLProtocol {
    static var responses: [(Int, String)] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard !Self.responses.isEmpty else { client?.urlProtocol(self, didFailWithError: URLError(.networkConnectionLost)); return }
        let (status, body) = Self.responses.removeFirst()
        client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main enum UsageMetricsTests {
    static func check(_ name: String, _ value: Bool) { if !value { fatalError("FAIL \(name)") }; print("PASS \(name)") }
    static func date(_ value: String) -> Date { ISO8601DateFormatter().date(from: value)! }
    static func main() async throws {
        let root = URL(fileURLWithPath: CommandLine.arguments[1]).appendingPathComponent("metrics-\(UUID())")
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let url = root.appendingPathComponent("profile/usage.json")
        let store = UsageMetricsStore(url: url)
        let id = UUID()
        let original = UsageDictation(id: id, recordedAt: date("2026-03-08T06:59:00Z"), originalSeconds: 120,
                                      engine: "whisper", provider: "whisper", model: "ggml-tiny.en.bin")
        let run = store.begin(original, at: date("2026-03-08T07:00:00Z"), engine: "whisper", provider: "whisper", model: original.model)
        store.finishRun(run, outcome: "failure")
        store.finishRun(run, outcome: "success")
        let retry = store.begin(original, at: date("2026-03-09T04:00:00Z"), engine: "cloudflare", provider: "openrouter", model: "openai/whisper-large-v3-turbo")
        let requestID = UUID()
        let request = UsageRequest(id: requestID, dictationID: id, runID: retry, startedAt: date("2026-03-09T04:00:00Z"), provider: "openrouter", model: "cleanup-model", phase: "cleanup", uploadedSeconds: nil)
        store.beginRequest(request); store.beginRequest(request)
        store.finishRequest(requestID, outcome: "httpSuccess", cost: UsageCost(kind: .actual, usd: 0.012))
        store.finishRequest(requestID, outcome: "httpSuccess", cost: UsageCost(kind: .actual, usd: 100))
        let pending = UsageRequest(id: UUID(), dictationID: id, runID: retry, startedAt: date("2026-03-10T03:59:59Z"), provider: "huggingface", model: "openai/whisper-large-v3", phase: "transcription", uploadedSeconds: 60)
        store.beginRequest(pending)
        let estimated = UsageRequest(id: UUID(), dictationID: id, runID: retry, startedAt: date("2026-03-10T04:00:00Z"), provider: "cloudflare", model: "@cf/deepgram/nova-3", phase: "transcription", uploadedSeconds: 60)
        store.beginRequest(estimated)
        store.finishRequest(estimated.id, outcome: "httpSuccess", cost: .audioEstimate(provider: "cloudflare", model: estimated.model, seconds: 60))
        let restart = UsageMetricsStore(url: url).snapshot()
        check("restart, duplicate begins/completions and retry identity", restart.archive.dictations.count == 1 && restart.archive.runs.count == 2 && restart.archive.requests.count == 3 && restart.archive.runs.first?.outcome == "failure")
        check("restart retains interrupted requests as unknown", restart.archive.requests[1].outcome == "inFlight" && restart.archive.requests[1].cost.kind == .unknown)
        let permissions = try FileManager.default.attributesOfItem(atPath: url.path)[.posixPermissions] as! NSNumber
        let directoryPermissions = try FileManager.default.attributesOfItem(atPath: url.deletingLastPathComponent().path)[.posixPermissions] as! NSNumber
        check("private file and directory", permissions.intValue == 0o600 && directoryPermissions.intValue == 0o700)
        let raw = try String(contentsOf: url, encoding: .utf8)
        check("metrics have no paths or transcript/key/body fields", !raw.contains(root.path) && !raw.contains("transcription\":") && !raw.contains("Authorization") && !raw.contains("fake-secret") && !raw.contains("audioData"))
        var calendar = Calendar(identifier: .gregorian)
        calendar.timeZone = TimeZone(identifier: "America/New_York")!
        calendar.firstWeekday = 2
        let start = date("2026-03-08T05:00:00Z"), end = date("2026-03-11T04:00:00Z")
        let buckets = UsageAggregation.buckets(restart.archive, start: start, end: end, period: .daily, calendar: calendar)
        check("spring DST uses 23-hour calendar day", buckets.count == 3 && buckets[1].start.timeIntervalSince(buckets[0].start) == 23 * 3600)
        check("original local minutes counted once, including failed run", buckets[0].minutes == 2 && buckets.reduce(0) { $0 + $1.minutes } == 2)
        check("cost uses request date including cleanup-only cost", buckets[1].actualUSD == 0.012 && buckets[1].unknownCosts == 1 && buckets[2].estimatedUSD == 0.0052)
        let filtered = UsageAggregation.buckets(restart.archive, start: start, end: end, period: .daily, provider: "openrouter", calendar: calendar)
        check("cost filter independent of first dictation provider", filtered.reduce(0) { $0 + $1.minutes } == 0 && filtered[1].actualUSD == 0.012 && filtered.reduce(0) { $0 + $1.unknownCosts } == 0)
        let local = UsageAggregation.buckets(restart.archive, start: start, end: end, period: .daily, provider: "whisper", calendar: calendar)
        check("local inference minutes with no cloud charge", local[0].minutes == 2 && local.reduce(0) { $0 + $1.requests } == 0)
        for period in [UsagePeriod.weekly, .monthly] {
            let grouped = UsageAggregation.buckets(restart.archive, start: start, end: end, period: period, calendar: calendar)
            check("\(period) totals", grouped.reduce(0) { $0 + $1.minutes } == 2 && grouped.reduce(0) { $0 + $1.actualUSD } == 0.012 && grouped.reduce(0) { $0 + $1.unknownCosts } == 1)
        }
        let fall = UsageAggregation.buckets(UsageArchive(), start: date("2026-11-01T04:00:00Z"), end: date("2026-11-03T05:00:00Z"), period: .daily, calendar: calendar)
        check("fall DST uses 25-hour calendar day", fall.count == 2 && fall[1].start.timeIntervalSince(fall[0].start) == 25 * 3600)
        check("empty and reversed ranges", UsageAggregation.buckets(UsageArchive(), start: end, end: start, period: .daily).isEmpty)
        check("unverified tiny-en rate remains unknown", UsageCost.audioEstimate(provider: "cloudflare", model: "@cf/openai/whisper-tiny-en", seconds: 60).kind == .unknown)
        for body in [#"{"usage":{"cost":null}}"#, #"{"usage":{"cost":true}}"#, #"{"usage":{"cost":-1}}"#, #"{"usage":{"cost":"0.1"}}"#, "{}"] {
            check("missing/invalid reported charges stay unknown", UsageCost.reported(Data(body.utf8), provider: "openrouter") == nil)
        }
        check("explicit provider zero is known actual", UsageCost.reported(Data(#"{"usage":{"cost":0}}"#.utf8), provider: "openrouter")?.kind == .actual)
        let corrupt = root.appendingPathComponent("corrupt.json")
        try Data("broken json".utf8).write(to: corrupt)
        let damaged = UsageMetricsStore(url: corrupt)
        _ = damaged.begin(original, engine: "whisper", provider: "whisper", model: "tiny")
        check("corrupt file preserved with visible error", try String(contentsOf: corrupt, encoding: .utf8) == "broken json" && damaged.snapshot().error != nil)
        let blocked = root.appendingPathComponent("not-a-directory")
        try Data().write(to: blocked)
        let unavailable = UsageMetricsStore(url: blocked.appendingPathComponent("usage.json"))
        _ = unavailable.begin(original, engine: "whisper", provider: "whisper", model: "tiny")
        check("write failures visible, no phantom persisted history", unavailable.snapshot().error != nil && unavailable.snapshot().archive.dictations.isEmpty)

        try FileManager.default.removeItem(at: blocked)
        unavailable.finishRun(UUID(), outcome: "failure")
        check("incomplete totals warning survives repair and restart", UsageMetricsStore(url: blocked.appendingPathComponent("usage.json")).snapshot().error != nil)
        var boundaryArchive = UsageArchive()
        boundaryArchive.dictations = [
            UsageDictation(id: UUID(), recordedAt: date("2026-12-31T23:59:59Z"), originalSeconds: 60, engine: "fluidaudio", provider: "parakeet", model: "v3"),
            UsageDictation(id: UUID(), recordedAt: date("2027-01-01T00:00:00Z"), originalSeconds: nil, engine: "whisper", provider: "whisper", model: "tiny")]
        var utc = Calendar(identifier: .gregorian); utc.timeZone = TimeZone(secondsFromGMT: 0)!
        let yearBuckets = UsageAggregation.buckets(boundaryArchive, start: date("2026-12-01T00:00:00Z"), end: date("2027-02-01T00:00:00Z"), period: .monthly, calendar: utc)
        check("month/year boundaries and missing duration", yearBuckets.count == 2 && yearBuckets[0].minutes == 1 && yearBuckets[1].unknownDurations == 1)
        let exclusive = UsageAggregation.buckets(boundaryArchive, start: date("2026-12-31T00:00:00Z"), end: date("2027-01-01T00:00:00Z"), period: .daily, calendar: utc)
        check("half-open range includes start and excludes end", exclusive.count == 1 && exclusive[0].dictations == 1)
        let changedRoute = UsageMetricsStore(url: root.appendingPathComponent("changed-route/usage.json"))
        let changingID = UUID()
        let changingRun = changedRoute.begin(UsageDictation(id: changingID, recordedAt: start, originalSeconds: 60, engine: "cloudflare", provider: "cloudflare", model: "selected-before-await"), engine: "cloudflare", provider: "cloudflare", model: "selected-before-await")
        changedRoute.beginRequest(UsageRequest(id: UUID(), dictationID: changingID, runID: changingRun, startedAt: start, provider: "openrouter", model: "actual-encoded-model", phase: "transcription", uploadedSeconds: 40))
        check("first cloud route follows encoded request if settings change", changedRoute.snapshot().archive.dictations[0].provider == "openrouter" && changedRoute.snapshot().archive.runs[0].model == "actual-encoded-model")
        let concurrent = UsageMetricsStore(url: root.appendingPathComponent("concurrent/usage.json"))
        DispatchQueue.concurrentPerform(iterations: 12) { index in
            let record = UsageDictation(id: UUID(), recordedAt: Date(timeIntervalSince1970: Double(1700000000 + index)), originalSeconds: 60, engine: "whisper", provider: "whisper", model: "tiny")
            let run = concurrent.begin(record, engine: "whisper", provider: "whisper", model: "tiny")
            concurrent.finishRun(run, outcome: "success")
        }
        check("concurrent atomic writes lose no dictations", UsageMetricsStore(url: concurrent.url).snapshot().archive.dictations.count == 12)

        URLProtocol.registerClass(UsageHTTPStub.self)
        defer { URLProtocol.unregisterClass(UsageHTTPStub.self) }
        let wav = root.appendingPathComponent("sample.wav")
        try HuggingFaceClient.silentWAV.write(to: wav)
        check("reads original seconds from actual audio", abs((await UsageTracking.audioSeconds(wav) ?? 0) - 0.25) < 0.001)
        let context = UsageContext(dictationID: id, runID: retry, store: store)
        let before = store.snapshot().archive.requests.count
        try await UsageTracking.$context.withValue(context) {
            UsageHTTPStub.responses = [(200, #"{"text":"PRIVATE SAMPLE","usage":{"cost":0.001}}"#), (200, #"{"choices":[{"message":{"content":"PRIVATE CLEANED"}}],"usage":{"cost":0.002}}"#)]
            _ = try await OpenRouterClient(token: "fake-secret").transcribe(fileURL: wav, query: [URLQueryItem(name: "cleanup", value: "1")])
            UsageHTTPStub.responses = [(503, #"{"error":"unavailable","usage":{"cost":0.003}}"#)]
            _ = try? await OpenRouterClient(token: "fake-secret").transcribe(fileURL: wav, query: [])
            UsageHTTPStub.responses = [(200, #"{"text":"PRIVATE HF"}"#), (503, #"{"error":"failed cleanup"}"#)]
            _ = try await HuggingFaceClient(token: "fake-secret").transcribe(fileURL: wav, query: [URLQueryItem(name: "cleanup", value: "1")])
            UsageHTTPStub.responses = []
            _ = try? await OpenRouterClient(token: "fake-secret").transcribe(fileURL: wav, query: [])
            let cf = CloudflareClient(endpoint: "https://example.invalid", token: "fake-secret", accountID: "fake", mode: .directAPI)
            UsageHTTPStub.responses = [(200, #"{"success":true,"result":{"results":{"channels":[{"alternatives":[{"transcript":"PRIVATE CF"}]}]}}}"#), (200, #"{"success":true,"result":{"response":"PRIVATE CF CLEAN"}}"#)]
            _ = try await cf.transcribe(fileURL: wav, query: [URLQueryItem(name: "cleanup", value: "1")])
            let worker = CloudflareClient(endpoint: "https://example.invalid", token: "fake-secret", accountID: "", mode: .worker)
            UsageHTTPStub.responses = [(200, #"{"text":"PRIVATE WORKER","cleaned":true,"cleanup_ms":5}"#)]
            _ = try await worker.transcribe(fileURL: wav, query: [URLQueryItem(name: "cleanup", value: "1")])
        }
        let attempts = Array(store.snapshot().archive.requests.dropFirst(before))
        check("all production clients record each send and cleanup", attempts.count == 10)
        check("OpenRouter reported transcription, cleanup and failed charge", attempts[0].cost.usd == 0.001 && attempts[1].phase == "cleanup" && attempts[1].cost.usd == 0.002 && attempts[2].outcome == "httpFailure" && attempts[2].cost.usd == 0.003)
        check("HF failed cleanup is unknown, transport retry is distinct", attempts[3].cost.kind == .unknown && attempts[4].phase == "cleanup" && attempts[4].outcome == "httpFailure" && attempts[5].outcome == "transportFailure")
        check("CF direct estimate uses uploaded duration, cleanup unknown", attempts[6].uploadedSeconds == 0.25 && abs(attempts[6].cost.usd! - 0.25 / 60 * 0.0052) < 0.000001 && attempts[7].phase == "cleanup" && attempts[7].cost.kind == .unknown)
        check("Worker observed cleanup has separate unavailable charge", attempts[8].phase == "workerPipeline" && attempts[9].phase == "workerCleanup" && attempts[9].cost.kind == .unknown)
        let final = try String(contentsOf: url, encoding: .utf8)
        check("production response/request private data never persisted", !final.contains("PRIVATE") && !final.contains("fake-secret") && !final.contains("example.invalid") && !final.contains(wav.path))
        check("all stub responses consumed", UsageHTTPStub.responses.isEmpty)
        print("All usage persistence, aggregation and actual client instrumentation checks passed")
    }
}

import Foundation
import AVFoundation

/// Talks to the cloud-dictation worker. Shared by the transcription engine and
/// the Test Connection button so both agree on what reachable means.
struct CloudflareClient {
    let endpoint: String
    let token: String

    enum ClientError: LocalizedError {
        case notConfigured
        case badStatus(Int, String)
        case languageMismatch(requested: String, detected: String)

        var errorDescription: String? {
            switch self {
            case .notConfigured:
                return "Set the worker endpoint and auth token first."
            case let .badStatus(code, body):
                if code == 401 { return "Rejected the auth token (401)." }
                let detail = body.trimmingCharacters(in: .whitespacesAndNewlines)
                return "Worker returned \(code)\(detail.isEmpty ? "" : ": \(detail)")"
            case let .languageMismatch(requested, detected):
                return "Discarded: came back as \(detected) script but the language is set to \(requested). Try a model that can be pinned to a language."
            }
        }
    }

    private func request(_ path: String) throws -> URLRequest {
        let trimmed = endpoint.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !token.isEmpty, let base = URL(string: trimmed) else {
            throw ClientError.notConfigured
        }
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    /// Model keys the worker offers, e.g. ["nova-3", "whisper-turbo"].
    func models() async throws -> [String] {
        try await catalog().map(\.key)
    }

    func catalog() async throws -> [ModelEntry] {
        var req = try request("models")
        req.timeoutInterval = 15

        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw ClientError.badStatus(0, "")
        }
        guard http.statusCode == 200 else {
            throw ClientError.badStatus(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }
        return try JSONDecoder().decode(ModelsResponse.self, from: data).models
    }

    struct ModelEntry: Decodable {
        let key: String
        /// nil when the model accepts any language the client can display.
        let languages: [String]?
    }

    func usage() async throws -> CloudflareUsage {
        var req = try request("usage")
        req.timeoutInterval = 15

        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw ClientError.badStatus(0, "")
        }
        guard http.statusCode == 200 else {
            throw ClientError.badStatus(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }
        return try JSONDecoder().decode(CloudflareUsage.self, from: data)
    }

    func transcribe(fileURL: URL, query: [URLQueryItem]) async throws -> String {
        var req = try request("transcribe")
        var components = URLComponents(url: req.url!, resolvingAgainstBaseURL: false)
        components?.queryItems = query
        req.url = components?.url
        req.httpMethod = "POST"
        req.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
        req.timeoutInterval = 300

        let (data, response) = try await URLSession.shared.upload(for: req, fromFile: fileURL)
        guard let http = response as? HTTPURLResponse else {
            throw ClientError.badStatus(0, "")
        }
        guard http.statusCode == 200 else {
            throw ClientError.badStatus(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }
        let decoded = try JSONDecoder().decode(TranscriptionResponse.self, from: data)
        // Whisper can mis-detect the language on short or noisy audio and hand
        // back fluent text in a script the speaker never used. Pasting that
        // into the focused app is worse than surfacing the failure.
        if let mismatch = decoded.language_mismatch {
            throw ClientError.languageMismatch(
                requested: mismatch.requested,
                detected: mismatch.detected_script
            )
        }
        return decoded.text
    }

    private struct ModelsResponse: Decodable {
        let models: [ModelEntry]
    }

    private struct TranscriptionResponse: Decodable {
        struct Mismatch: Decodable {
            let requested: String
            let detected_script: String
        }
        let text: String
        let language_mismatch: Mismatch?
    }
}

/// AVAudioPlayerNode invokes its completion handler from the render thread.
/// This small lock makes that signal safe to inspect from the upload task.
private final class CloudflarePlaybackCompletion: @unchecked Sendable {
    private let lock = NSLock()
    private var didComplete = false

    func finish() {
        lock.lock()
        didComplete = true
        lock.unlock()
    }

    var isComplete: Bool {
        lock.lock()
        defer { lock.unlock() }
        return didComplete
    }
}

/// Produces a pitch-preserving, time-compressed WAV for cloud upload only.
/// The recorder's file is never changed: callers own and delete the copy.
private enum CloudflareAudioCompressor {
    enum CompressionError: LocalizedError {
        case invalidRate(Double)
        case noAudioProduced
        case stalledRender

        var errorDescription: String? {
            switch self {
            case let .invalidRate(rate):
                return "Invalid Cloudflare audio speed: \(rate)"
            case .noAudioProduced:
                return "Audio speed conversion produced no audio"
            case .stalledRender:
                return "Audio speed conversion stopped rendering"
            }
        }
    }

    static func compressForUpload(source: URL, rate: Double) throws -> URL {
        guard rate > 1 else { return source }
        guard rate <= 3 else { throw CompressionError.invalidRate(rate) }

        let inputFile = try AVAudioFile(forReading: source)
        guard inputFile.length > 0 else { throw CompressionError.noAudioProduced }

        let engine = AVAudioEngine()
        let player = AVAudioPlayerNode()
        let timePitch = AVAudioUnitTimePitch()
        timePitch.rate = Float(rate)
        engine.attach(player)
        engine.attach(timePitch)
        engine.connect(player, to: timePitch, format: inputFile.processingFormat)
        engine.connect(timePitch, to: engine.mainMixerNode, format: inputFile.processingFormat)

        try engine.enableManualRenderingMode(
            .offline,
            format: inputFile.processingFormat,
            maximumFrameCount: 4_096
        )
        let destination = FileManager.default.temporaryDirectory
            .appendingPathComponent("cloud-dictation-\(UUID().uuidString)")
            .appendingPathExtension("wav")
        var completed = false
        defer {
            if !completed {
                try? FileManager.default.removeItem(at: destination)
            }
        }
        let outputFile = try AVAudioFile(
            forWriting: destination,
            settings: engine.manualRenderingFormat.settings,
            commonFormat: engine.manualRenderingFormat.commonFormat,
            interleaved: engine.manualRenderingFormat.isInterleaved
        )
        let buffer = AVAudioPCMBuffer(
            pcmFormat: engine.manualRenderingFormat,
            frameCapacity: engine.manualRenderingMaximumFrameCount
        )!

        engine.prepare()
        try engine.start()
        let playbackCompletion = CloudflarePlaybackCompletion()
        player.scheduleFile(inputFile, at: nil, completionCallbackType: .dataRendered) { _ in
            playbackCompletion.finish()
        }
        player.play()

        // The offline renderer completes its final block with a little zero
        // padding. Limit the file to the mathematical duration so billing
        // follows the selected speed rather than the renderer's block size.
        let expectedFrameCount = AVAudioFrameCount(
            (Double(inputFile.length) / rate).rounded(.up)
        )
        var framesWritten: AVAudioFrameCount = 0
        var stalledRenders = 0
        while !playbackCompletion.isComplete {
            switch try engine.renderOffline(engine.manualRenderingMaximumFrameCount, to: buffer) {
            case .success:
                stalledRenders = 0
                let remainingFrames = expectedFrameCount - framesWritten
                if buffer.frameLength > 0, remainingFrames > 0 {
                    buffer.frameLength = min(buffer.frameLength, remainingFrames)
                    try outputFile.write(from: buffer)
                    framesWritten += buffer.frameLength
                }
            case .insufficientDataFromInputNode, .cannotDoInCurrentContext:
                stalledRenders += 1
                if stalledRenders > 100 { throw CompressionError.stalledRender }
            case .error:
                throw CompressionError.noAudioProduced
            @unknown default:
                throw CompressionError.noAudioProduced
            }
        }
        engine.stop()

        guard framesWritten == expectedFrameCount else {
            throw CompressionError.noAudioProduced
        }
        completed = true
        return destination
    }
}

/// Transcribes through a Cloudflare Worker backed by Workers AI.
/// Audio never touches a local model: the recorded WAV is uploaded and the
/// worker returns the finished text.
class CloudflareEngine: TranscriptionEngine {
    var engineName: String { "Cloudflare" }

    private var reachable = false
    private var isCancelled = false
    private var uploadTask: Task<String, Error>?
    private var heartbeat: Task<Void, Never>?

    var onProgressUpdate: ((Float) -> Void)?

    var isModelLoaded: Bool { reachable }

    static var client: CloudflareClient {
        CloudflareClient(
            endpoint: AppPreferences.shared.cloudflareEndpoint,
            token: AppPreferences.shared.cloudflareAuthToken
        )
    }

    func initialize() async throws {
        // Cache each model's language list so the picker offers only what the
        // selected model accepts. Nova-3 hard errors on an unsupported code.
        let entries = try await Self.client.catalog()
        let map = Dictionary(uniqueKeysWithValues: entries.map { ($0.key, $0.languages ?? ["*"]) })
        if let encoded = try? JSONEncoder().encode(map),
           let json = String(data: encoded, encoding: .utf8) {
            AppPreferences.shared.cloudflareModelLanguages = json
        }
        reachable = true
    }

    func transcribeAudio(url: URL, settings: Settings) async throws -> String {
        isCancelled = false
        onProgressUpdate?(0.05)

        // The worker reports no intermediate progress, so advance a slow bar
        // toward 0.9 while the round trip is in flight.
        startHeartbeat()
        defer { stopHeartbeat() }

        let rate = AppPreferences.shared.cloudflareCompressionRate
        let uploadURL: URL
        if rate > 1 {
            do {
                uploadURL = try CloudflareAudioCompressor.compressForUpload(source: url, rate: rate)
            } catch {
                // Dictation should still work if AVFoundation cannot render a
                // particular recording. Keep the original for history and send
                // it down the exact upload path used before compression existed.
                print("Cloudflare audio compression failed at \(rate)x: \(error.localizedDescription). Uploading original audio.")
                uploadURL = url
            }
        } else {
            // 1x is deliberately byte-path-identical to the historical upload.
            uploadURL = url
        }
        defer {
            if uploadURL != url {
                try? FileManager.default.removeItem(at: uploadURL)
            }
        }

        let task = Task { () throws -> String in
            try await Self.client.transcribe(fileURL: uploadURL, query: Self.query(settings: settings))
        }

        uploadTask = task
        defer { uploadTask = nil }

        let text = try await task.value
        guard !isCancelled else { throw CancellationError() }

        onProgressUpdate?(0.95)

        var processed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if settings.shouldApplyAsianAutocorrect && !processed.isEmpty {
            processed = AutocorrectWrapper.format(processed)
        }

        onProgressUpdate?(1.0)
        return processed
    }

    func cancelTranscription() {
        isCancelled = true
        uploadTask?.cancel()
        uploadTask = nil
        stopHeartbeat()
    }

    func getSupportedLanguages() -> [String] {
        LanguageUtil.supportedLanguages(
            engine: "cloudflare",
            fluidAudioModelVersion: AppPreferences.shared.fluidAudioModelVersion
        )
    }

    private static func query(settings: Settings) -> [URLQueryItem] {
        let prefs = AppPreferences.shared
        var items = [
            URLQueryItem(name: "model", value: prefs.cloudflareModel),
            URLQueryItem(name: "language", value: settings.selectedLanguage),
        ]

        // Settings > Transcription > Vocabulary, a comma separated term list.
        // Nova-3 boosts the terms directly, Whisper takes them as a decoder
        // glossary, and the cleanup pass uses them as known spellings.
        let vocabulary = settings.initialPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
        if !vocabulary.isEmpty {
            items.append(URLQueryItem(name: "vocabulary", value: vocabulary))
        }

        if prefs.cloudflareCleanupEnabled {
            items.append(URLQueryItem(name: "cleanup", value: "1"))
            items.append(URLQueryItem(name: "cleanup_model", value: prefs.cloudflareCleanupModel))
        }

        return items
    }

    private func startHeartbeat() {
        heartbeat?.cancel()
        heartbeat = Task { [weak self] in
            var progress: Float = 0.05
            while !Task.isCancelled, progress < 0.9 {
                try? await Task.sleep(nanoseconds: 250_000_000)
                progress += 0.03
                let value = min(progress, 0.9)
                await MainActor.run { self?.onProgressUpdate?(value) }
            }
        }
    }

    private func stopHeartbeat() {
        heartbeat?.cancel()
        heartbeat = nil
    }
}

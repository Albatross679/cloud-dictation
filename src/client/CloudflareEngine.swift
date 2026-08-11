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
        var req = try request("models")
        req.timeoutInterval = 15

        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else {
            throw ClientError.badStatus(0, "")
        }
        guard http.statusCode == 200 else {
            throw ClientError.badStatus(http.statusCode, String(data: data, encoding: .utf8) ?? "")
        }
        return try JSONDecoder().decode(ModelsResponse.self, from: data).models.map(\.key)
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
        struct Entry: Decodable { let key: String }
        let models: [Entry]
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
        _ = try await Self.client.models()
        reachable = true
    }

    func transcribeAudio(url: URL, settings: Settings) async throws -> String {
        isCancelled = false
        onProgressUpdate?(0.05)

        // The worker reports no intermediate progress, so advance a slow bar
        // toward 0.9 while the round trip is in flight.
        startHeartbeat()
        defer { stopHeartbeat() }

        let task = Task { () throws -> String in
            try await Self.client.transcribe(fileURL: url, query: Self.query(settings: settings))
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

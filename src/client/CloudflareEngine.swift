import Foundation
import AVFoundation

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

    private var endpoint: String { AppPreferences.shared.cloudflareEndpoint }
    private var token: String { AppPreferences.shared.cloudflareAuthToken }
    private var model: String { AppPreferences.shared.cloudflareModel }
    private var cleanupEnabled: Bool { AppPreferences.shared.cloudflareCleanupEnabled }
    private var cleanupModel: String { AppPreferences.shared.cloudflareCleanupModel }
    private var keyterms: String { AppPreferences.shared.cloudflareKeyterms }

    func initialize() async throws {
        guard let base = URL(string: endpoint), !token.isEmpty else {
            throw TranscriptionError.contextInitializationFailed
        }

        var request = URLRequest(url: base.appendingPathComponent("models"))
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.timeoutInterval = 15

        let (_, response) = try await URLSession.shared.data(for: request)
        guard let http = response as? HTTPURLResponse, http.statusCode == 200 else {
            throw TranscriptionError.contextInitializationFailed
        }

        reachable = true
    }

    func transcribeAudio(url: URL, settings: Settings) async throws -> String {
        guard let requestURL = buildURL(settings: settings) else {
            throw TranscriptionError.contextInitializationFailed
        }

        isCancelled = false
        onProgressUpdate?(0.05)

        var request = URLRequest(url: requestURL)
        request.httpMethod = "POST"
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
        request.timeoutInterval = 300

        // The worker reports no intermediate progress, so advance a slow bar
        // toward 0.9 while the round trip is in flight.
        startHeartbeat()
        defer { stopHeartbeat() }

        let task = Task { () throws -> String in
            let (data, response) = try await URLSession.shared.upload(for: request, fromFile: url)

            guard let http = response as? HTTPURLResponse else {
                throw TranscriptionError.processingFailed
            }
            guard http.statusCode == 200 else {
                let body = String(data: data, encoding: .utf8) ?? ""
                throw CloudflareEngineError.server(status: http.statusCode, body: body)
            }

            let decoded = try JSONDecoder().decode(TranscriptionResponse.self, from: data)
            return decoded.text
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

    private func buildURL(settings: Settings) -> URL? {
        guard let base = URL(string: endpoint) else { return nil }
        var components = URLComponents(
            url: base.appendingPathComponent("transcribe"),
            resolvingAgainstBaseURL: false
        )

        var items = [
            URLQueryItem(name: "model", value: model),
            URLQueryItem(name: "language", value: settings.selectedLanguage),
        ]
        if cleanupEnabled {
            items.append(URLQueryItem(name: "cleanup", value: "1"))
            items.append(URLQueryItem(name: "cleanup_model", value: cleanupModel))
            let instruction = settings.initialPrompt.trimmingCharacters(in: .whitespacesAndNewlines)
            if !instruction.isEmpty {
                items.append(URLQueryItem(name: "instruction", value: instruction))
            }
        }
        let terms = keyterms.trimmingCharacters(in: .whitespacesAndNewlines)
        if !terms.isEmpty {
            items.append(URLQueryItem(name: "keyterms", value: terms))
        }

        components?.queryItems = items
        return components?.url
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

enum CloudflareEngineError: LocalizedError {
    case server(status: Int, body: String)

    var errorDescription: String? {
        switch self {
        case let .server(status, body):
            return "Cloudflare worker returned \(status): \(body)"
        }
    }
}

private struct TranscriptionResponse: Decodable {
    let text: String
}

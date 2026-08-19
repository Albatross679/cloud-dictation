import Foundation
import AVFoundation

/// Talks to the cloud-dictation worker. Shared by the transcription engine and
/// the Test Connection button so both agree on what reachable means.
struct CloudflareClient {
    enum ConnectionMode: String {
        case worker
        case directAPI
    }

    let endpoint: String
    let token: String
    let accountID: String
    let mode: ConnectionMode

    enum ClientError: LocalizedError {
        case notConfigured
        case badStatus(Int, String)
        case directUsageUnavailable
        case languageMismatch(requested: String, detected: String)

        var errorDescription: String? {
            switch self {
            case .notConfigured:
                return "Set the credentials for the selected Cloudflare connection mode first."
            case let .badStatus(code, body):
                let detail = CloudflareClient.apiErrorMessage(from: body)
                if code == 401 { return "Cloudflare rejected the API token (401)\(detail.isEmpty ? "" : ": \(detail)")" }
                return "Cloudflare returned \(code)\(detail.isEmpty ? "" : ": \(detail)")"
            case .directUsageUnavailable:
                return "Usage is available in the Cloudflare dashboard when using Direct API."
            case let .languageMismatch(requested, detected):
                return "Discarded: came back as \(detected) script but the language is set to \(requested). Try a model that can be pinned to a language."
            }
        }
    }

    private func request(_ path: String) throws -> URLRequest {
        let trimmed = endpoint.trimmingCharacters(in: .whitespacesAndNewlines)
        guard mode == .worker, !trimmed.isEmpty, !token.isEmpty, let base = URL(string: trimmed) else {
            throw ClientError.notConfigured
        }
        var request = URLRequest(url: base.appendingPathComponent(path))
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    private func directRequest(model: String, payload: [String: Any]) throws -> URLRequest {
        let id = accountID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard mode == .directAPI, !id.isEmpty, !token.isEmpty,
              let url = URL(string: "https://api.cloudflare.com/client/v4/accounts/\(id)/ai/run/\(model)")
        else { throw ClientError.notConfigured }

        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.timeoutInterval = 300
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: payload)
        return request
    }

    private func accountsRequest() throws -> URLRequest {
        guard mode == .directAPI, !token.isEmpty,
              let url = URL(string: "https://api.cloudflare.com/client/v4/accounts")
        else { throw ClientError.notConfigured }
        var request = URLRequest(url: url)
        request.timeoutInterval = 15
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        return request
    }

    private static func apiErrorMessage(from body: String) -> String {
        guard let data = body.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return body.trimmingCharacters(in: .whitespacesAndNewlines) }
        let errors = (object["errors"] as? [[String: Any]] ?? [])
            .compactMap { $0["message"] as? String }
        if !errors.isEmpty { return errors.joined(separator: "; ") }
        if let error = object["error"] as? String { return error }
        return body.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    private func directRun(model: String, payload: [String: Any]) async throws -> [String: Any] {
        let (data, response) = try await URLSession.shared.data(for: directRequest(model: model, payload: payload))
        guard let http = response as? HTTPURLResponse else { throw ClientError.badStatus(0, "") }
        let body = String(data: data, encoding: .utf8) ?? ""
        guard (200...299).contains(http.statusCode) else { throw ClientError.badStatus(http.statusCode, body) }
        guard let envelope = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              envelope["success"] as? Bool == true,
              let result = envelope["result"] as? [String: Any]
        else { throw ClientError.badStatus(http.statusCode, body) }
        return result
    }

    struct Account: Decodable, Identifiable {
        let id: String
        let name: String
    }

    /// Accounts visible to this token. Workers AI's REST API token template
    /// grants this lookup, letting the app avoid asking a new user for an ID.
    func accounts() async throws -> [Account] {
        let (data, response) = try await URLSession.shared.data(for: accountsRequest())
        guard let http = response as? HTTPURLResponse else { throw ClientError.badStatus(0, "") }
        let body = String(data: data, encoding: .utf8) ?? ""
        guard (200...299).contains(http.statusCode),
              let envelope = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              envelope["success"] as? Bool == true,
              let result = envelope["result"] as? [[String: Any]]
        else { throw ClientError.badStatus(http.statusCode, body) }
        let accounts = result.compactMap { item -> Account? in
            guard let id = item["id"] as? String, let name = item["name"] as? String else { return nil }
            return Account(id: id, name: name)
        }
        guard !accounts.isEmpty else { throw ClientError.badStatus(http.statusCode, "This API token cannot access any Cloudflare accounts.") }
        return accounts
    }

    /// Model keys the worker offers, e.g. ["nova-3", "whisper-turbo"].
    func models() async throws -> [String] {
        try await catalog().map(\.key)
    }

    func catalog() async throws -> [ModelEntry] {
        if mode == .directAPI { return Self.directCatalog }
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

    // The direct API has no equivalent to this app's Worker-owned /models
    // catalog, so retain the same tested registry locally for its picker.
    private static let directCatalog = [
        ModelEntry(key: "nova-3", languages: ["en", "es", "fr", "de", "it", "pt", "nl", "hi", "ru", "ja"]),
        ModelEntry(key: "whisper-turbo", languages: nil),
        ModelEntry(key: "whisper", languages: []),
        ModelEntry(key: "whisper-tiny-en", languages: ["en"]),
    ]

    func validateConnection() async throws -> [String] {
        if mode == .worker { return try await models() }
        // This small Workers AI inference validates both Account ID and token.
        // There is no credential-only Workers AI REST endpoint, and account
        // metadata endpoints would require permissions beyond the documented
        // Workers AI API Token template.
        _ = try await directRun(model: "@cf/meta/llama-3.2-3b-instruct", payload: [
            "messages": [["role": "user", "content": "Reply with OK."]],
            "max_tokens": 2,
            "temperature": 0,
        ])
        return Self.directCatalog.map(\.key)
    }

    func usage() async throws -> CloudflareUsage {
        if mode == .directAPI { throw ClientError.directUsageUnavailable }
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
        if mode == .directAPI { return try await directTranscribe(fileURL: fileURL, query: query) }
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

    private func directTranscribe(fileURL: URL, query: [URLQueryItem]) async throws -> String {
        let values = Dictionary(uniqueKeysWithValues: query.compactMap { item in
            item.value.map { (item.name, $0) }
        })
        let model = values["model"] ?? "nova-3"
        let language = values["language"] ?? "auto"
        let terms = Self.parseTerms(values["vocabulary"] ?? "")
        let audio = try Data(contentsOf: fileURL)
        let byteArray = audio.map(Int.init)

        var payload: [String: Any] = ["audio": byteArray]
        switch model {
        case "nova-3":
            payload["punctuate"] = true
            payload["smart_format"] = true
            payload["numerals"] = true
            payload["detect_language"] = language == "auto"
            if language != "auto" { payload["language"] = language }
            if language != "auto", !terms.isEmpty { payload["keyterm"] = terms }
        case "whisper-turbo":
            // The Worker sends this model base64; preserve that input form.
            payload["audio"] = audio.base64EncodedString()
            payload["task"] = "transcribe"
            payload["vad_filter"] = true
            if language != "auto" { payload["language"] = language }
            if !terms.isEmpty { payload["initial_prompt"] = "Glossary: \(terms.joined(separator: ", "))." }
        case "whisper", "whisper-tiny-en":
            break
        default:
            throw ClientError.badStatus(400, "Unknown transcription model: \(model)")
        }

        let ids = [
            "nova-3": "@cf/deepgram/nova-3",
            "whisper-turbo": "@cf/openai/whisper-large-v3-turbo",
            "whisper": "@cf/openai/whisper",
            "whisper-tiny-en": "@cf/openai/whisper-tiny-en",
        ]
        guard let modelID = ids[model] else { throw ClientError.badStatus(400, "Unknown transcription model: \(model)") }
        let result = try await directRun(model: modelID, payload: payload)
        let transcript: String
        if model == "nova-3" {
            transcript = (((result["results"] as? [String: Any])?["channels"] as? [[String: Any]])?.first?["alternatives"] as? [[String: Any]])?.first?["transcript"] as? String ?? ""
        } else {
            transcript = (result["text"] as? String) ?? ((result["transcription_info"] as? [String: Any])?["text"] as? String) ?? ""
        }

        var text = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        if values["cleanup"] == "1", !text.isEmpty {
            text = try await directCleanup(text: text, model: values["cleanup_model"] ?? "llama-8b", terms: terms)
        }
        if let mismatch = Self.languageMismatch(text, requested: language) {
            throw ClientError.languageMismatch(requested: language, detected: mismatch)
        }
        return text
    }

    private func directCleanup(text: String, model: String, terms: [String]) async throws -> String {
        let modelIDs = [
            "llama-8b": "@cf/meta/llama-3.1-8b-instruct-fp8",
            "llama-3b": "@cf/meta/llama-3.2-3b-instruct",
            "granite-micro": "@cf/ibm-granite/granite-4.0-h-micro",
            "mistral-24b": "@cf/mistralai/mistral-small-3.1-24b-instruct",
        ]
        let system = Self.cleanupSystem + (terms.isEmpty ? "" : "\n\nThese terms are spelled correctly. Only correct a word to one of them when it is clearly the same word misheard: \(terms.joined(separator: ", ")).")
        let result = try await directRun(model: modelIDs[model] ?? modelIDs["llama-8b"]!, payload: [
            "messages": [
                ["role": "system", "content": system],
                ["role": "user", "content": text],
            ],
            "temperature": 0.1,
            "max_tokens": 2048,
        ])
        let cleaned = (((result["choices"] as? [[String: Any]])?.first?["message"] as? [String: Any])?["content"] as? String ?? result["response"] as? String ?? "").trimmingCharacters(in: .whitespacesAndNewlines)
        return cleaned.isEmpty ? text : cleaned
    }

    private static let cleanupSystem = """
    You clean up dictated speech into written text.

    Rules:
    - Remove filler words: um, uh, like, you know, I mean, sort of.
    - Remove false starts and self-corrections. Keep only what the speaker settled on.
    - Fix grammar and add punctuation, paragraph breaks, and capitalization.
    - Keep the speaker's own words, tone, and meaning. Do not summarize, expand, or add ideas.
    - Never substitute a word you did not hear. If a term looks like a garbled proper noun, product name, or identifier, leave it exactly as transcribed. Never guess what it \"should\" have been.
    - Keep technical terms, product names, and code identifiers exactly as transcribed, including spacing oddities.
    - Never use em dashes. Use commas, parentheses, colons, or separate sentences.
    - Output only the cleaned text. No preamble, no quotes, no commentary.
    """

    private static func parseTerms(_ vocabulary: String) -> [String] {
        var seen = Set<String>()
        return vocabulary.components(separatedBy: CharacterSet(charactersIn: ",;\n\r")).compactMap { entry in
            let term = entry.trimmingCharacters(in: .whitespacesAndNewlines)
                .replacingOccurrences(of: "^[^\\p{L}\\p{N}]+|[.!?]+$", with: "", options: .regularExpression)
                .trimmingCharacters(in: .whitespacesAndNewlines)
            guard !term.isEmpty, term.count <= 60,
                  term.rangeOfCharacter(from: .alphanumerics) != nil,
                  seen.insert(term.lowercased()).inserted
            else { return nil }
            return term
        }.prefix(100).map { $0 }
    }

    // Port of src/core/language.js: reject a confidently wrong script before
    // it can be pasted into the focused app (notably Whisper on short audio).
    private static func languageMismatch(_ text: String, requested: String) -> String? {
        guard requested != "auto" else { return nil }
        let expected = [
            "zh": "han", "ja": "kana", "ko": "hangul", "ru": "cyrillic", "uk": "cyrillic",
            "ar": "arabic", "he": "hebrew", "hi": "devanagari", "ml": "malayalam",
        ][requested] ?? "latin"
        var counts: [String: Int] = [:]
        for scalar in text.unicodeScalars {
            guard let script = script(of: scalar.value) else { continue }
            counts[script, default: 0] += 1
        }
        let total = counts.values.reduce(0, +)
        guard total >= 8, let dominant = counts.max(by: { $0.value < $1.value }) else { return nil }
        guard dominant.key != expected, Double(dominant.value) / Double(total) >= 0.5 else { return nil }
        return dominant.key
    }

    private static func script(of value: UInt32) -> String? {
        switch value {
        case 0x3400...0x4DBF, 0x4E00...0x9FFF, 0xF900...0xFAFF: return "han"
        case 0x3040...0x30FF: return "kana"
        case 0xAC00...0xD7AF, 0x1100...0x11FF: return "hangul"
        case 0x0400...0x052F: return "cyrillic"
        case 0x0600...0x06FF: return "arabic"
        case 0x0590...0x05FF: return "hebrew"
        case 0x0900...0x097F: return "devanagari"
        case 0x0D00...0x0D7F: return "malayalam"
        case 0x0041...0x024F: return "latin"
        default: return nil
        }
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
        let prefs = AppPreferences.shared
        return CloudflareClient(
            endpoint: prefs.cloudflareEndpoint,
            token: prefs.cloudflareConnectionMode == "direct" ? prefs.cloudflareDirectAPIToken : prefs.cloudflareAuthToken,
            accountID: prefs.cloudflareAccountID,
            mode: prefs.cloudflareConnectionMode == "direct" ? .directAPI : .worker
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

import Foundation

/// OpenRouter's dedicated speech-to-text endpoint.
///
/// OpenRouter has had `POST /api/v1/audio/transcriptions` since 2026-05-01, so
/// audio does not go through chat completions as an `input_audio` content part.
/// The JSON body is `{"model", "input_audio": {"data": "<base64>", "format"},
/// "language"?, "temperature"?}`; a `multipart/form-data` form with `file` and
/// `model` is accepted too for OpenAI SDK compatibility, capped at 25 MB. The
/// response is `{"text", "usage": {"seconds", "total_tokens", "cost"}}`, and
/// upstream providers time out after roughly 60 seconds per request.
///
/// JSON is chosen over multipart because the body is then one pure, comparable
/// value rather than a boundary-delimited stream, which keeps the encoder
/// testable. Accepted formats are wav, mp3, flac, m4a, ogg, webm, and aac; this
/// app always uploads the recorder's WAV.
///
/// Verified live: a syntactically valid request with a wrong key returns HTTP
/// 401 `{"error":{"message":"User not found.","code":401}}`, distinct from the
/// transport failure that means the host was unreachable. The model catalogue
/// below was read from
/// GET /api/v1/models?output_modalities=transcription rather than from memory.
enum OpenRouterRequest {
    static let host = "https://openrouter.ai/api/v1"

    /// OpenRouter's own 25 MB multipart cap. Applied to the raw audio before
    /// base64 expansion so the message names a size the user recognises.
    static let maxAudioBytes = 25 * 1_048_576

    /// A deliberately short list drawn from the 19 models the live catalogue
    /// reports, chosen to cover the three reasons someone switches: matching
    /// the Cloudflare default, lowest cost, and highest accuracy.
    static let catalog: [CloudModel] = [
        CloudModel(
            key: "whisper-large-v3-turbo",
            id: "openai/whisper-large-v3-turbo",
            label: "Whisper large-v3-turbo",
            languages: nil,
            notes: "Cheapest of the Whisper weights here, and accepts a pinned language."
        ),
        CloudModel(
            key: "whisper-large-v3",
            id: "openai/whisper-large-v3",
            label: "Whisper large-v3",
            languages: nil,
            notes: "More accurate Whisper weights at roughly twice the turbo rate."
        ),
        CloudModel(
            key: "nova-3",
            id: "deepgram/nova-3",
            label: "Deepgram Nova-3",
            // The same ten codes Cloudflare's Nova-3 build accepts.
            languages: ["en", "es", "fr", "de", "it", "pt", "nl", "hi", "ru", "ja"],
            notes: "The same model the Cloudflare default uses, billed per audio minute."
        ),
        CloudModel(
            key: "gpt-4o-mini-transcribe",
            id: "openai/gpt-4o-mini-transcribe",
            label: "GPT-4o mini transcribe",
            languages: nil,
            notes: "OpenAI's small transcription model, strong on proper nouns."
        ),
        CloudModel(
            key: "gpt-4o-transcribe",
            id: "openai/gpt-4o-transcribe",
            label: "GPT-4o transcribe",
            languages: nil,
            notes: "OpenAI's full transcription model. The most accurate option here."
        ),
    ]

    static let defaultModelKey = "whisper-large-v3-turbo"

    /// Text models for the cleanup pass over the standard chat route.
    static let cleanupModels: [(key: String, id: String, label: String)] = [
        (key: "gemini-flash", id: "google/gemini-2.5-flash", label: "Gemini 2.5 Flash (fastest)"),
        (key: "gpt-4o-mini", id: "openai/gpt-4o-mini", label: "GPT-4o mini"),
        (key: "llama-8b", id: "meta-llama/llama-3.1-8b-instruct", label: "Llama 3.1 8B"),
    ]
    static let defaultCleanupModelKey = "gemini-flash"

    static func model(_ key: String) throws -> CloudModel {
        guard let model = catalog.first(where: { $0.key == key }) else {
            throw CloudProviderError.unknownModel(key)
        }
        return model
    }

    /// Encodes one transcription call. `language` is omitted under auto-detect
    /// rather than sent as the string "auto", which is not an ISO-639-1 code.
    static func transcription(
        model key: String,
        audio: Data,
        format: String = "wav",
        language: String
    ) throws -> CloudRequestPlan {
        let model = try model(key)
        guard audio.count <= maxAudioBytes else {
            throw CloudProviderError.audioTooLarge(.openrouter, audio.count, maxAudioBytes)
        }
        guard let url = URL(string: "\(host)/audio/transcriptions") else {
            throw CloudProviderError.unknownModel(key)
        }

        var payload: [String: Any] = [
            "model": model.id,
            "input_audio": ["data": audio.base64EncodedString(), "format": format],
        ]
        if language != "auto" { payload["language"] = language }

        return CloudRequestPlan(
            url: url,
            method: "POST",
            headers: [("Content-Type", "application/json")],
            // Sorted keys keep an encoded body reproducible across runs.
            body: try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        )
    }

    static func readText(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return object["text"] as? String
    }

    static func cleanup(model id: String, system: String, text: String) throws -> CloudRequestPlan {
        guard let url = URL(string: "\(host)/chat/completions") else {
            throw CloudProviderError.unknownModel(id)
        }
        return CloudRequestPlan(
            url: url,
            method: "POST",
            headers: [("Content-Type", "application/json")],
            body: try CloudHTTP.chatCleanupBody(model: id, system: system, text: text)
        )
    }

    /// Credential-only probe. Unlike the other two vendors OpenRouter has one,
    /// so Test Connection can separate a bad key from a model that is refusing
    /// the request without spending anything on inference.
    static func keyProbe() throws -> CloudRequestPlan {
        guard let url = URL(string: "\(host)/key") else {
            throw CloudProviderError.notConfigured(.openrouter)
        }
        return CloudRequestPlan(url: url, method: "GET", headers: [], body: Data())
    }
}

/// Talks to OpenRouter. Shared by the engine and Test Connection so both agree
/// on what a working key means.
struct OpenRouterClient: CloudTranscriber {
    static var catalog: [CloudModel] { OpenRouterRequest.catalog }

    let token: String

    private var key: String {
        get throws {
            let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { throw CloudProviderError.notConfigured(.openrouter) }
            return trimmed
        }
    }

    func transcribe(fileURL: URL, query: [URLQueryItem]) async throws -> String {
        let key = try key
        let values = CloudHTTP.values(query)
        let modelKey = values["model"] ?? OpenRouterRequest.defaultModelKey
        let language = values["language"] ?? "auto"
        let audio = try Data(contentsOf: fileURL)

        let plan = try OpenRouterRequest.transcription(model: modelKey, audio: audio, language: language)
        // Upstream providers cut off around 60 seconds, so waiting the
        // Cloudflare path's five minutes would only delay the error.
        let data = try await CloudHTTP.send(plan, provider: .openrouter, bearer: key, timeout: 120)
        var text = (OpenRouterRequest.readText(data) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        if values["cleanup"] == "1", !text.isEmpty {
            let terms = CloudHTTP.parseTerms(values["vocabulary"] ?? "")
            text = try await cleanup(text: text, modelKey: values["cleanup_model"] ?? OpenRouterRequest.defaultCleanupModelKey, terms: terms, key: key)
        }
        return text
    }

    private func cleanup(text: String, modelKey: String, terms: [String], key: String) async throws -> String {
        let id = OpenRouterRequest.cleanupModels.first(where: { $0.key == modelKey })?.id
            ?? OpenRouterRequest.cleanupModels.first(where: { $0.key == OpenRouterRequest.defaultCleanupModelKey })!.id
        let plan = try OpenRouterRequest.cleanup(
            model: id,
            system: CloudHTTP.cleanupSystem(terms: terms),
            text: text
        )
        guard let data = try? await CloudHTTP.send(plan, provider: .openrouter, bearer: key, timeout: 120),
              let cleaned = CloudHTTP.readChatText(data)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !cleaned.isEmpty
        else { return text }
        return cleaned
    }

    /// Checks the key against /key, which costs nothing and answers 401 for a
    /// key OpenRouter does not know, then confirms the account can actually
    /// list transcription models.
    func validateConnection() async throws -> [String] {
        let key = try key
        _ = try await CloudHTTP.send(OpenRouterRequest.keyProbe(), provider: .openrouter, bearer: key, timeout: 15)
        return OpenRouterRequest.catalog.map(\.key)
    }
}

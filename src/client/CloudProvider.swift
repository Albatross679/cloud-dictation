import Foundation

/// Which cloud transcribes the audio.
///
/// Cloudflare came first and stays the default: its request encoders, usage
/// readout, and Worker mode are untouched by this enum existing. The other two
/// cases route the same recorded WAV to a different vendor with its own key.
enum CloudProvider: String, CaseIterable, Equatable {
    case cloudflare
    case huggingface
    case openrouter

    /// Preferences store the raw string, so an unknown value must not crash a
    /// launch. Anything unrecognised falls back to the historical provider.
    static func named(_ raw: String) -> CloudProvider {
        CloudProvider(rawValue: raw) ?? .cloudflare
    }

    var label: String {
        switch self {
        case .cloudflare: return "Cloudflare"
        case .huggingface: return "Hugging Face"
        case .openrouter: return "OpenRouter"
        }
    }

    /// Separate Keychain accounts. A user switching providers must never have
    /// one vendor's key overwritten by another's, and no key is ever shared.
    var keychainAccount: String {
        switch self {
        case .cloudflare: return "cloudflareDirectAPIToken"
        case .huggingface: return "huggingFaceAPIToken"
        case .openrouter: return "openRouterAPIToken"
        }
    }

    /// Where a user creates the key, opened by the Settings button.
    var tokenPageURL: String {
        switch self {
        case .cloudflare: return "https://dash.cloudflare.com/profile/api-tokens"
        case .huggingface: return "https://huggingface.co/settings/tokens/new?ownUserPermissions=inference.serverless.write&tokenType=fineGrained"
        case .openrouter: return "https://openrouter.ai/settings/keys"
        }
    }

    var keyFieldPrompt: String {
        switch self {
        case .cloudflare: return "Workers AI API token"
        case .huggingface: return "Hugging Face access token (hf_...)"
        case .openrouter: return "OpenRouter API key (sk-or-...)"
        }
    }
}

/// Whether one feature reaches the model on a given provider.
///
/// A feature that cannot work must say so in the UI rather than being accepted
/// and dropped. `unsupported` carries the one-line reason the settings pane
/// shows beside the disabled control.
enum CloudFeatureSupport: Equatable {
    case supported
    case unsupported(String)

    var isSupported: Bool {
        if case .supported = self { return true }
        return false
    }

    var reason: String? {
        if case let .unsupported(text) = self { return text }
        return nil
    }
}

/// The honest feature matrix, one row per provider.
///
/// Every `unsupported` reason below was produced by a live request against the
/// vendor rather than read off a docs page; see the PR that added this file.
struct CloudProviderFeatures: Equatable {
    let language: CloudFeatureSupport
    let vocabulary: CloudFeatureSupport
    let audioSpeed: CloudFeatureSupport
    let cleanup: CloudFeatureSupport
    let usage: CloudFeatureSupport

    static func of(_ provider: CloudProvider) -> CloudProviderFeatures {
        switch provider {
        case .cloudflare:
            // Unchanged from before this enum existed. Per-model narrowing is
            // still done by CloudflareDirectRequest, which stays authoritative.
            return CloudProviderFeatures(
                language: .supported,
                vocabulary: .supported,
                audioSpeed: .supported,
                cleanup: .supported,
                usage: .supported
            )
        case .huggingface:
            return CloudProviderFeatures(
                // Live: {"error":"AutomaticSpeechRecognitionPipeline._sanitize_parameters()
                // got an unexpected keyword argument 'language'"} with HTTP 400.
                language: .unsupported("Hugging Face's speech pipeline rejects a language parameter, so Whisper always auto-detects."),
                // Live: {"error":"The following `model_kwargs` are not used by the
                // model: ['prompt']"} with HTTP 400.
                vocabulary: .unsupported("Hugging Face's speech pipeline takes no decoder prompt, so the term list cannot reach the model."),
                // Compression happens on this Mac before the upload, so it is
                // provider-independent.
                audioSpeed: .supported,
                cleanup: .supported,
                usage: .unsupported("Neurons are a Cloudflare billing unit. Hugging Face spend is on your account's billing page.")
            )
        case .openrouter:
            return CloudProviderFeatures(
                language: .supported,
                vocabulary: .unsupported("OpenRouter's transcription endpoint has no prompt or keyterm field, so terms cannot be boosted."),
                audioSpeed: .supported,
                cleanup: .supported,
                usage: .unsupported("Neurons are a Cloudflare billing unit. OpenRouter reports per-request cost on its activity page.")
            )
        }
    }
}

/// One transcription model as a provider's API sees it.
struct CloudModel: Equatable {
    let key: String
    let id: String
    let label: String
    /// nil when the model accepts any language the client can display, []
    /// when it cannot be pinned at all.
    let languages: [String]?
    let notes: String
}

/// Everything `URLSession` needs for one provider call, already encoded.
struct CloudRequestPlan: Equatable {
    let url: URL
    let method: String
    /// Ordered so an encoded request is byte-stable and testable. Never
    /// contains a credential: the caller adds Authorization separately.
    let headers: [(String, String)]
    let body: Data

    static func == (lhs: CloudRequestPlan, rhs: CloudRequestPlan) -> Bool {
        lhs.url == rhs.url && lhs.method == rhs.method && lhs.body == rhs.body
            && lhs.headers.map { [$0.0, $0.1] } == rhs.headers.map { [$0.0, $0.1] }
    }
}

/// Failures a non-Cloudflare provider can produce.
///
/// Test Connection has to tell three outcomes apart, so they are distinct cases
/// rather than one string: a key the vendor refused, a vendor this Mac could
/// not reach, and a request the vendor understood but rejected.
enum CloudProviderError: LocalizedError, Equatable {
    case notConfigured(CloudProvider)
    case invalidKey(CloudProvider, String)
    case unreachable(CloudProvider, String)
    case badStatus(CloudProvider, Int, String)
    case emptyTranscript(CloudProvider)
    case unknownModel(String)
    case audioTooLarge(CloudProvider, Int, Int)

    var errorDescription: String? {
        switch self {
        case let .notConfigured(provider):
            return "Paste your \(provider.label) API key in Settings > Models > Engine first."
        case let .invalidKey(provider, detail):
            return "\(provider.label) rejected this API key\(Self.suffix(detail))"
        case let .unreachable(provider, detail):
            return "Could not reach \(provider.label)\(Self.suffix(detail))"
        case let .badStatus(provider, code, detail):
            return "\(provider.label) returned \(code)\(Self.suffix(detail))"
        case let .emptyTranscript(provider):
            return "\(provider.label) returned no words. Speak during the recording and try again."
        case let .unknownModel(key):
            return "Unknown transcription model: \(key)"
        case let .audioTooLarge(provider, bytes, limit):
            return "This recording is \(bytes / 1_048_576) MB and \(provider.label) accepts at most \(limit / 1_048_576) MB. Record a shorter clip or raise the audio speed."
        }
    }

    private static func suffix(_ detail: String) -> String {
        let trimmed = detail.trimmingCharacters(in: .whitespacesAndNewlines)
        return trimmed.isEmpty ? "." : ": \(trimmed)"
    }
}

/// What the engine needs from any cloud, so `CloudflareEngine` can hold one of
/// three implementations without knowing which.
protocol CloudTranscriber {
    /// The models this provider offers, in picker order.
    static var catalog: [CloudModel] { get }
    /// Transcribes one recording. `query` carries the same names the Worker
    /// path uses: model, language, vocabulary, cleanup, cleanup_model.
    func transcribe(fileURL: URL, query: [URLQueryItem]) async throws -> String
    /// Proves the key works, returning the model keys now available.
    func validateConnection() async throws -> [String]
}

/// Shared HTTP plumbing, so both new providers classify a failure the same way.
enum CloudHTTP {
    /// Sends a plan and returns the raw response body, mapping transport and
    /// status failures onto the three outcomes Test Connection distinguishes.
    static func send(
        _ plan: CloudRequestPlan,
        provider: CloudProvider,
        bearer: String,
        timeout: TimeInterval
    ) async throws -> Data {
        var request = URLRequest(url: plan.url)
        request.httpMethod = plan.method
        request.timeoutInterval = timeout
        for (name, value) in plan.headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        // Added last and never part of the plan, so a logged or compared plan
        // cannot carry the key.
        request.setValue("Bearer \(bearer)", forHTTPHeaderField: "Authorization")
        if plan.method != "GET" { request.httpBody = plan.body }

        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await URLSession.shared.data(for: request)
        } catch let error as URLError {
            throw CloudProviderError.unreachable(provider, error.localizedDescription)
        }

        guard let http = response as? HTTPURLResponse else {
            throw CloudProviderError.unreachable(provider, "No HTTP response.")
        }
        let body = String(data: data, encoding: .utf8) ?? ""
        if (200...299).contains(http.statusCode) { return data }
        let detail = errorMessage(from: body)
        if http.statusCode == 401 || http.statusCode == 403 {
            throw CloudProviderError.invalidKey(provider, detail)
        }
        // 5xx and 502-from-upstream mean the vendor is up but the model path is
        // not, which is a different fix than a bad key.
        throw CloudProviderError.badStatus(provider, http.statusCode, detail)
    }

    /// Both vendors nest their message differently: Hugging Face returns
    /// `{"error": "..."}` and OpenRouter `{"error": {"message": "..."}}`.
    static func errorMessage(from body: String) -> String {
        guard let data = body.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else { return String(body.trimmingCharacters(in: .whitespacesAndNewlines).prefix(300)) }

        if let text = object["error"] as? String { return text }
        if let nested = object["error"] as? [String: Any],
           let text = nested["message"] as? String { return text }
        if let text = object["message"] as? String { return text }
        return String(body.trimmingCharacters(in: .whitespacesAndNewlines).prefix(300))
    }

    /// The query names the Worker path already uses, so every provider reads
    /// one settings vocabulary rather than inventing its own.
    static func values(_ query: [URLQueryItem]) -> [String: String] {
        Dictionary(
            query.compactMap { item in item.value.map { (item.name, $0) } },
            uniquingKeysWith: { _, last in last }
        )
    }

    /// Settings > Transcription > Vocabulary, shared with the Cloudflare path.
    /// Kept here so a provider that can use the terms parses them identically.
    static func parseTerms(_ vocabulary: String) -> [String] {
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

    /// The cleanup prompt, identical across providers so switching vendor does
    /// not silently change how dictation reads.
    static let cleanupSystem = """
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

    /// Both new providers expose an OpenAI-shaped chat completions route, so
    /// the cleanup pass is one encoder rather than two.
    static func chatCleanupBody(model: String, system: String, text: String) throws -> Data {
        try JSONSerialization.data(
            withJSONObject: [
                "model": model,
                "messages": [
                    ["role": "system", "content": system],
                    ["role": "user", "content": text],
                ],
                "temperature": 0.1,
                "max_tokens": 2048,
            ],
            options: [.sortedKeys]
        )
    }

    /// Reads the assistant message out of an OpenAI-shaped completion.
    static func readChatText(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let choices = object["choices"] as? [[String: Any]],
              let message = choices.first?["message"] as? [String: Any],
              let content = message["content"] as? String
        else { return nil }
        return content
    }

    /// Appends the vocabulary as known spellings. A provider whose recognizer
    /// ignores terms can still keep them from being rewritten here, which is
    /// the same fallback the Cloudflare Whisper models already use.
    static func cleanupSystem(terms: [String]) -> String {
        guard !terms.isEmpty else { return cleanupSystem }
        return cleanupSystem
            + "\n\nThese terms are spelled correctly. Only correct a word to one of them when it is clearly the same word misheard: "
            + terms.joined(separator: ", ") + "."
    }
}

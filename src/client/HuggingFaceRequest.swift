import Foundation

/// Hugging Face Inference Providers, routed to the `hf-inference` provider.
///
/// Verified live against a real token rather than read off the docs page:
///
/// - `POST https://router.huggingface.co/hf-inference/models/<model>` with the
///   WAV as the raw request body and `Content-Type: audio/wav` returns
///   `{"text": "..."}` with HTTP 200.
/// - The same audio as `{"inputs": "<base64>"}` also works, so raw bytes are
///   chosen only because they avoid base64's 33% upload overhead.
/// - `parameters.language` is refused: HTTP 400,
///   "AutomaticSpeechRecognitionPipeline._sanitize_parameters() got an
///   unexpected keyword argument 'language'". Whisper always auto-detects here.
/// - `parameters.generation_parameters.prompt` is refused: HTTP 400,
///   "The following `model_kwargs` are not used by the model: ['prompt']".
/// - The legacy host `api-inference.huggingface.co` no longer resolves.
///
/// Because neither a language nor a decoder prompt can reach the model, the
/// settings pane disables both controls with the reason rather than encoding a
/// parameter the API throws away. See `CloudProviderFeatures.of(.huggingface)`.
enum HuggingFaceRequest {
    static let host = "https://router.huggingface.co"
    /// The one HF provider whose ASR route this client speaks. Others in the
    /// network (fal-ai, replicate, together, deepinfra) also serve Whisper but
    /// each with its own request schema, so none is offered here.
    static let inferenceProvider = "hf-inference"

    /// The Hub reports exactly these two warm ASR models for `hf-inference`:
    /// GET /api/models?pipeline_tag=automatic-speech-recognition
    /// &inference_provider=hf-inference returns a list of length two. Every
    /// other Whisper size tried returns HTTP 400
    /// "Model not supported by provider hf-inference", so offering one would be
    /// a guaranteed failure rather than a slower transcription.
    static let catalog: [CloudModel] = [
        CloudModel(
            key: "whisper-large-v3-turbo",
            id: "openai/whisper-large-v3-turbo",
            label: "Whisper large-v3-turbo",
            // Whisper's full multilingual range, but it is always auto-detected
            // because the pipeline refuses a language parameter.
            languages: [],
            notes: "Faster and cheaper than large-v3, with slightly lower accuracy."
        ),
        CloudModel(
            key: "whisper-large-v3",
            id: "openai/whisper-large-v3",
            label: "Whisper large-v3",
            languages: [],
            notes: "The most accurate Whisper weights Hugging Face serves warm."
        ),
    ]

    static let defaultModelKey = "whisper-large-v3-turbo"

    /// Text models for the cleanup pass, reached through the router's
    /// OpenAI-compatible /v1/chat/completions route.
    ///
    /// Each was sent a filler-laden sentence and checked for a non-empty
    /// `choices[0].message.content`. The gpt-oss family answers 200 with empty
    /// content because it puts its answer in `reasoning`, so it is left out
    /// rather than shipped as a cleanup pass that quietly changes nothing.
    static let cleanupModels: [(key: String, id: String, label: String)] = [
        (key: "llama-8b", id: "meta-llama/Llama-3.1-8B-Instruct", label: "Llama 3.1 8B (fastest)"),
        (key: "llama-70b", id: "meta-llama/Llama-3.3-70B-Instruct", label: "Llama 3.3 70B"),
        (key: "qwen-235b", id: "Qwen/Qwen3-235B-A22B-Instruct-2507", label: "Qwen3 235B A22B"),
    ]
    static let defaultCleanupModelKey = "llama-8b"

    static func model(_ key: String) throws -> CloudModel {
        guard let model = catalog.first(where: { $0.key == key }) else {
            throw CloudProviderError.unknownModel(key)
        }
        return model
    }

    /// Encodes one transcription call. Pure, so the wire shape verified against
    /// the live API is an executable contract rather than a comment.
    static func transcription(model key: String, audio: Data, contentType: String = "audio/wav") throws -> CloudRequestPlan {
        let model = try model(key)
        guard let url = URL(string: "\(host)/\(inferenceProvider)/models/\(model.id)") else {
            throw CloudProviderError.unknownModel(key)
        }
        return CloudRequestPlan(
            url: url,
            method: "POST",
            headers: [("Content-Type", contentType)],
            body: audio
        )
    }

    /// The transcript sits at the top level. `chunks` only appears when
    /// return_timestamps is requested, which this client never does.
    static func readText(_ data: Data) -> String? {
        guard let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        return object["text"] as? String
    }

    static func cleanup(model id: String, system: String, text: String) throws -> CloudRequestPlan {
        guard let url = URL(string: "\(host)/v1/chat/completions") else {
            throw CloudProviderError.unknownModel(id)
        }
        return CloudRequestPlan(
            url: url,
            method: "POST",
            headers: [("Content-Type", "application/json")],
            body: try CloudHTTP.chatCleanupBody(model: id, system: system, text: text)
        )
    }
}

/// Talks to Hugging Face. Shared by the engine and Test Connection so both
/// agree on what a working key means.
struct HuggingFaceClient: CloudTranscriber {
    static var catalog: [CloudModel] { HuggingFaceRequest.catalog }

    let token: String

    private var key: String {
        get throws {
            let trimmed = token.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !trimmed.isEmpty else { throw CloudProviderError.notConfigured(.huggingface) }
            return trimmed
        }
    }

    func transcribe(fileURL: URL, query: [URLQueryItem]) async throws -> String {
        let key = try key
        let values = CloudHTTP.values(query)
        let modelKey = values["model"] ?? HuggingFaceRequest.defaultModelKey
        let audio = try Data(contentsOf: fileURL)

        let plan = try HuggingFaceRequest.transcription(model: modelKey, audio: audio)
        let data = try await CloudHTTP.send(plan, provider: .huggingface, bearer: key, timeout: 300)
        var text = (HuggingFaceRequest.readText(data) ?? "").trimmingCharacters(in: .whitespacesAndNewlines)

        // The recognizer cannot see the vocabulary, but the cleanup pass can
        // still be told not to rewrite those spellings.
        if values["cleanup"] == "1", !text.isEmpty {
            let terms = CloudHTTP.parseTerms(values["vocabulary"] ?? "")
            text = try await cleanup(text: text, modelKey: values["cleanup_model"] ?? HuggingFaceRequest.defaultCleanupModelKey, terms: terms, key: key)
        }
        return text
    }

    private func cleanup(text: String, modelKey: String, terms: [String], key: String) async throws -> String {
        let id = HuggingFaceRequest.cleanupModels.first(where: { $0.key == modelKey })?.id
            ?? HuggingFaceRequest.cleanupModels.first(where: { $0.key == HuggingFaceRequest.defaultCleanupModelKey })!.id
        let plan = try HuggingFaceRequest.cleanup(
            model: id,
            system: CloudHTTP.cleanupSystem(terms: terms),
            text: text
        )
        // A cleanup failure must not lose a transcript the user already spoke.
        guard let data = try? await CloudHTTP.send(plan, provider: .huggingface, bearer: key, timeout: 120),
              let cleaned = CloudHTTP.readChatText(data)?.trimmingCharacters(in: .whitespacesAndNewlines),
              !cleaned.isEmpty
        else { return text }
        return cleaned
    }

    /// One real inference against the selected model. There is no
    /// credential-only ASR route, and `/api/whoami-v2` proves the token exists
    /// without proving it may call Inference Providers, so the check that
    /// matters is the call the engine will actually make.
    func validateConnection() async throws -> [String] {
        let key = try key
        let plan = try HuggingFaceRequest.transcription(
            model: HuggingFaceRequest.defaultModelKey,
            audio: Self.silentWAV
        )
        _ = try await CloudHTTP.send(plan, provider: .huggingface, bearer: key, timeout: 60)
        return HuggingFaceRequest.catalog.map(\.key)
    }

    /// A quarter second of 16 kHz silence: the smallest well-formed WAV that
    /// exercises the same route as a real dictation. Whisper returns an empty
    /// or near-empty transcript, which is a pass, because the check is that the
    /// key and model resolved rather than what was heard.
    static let silentWAV: Data = {
        let sampleRate = 16_000
        let frames = sampleRate / 4
        let dataBytes = frames * 2
        var wav = Data()
        func append(_ text: String) { wav.append(contentsOf: Array(text.utf8)) }
        func append32(_ value: Int) { wav.append(contentsOf: (0..<4).map { UInt8((value >> ($0 * 8)) & 0xFF) }) }
        func append16(_ value: Int) { wav.append(contentsOf: (0..<2).map { UInt8((value >> ($0 * 8)) & 0xFF) }) }
        append("RIFF"); append32(36 + dataBytes); append("WAVE")
        append("fmt "); append32(16); append16(1); append16(1)
        append32(sampleRate); append32(sampleRate * 2); append16(2); append16(16)
        append("data"); append32(dataBytes)
        wav.append(Data(count: dataBytes))
        return wav
    }()
}

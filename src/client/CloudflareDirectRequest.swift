import Foundation

/// How Workers AI's REST API wants one model's audio on the wire.
///
/// The Worker never faces this choice: `env.AI.run` is an in-process binding,
/// so `src/core/models.js` can hand Nova-3 a `{ body: ReadableStream }` object
/// that no HTTP request can serialize. Over REST the same audio is the request
/// body itself and the options move to the query string. A direct-API client
/// that copies the binding's shape gets a 400 naming the two properties it can
/// never satisfy from JSON, so the wire form is declared per model here.
enum CloudflareAudioWire: Equatable {
    /// Audio is the raw request body; options ride in the query string.
    case rawBody
    /// `{"audio": "<base64>", ...options}`
    case base64JSON
    /// `{"audio": [0, 255, ...], ...options}`
    case byteArrayJSON
}

/// One model option, in a form both wire channels can carry.
enum CloudflareDirectOption: Equatable {
    case flag(Bool)
    case text(String)
    case list([String])
}

/// A transcription model as the REST API sees it. Mirrors one entry of
/// `src/core/models.js`, which stays authoritative for the Worker path.
struct CloudflareDirectModel {
    let id: String
    let wire: CloudflareAudioWire
    /// nil when the model accepts any language the client can display.
    let languages: [String]?
    /// Ordered so an encoded request is byte-stable and testable.
    let options: (_ language: String, _ terms: [String]) -> [(String, CloudflareDirectOption)]
    let readText: ([String: Any]) -> String?
}

/// Everything `URLSession` needs for one Workers AI REST call.
struct CloudflareDirectPlan: Equatable {
    let modelID: String
    /// Already percent-encoded, so it can be assigned straight to
    /// `URLComponents.percentEncodedQuery`. nil when the model takes no query
    /// options. See `CloudflareDirectRequest.queryString` for why the encoder
    /// does not leave this to `URLComponents`.
    let percentEncodedQuery: String?
    let contentType: String
    let body: Data
}

enum CloudflareDirectRequest {
    enum EncodingError: LocalizedError, Equatable {
        case unknownModel(String)

        var errorDescription: String? {
            switch self {
            case let .unknownModel(key): return "Unknown transcription model: \(key)"
            }
        }
    }

    /// The registry the Direct API path reads for everything model-specific:
    /// wire form, options, offered languages, and where the transcript sits in
    /// the response. Nothing else in the client may hardcode these.
    static let models: [String: CloudflareDirectModel] = [
        "nova-3": CloudflareDirectModel(
            id: "@cf/deepgram/nova-3",
            // Verified live: JSON is rejected in every shape, including the
            // binding's `{body, contentType}`. Raw bytes are the only input
            // this model's REST endpoint accepts.
            wire: .rawBody,
            languages: ["en", "es", "fr", "de", "it", "pt", "nl", "hi", "ru", "ja"],
            options: { language, terms in
                var options: [(String, CloudflareDirectOption)] = [
                    ("punctuate", .flag(true)),
                    ("smart_format", .flag(true)),
                    ("numerals", .flag(true)),
                ]
                if language == "auto" {
                    options.append(("detect_language", .flag(true)))
                } else {
                    options.append(("language", .text(language)))
                    // Language detection routes to the multilingual Nova-3,
                    // which rejects keyterm outright, so boosting requires a
                    // pinned language.
                    if !terms.isEmpty { options.append(("keyterm", .list(terms))) }
                }
                return options
            },
            readText: { result in
                let channels = (result["results"] as? [String: Any])?["channels"] as? [[String: Any]]
                let alternatives = channels?.first?["alternatives"] as? [[String: Any]]
                return alternatives?.first?["transcript"] as? String
            }
        ),

        "whisper-turbo": CloudflareDirectModel(
            id: "@cf/openai/whisper-large-v3-turbo",
            wire: .base64JSON,
            languages: nil,
            options: { language, terms in
                var options: [(String, CloudflareDirectOption)] = [
                    ("task", .text("transcribe")),
                    ("vad_filter", .flag(true)),
                ]
                if language != "auto" { options.append(("language", .text(language))) }
                if !terms.isEmpty {
                    options.append(("initial_prompt", .text("Glossary: \(terms.joined(separator: ", ")).")))
                }
                return options
            },
            readText: { result in
                (result["text"] as? String)
                    ?? ((result["transcription_info"] as? [String: Any])?["text"] as? String)
            }
        ),

        "whisper": CloudflareDirectModel(
            id: "@cf/openai/whisper",
            wire: .byteArrayJSON,
            // Accepts the language parameter and discards it, so auto is the
            // only honest offer.
            languages: [],
            options: { _, _ in [] },
            readText: { $0["text"] as? String }
        ),

        "whisper-tiny-en": CloudflareDirectModel(
            id: "@cf/openai/whisper-tiny-en",
            wire: .byteArrayJSON,
            languages: ["en"],
            options: { _, _ in [] },
            readText: { $0["text"] as? String }
        ),
    ]

    /// Registry order for the model picker, matching `src/core/models.js`.
    static let modelKeys = ["nova-3", "whisper-turbo", "whisper", "whisper-tiny-en"]

    static func model(_ key: String) throws -> CloudflareDirectModel {
        guard let model = models[key] else { throw EncodingError.unknownModel(key) }
        return model
    }

    /// Encodes one transcription call. Pure, so the wire shape each model was
    /// verified against is an executable contract rather than a comment.
    static func transcription(
        model key: String,
        audio: Data,
        contentType: String = "audio/wav",
        language: String,
        terms: [String]
    ) throws -> CloudflareDirectPlan {
        let model = try model(key)
        let options = model.options(language, terms)

        switch model.wire {
        case .rawBody:
            return CloudflareDirectPlan(
                modelID: model.id,
                percentEncodedQuery: queryString(options),
                contentType: contentType,
                body: audio
            )
        case .base64JSON:
            return try jsonPlan(model: model, audio: audio.base64EncodedString(), options: options)
        case .byteArrayJSON:
            return try jsonPlan(model: model, audio: audio.map(Int.init), options: options)
        }
    }

    private static func jsonPlan(
        model: CloudflareDirectModel,
        audio: Any,
        options: [(String, CloudflareDirectOption)]
    ) throws -> CloudflareDirectPlan {
        var payload: [String: Any] = ["audio": audio]
        for (name, value) in options {
            switch value {
            case let .flag(flag): payload[name] = flag
            case let .text(text): payload[name] = text
            case let .list(list): payload[name] = list
            }
        }
        return CloudflareDirectPlan(
            modelID: model.id,
            percentEncodedQuery: nil,
            contentType: "application/json",
            // Sorted keys keep an encoded body reproducible across runs.
            body: try JSONSerialization.data(withJSONObject: payload, options: [.sortedKeys])
        )
    }

    /// Deepgram reads repeated parameters as a list, which is how the query
    /// channel carries `keyterm`.
    ///
    /// Encoded here rather than by `URLComponents`, which leaves `+` literal in
    /// a query value because it is a legal sub-delimiter. A form decoder reads
    /// that `+` as a space, so a vocabulary term like "C++" would reach the
    /// model as "C  ". Escaping down to the unreserved set removes the
    /// ambiguity for every character a user can type.
    private static func queryString(_ options: [(String, CloudflareDirectOption)]) -> String? {
        let pairs = options.flatMap { name, value -> [String] in
            let key = escape(name)
            switch value {
            case let .flag(flag): return ["\(key)=\(flag ? "true" : "false")"]
            case let .text(text): return ["\(key)=\(escape(text))"]
            case let .list(list): return list.map { "\(key)=\(escape($0))" }
            }
        }
        return pairs.isEmpty ? nil : pairs.joined(separator: "&")
    }

    private static let unreserved = CharacterSet(charactersIn: "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")

    private static func escape(_ value: String) -> String {
        value.addingPercentEncoding(withAllowedCharacters: unreserved) ?? value
    }
}

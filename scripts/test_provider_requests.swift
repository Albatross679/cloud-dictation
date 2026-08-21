import Foundation

// Executable contract for the Hugging Face and OpenRouter wire shapes. Every
// expectation here was verified against a live call to the vendor; the probe
// results are quoted in the doc comments of the two encoder files and in the
// PR that introduced them.
//
// Run with scripts/test_client.sh.
@main
enum ProviderRequestTests {
    static var failures = 0

    static let audio = Data([0x52, 0x49, 0x46, 0x46, 0x00, 0x01, 0xFE, 0xFF])

    static func check(
        _ name: String,
        _ condition: @autoclosure () -> Bool,
        _ detail: @autoclosure () -> String = ""
    ) {
        if condition() {
            print("  ok   \(name)")
        } else {
            failures += 1
            let extra = detail()
            print("  FAIL \(name)\(extra.isEmpty ? "" : ": \(extra)")")
        }
    }

    static func section(_ name: String) { print("\n\(name)") }

    static func payload(_ p: CloudRequestPlan) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: p.body)) as? [String: Any] ?? [:]
    }

    static func header(_ p: CloudRequestPlan, _ name: String) -> String? {
        p.headers.first(where: { $0.0 == name })?.1
    }

    static func main() {
        huggingFaceSendsRawBytes()
        huggingFaceOffersOnlyServedModels()
        huggingFaceReadsItsResponse()
        openRouterSendsBase64ToTheSTTEndpoint()
        openRouterPinsLanguage()
        openRouterRejectsOversizedAudio()
        openRouterReadsItsResponse()
        unknownModelsAreRejected()
        noPlanCarriesACredential()
        cleanupIsEncodedTheSameForBoth()
        featureMatrixIsHonest()
        errorsSeparateKeyFromReachability()

        print("")
        if failures == 0 {
            print("all checks passed")
        } else {
            print("\(failures) check(s) failed")
            exit(1)
        }
    }

    // Live: POST to this URL with the WAV as the body and Content-Type
    // audio/wav returns {"text": "..."} with HTTP 200. The same audio as
    // {"inputs": "<base64>"} also works; raw bytes avoid the 33% overhead.
    static func huggingFaceSendsRawBytes() {
        section("hugging face sends raw audio bytes to the hf-inference route")
        let p = try! HuggingFaceRequest.transcription(model: "whisper-large-v3-turbo", audio: audio)
        check(
            "url",
            p.url.absoluteString == "https://router.huggingface.co/hf-inference/models/openai/whisper-large-v3-turbo",
            p.url.absoluteString
        )
        check("method", p.method == "POST", p.method)
        check("content type", header(p, "Content-Type") == "audio/wav", String(describing: header(p, "Content-Type")))
        check("body is the audio itself", p.body == audio)
        check("body is not JSON", (try? JSONSerialization.jsonObject(with: p.body)) == nil)

        let large = try! HuggingFaceRequest.transcription(model: "whisper-large-v3", audio: audio)
        check(
            "second model id in the path",
            large.url.absoluteString.hasSuffix("/models/openai/whisper-large-v3"),
            large.url.absoluteString
        )
    }

    // The Hub reports exactly two warm ASR models for hf-inference. Every other
    // Whisper size tried answers HTTP 400 "Model not supported by provider
    // hf-inference", so the picker must not offer one.
    static func huggingFaceOffersOnlyServedModels() {
        section("hugging face offers only the models hf-inference serves")
        check("two models", HuggingFaceRequest.catalog.count == 2, "\(HuggingFaceRequest.catalog.count)")
        check(
            "ids",
            HuggingFaceRequest.catalog.map(\.id)
                == ["openai/whisper-large-v3-turbo", "openai/whisper-large-v3"],
            "\(HuggingFaceRequest.catalog.map(\.id))"
        )
        check(
            "default is in the catalog",
            HuggingFaceRequest.catalog.contains { $0.key == HuggingFaceRequest.defaultModelKey }
        )
        // parameters.language returns HTTP 400 from the ASR pipeline, so no
        // model here may claim a pinnable language.
        check(
            "no model claims a pinnable language",
            HuggingFaceRequest.catalog.allSatisfy { $0.languages == [] },
            "\(HuggingFaceRequest.catalog.map { String(describing: $0.languages) })"
        )
        // The silent WAV used by Test Connection must be a real RIFF file.
        let probe = HuggingFaceClient.silentWAV
        check("probe is a RIFF/WAVE file", Array(probe.prefix(4)) == Array("RIFF".utf8) && Array(probe[8..<12]) == Array("WAVE".utf8))
        check("probe is a quarter second of 16 kHz mono", probe.count == 44 + 8_000, "\(probe.count)")
    }

    static func huggingFaceReadsItsResponse() {
        section("hugging face reads the transcript out of its own response shape")
        let body = Data(#"{"text":" He began a confused complaint."}"#.utf8)
        check("text", HuggingFaceRequest.readText(body) == " He began a confused complaint.")
        check("empty result", HuggingFaceRequest.readText(Data("{}".utf8)) == nil)
        check("not JSON", HuggingFaceRequest.readText(Data("boom".utf8)) == nil)
    }

    // Live: this endpoint exists and answers 401 with
    // {"error":{"message":"User not found.","code":401}} for a wrong key, so
    // audio never has to go through chat completions as an input_audio part.
    static func openRouterSendsBase64ToTheSTTEndpoint() {
        section("openrouter sends base64 audio to the dedicated STT endpoint")
        let p = try! OpenRouterRequest.transcription(model: "whisper-large-v3-turbo", audio: audio, language: "auto")
        check(
            "url",
            p.url.absoluteString == "https://openrouter.ai/api/v1/audio/transcriptions",
            p.url.absoluteString
        )
        check("not the chat route", !p.url.absoluteString.contains("chat/completions"))
        check("method", p.method == "POST", p.method)
        check("content type", header(p, "Content-Type") == "application/json", String(describing: header(p, "Content-Type")))

        let body = payload(p)
        check("model id", body["model"] as? String == "openai/whisper-large-v3-turbo", String(describing: body["model"]))
        let input = body["input_audio"] as? [String: Any] ?? [:]
        check("audio is base64", input["data"] as? String == audio.base64EncodedString())
        check("format", input["format"] as? String == "wav", String(describing: input["format"]))
        check("no language under auto", body["language"] == nil)
        check("carries nothing else", Set(body.keys) == ["model", "input_audio"], "\(body.keys.sorted())")
    }

    static func openRouterPinsLanguage() {
        section("openrouter pins an ISO-639-1 language when one is selected")
        let body = payload(try! OpenRouterRequest.transcription(model: "nova-3", audio: audio, language: "ja"))
        check("language", body["language"] as? String == "ja", String(describing: body["language"]))
        check("model id", body["model"] as? String == "deepgram/nova-3", String(describing: body["model"]))

        // "auto" is not an ISO-639-1 code, so it must never reach the wire.
        for key in OpenRouterRequest.catalog.map(\.key) {
            let auto = payload(try! OpenRouterRequest.transcription(model: key, audio: audio, language: "auto"))
            check("\(key) omits language under auto", auto["language"] == nil, String(describing: auto["language"]))
        }
    }

    // OpenRouter caps uploads at 25 MB. Rejecting locally names a size the user
    // recognises instead of surfacing the vendor's truncated upload error.
    static func openRouterRejectsOversizedAudio() {
        section("openrouter rejects audio past its 25 MB cap before sending")
        let big = Data(count: OpenRouterRequest.maxAudioBytes + 1)
        do {
            _ = try OpenRouterRequest.transcription(model: "whisper-large-v3", audio: big, language: "auto")
            check("throws", false, "no error thrown")
        } catch let error as CloudProviderError {
            check(
                "throws audioTooLarge",
                error == .audioTooLarge(.openrouter, big.count, OpenRouterRequest.maxAudioBytes),
                "\(error)"
            )
        } catch {
            check("throws audioTooLarge", false, "\(error)")
        }

        let atLimit = Data(count: OpenRouterRequest.maxAudioBytes)
        check(
            "exactly at the cap is allowed",
            (try? OpenRouterRequest.transcription(model: "whisper-large-v3", audio: atLimit, language: "auto")) != nil
        )
    }

    static func openRouterReadsItsResponse() {
        section("openrouter reads the transcript out of its own response shape")
        let body = Data(#"{"text":"ask not","usage":{"seconds":9.2,"cost":0.000508}}"#.utf8)
        check("text", OpenRouterRequest.readText(body) == "ask not")
        check("empty result", OpenRouterRequest.readText(Data("{}".utf8)) == nil)
    }

    static func unknownModelsAreRejected() {
        section("an unknown model is rejected before the request goes out")
        for (name, encode) in [
            ("hugging face", { try HuggingFaceRequest.transcription(model: "whisper-medium", audio: audio) }),
            ("openrouter", { try OpenRouterRequest.transcription(model: "whisper-medium", audio: audio, language: "auto") }),
        ] as [(String, () throws -> CloudRequestPlan)] {
            do {
                _ = try encode()
                check("\(name) throws", false, "no error thrown")
            } catch let error as CloudProviderError {
                check("\(name) throws unknownModel", error == .unknownModel("whisper-medium"), "\(error)")
            } catch {
                check("\(name) throws unknownModel", false, "\(error)")
            }
        }
    }

    // A plan is compared, logged, and asserted on in these tests. The bearer
    // token is attached by CloudHTTP.send at the last moment precisely so it
    // cannot end up in one.
    static func noPlanCarriesACredential() {
        section("no encoded plan carries an API key")
        let secret = "hf_averysecrettokenvalue"
        let plans: [CloudRequestPlan] = [
            try! HuggingFaceRequest.transcription(model: "whisper-large-v3", audio: audio),
            try! OpenRouterRequest.transcription(model: "whisper-large-v3", audio: audio, language: "en"),
            try! HuggingFaceRequest.cleanup(model: "meta-llama/Llama-3.1-8B-Instruct", system: "s", text: "t"),
            try! OpenRouterRequest.cleanup(model: "google/gemini-2.5-flash", system: "s", text: "t"),
            try! OpenRouterRequest.keyProbe(),
        ]
        for plan in plans {
            let host = plan.url.host ?? ""
            check("\(host)\(plan.url.path) has no Authorization header", header(plan, "Authorization") == nil)
            check("\(host)\(plan.url.path) url has no query", plan.url.query == nil, String(describing: plan.url.query))
            let rendered = plan.headers.map { "\($0.0): \($0.1)" }.joined() + plan.url.absoluteString
            check("\(host)\(plan.url.path) cannot leak a key", !rendered.contains(secret))
        }
    }

    static func cleanupIsEncodedTheSameForBoth() {
        section("both providers encode the cleanup pass as one OpenAI-shaped call")
        let hf = try! HuggingFaceRequest.cleanup(model: "meta-llama/Llama-3.1-8B-Instruct", system: "rules", text: "um hello")
        let or = try! OpenRouterRequest.cleanup(model: "google/gemini-2.5-flash", system: "rules", text: "um hello")
        check("hf url", hf.url.absoluteString == "https://router.huggingface.co/v1/chat/completions", hf.url.absoluteString)
        check("openrouter url", or.url.absoluteString == "https://openrouter.ai/api/v1/chat/completions", or.url.absoluteString)

        for (name, plan, id) in [
            ("hugging face", hf, "meta-llama/Llama-3.1-8B-Instruct"),
            ("openrouter", or, "google/gemini-2.5-flash"),
        ] {
            let body = payload(plan)
            check("\(name) model", body["model"] as? String == id, String(describing: body["model"]))
            let messages = body["messages"] as? [[String: Any]] ?? []
            check("\(name) system then user", messages.map { $0["role"] as? String } == ["system", "user"], "\(messages.count)")
            check("\(name) system content", messages.first?["content"] as? String == "rules")
            check("\(name) user content", messages.last?["content"] as? String == "um hello")
            check("\(name) temperature", (body["temperature"] as? NSNumber)?.doubleValue == 0.1)
        }

        // The terms are appended as known spellings so a recognizer that cannot
        // take a vocabulary still stops the cleanup pass from rewriting them.
        let withTerms = CloudHTTP.cleanupSystem(terms: ["R2", "Vectorize"])
        check("terms appended", withTerms.hasSuffix("misheard: R2, Vectorize."), String(withTerms.suffix(40)))
        check("no terms leaves the prompt alone", CloudHTTP.cleanupSystem(terms: []) == CloudHTTP.cleanupSystem)

        // The reply shape both vendors share.
        let reply = Data(#"{"choices":[{"message":{"role":"assistant","content":"Hello."}}]}"#.utf8)
        check("reads the assistant message", CloudHTTP.readChatText(reply) == "Hello.")
        check("empty reply", CloudHTTP.readChatText(Data("{}".utf8)) == nil)
    }

    // Every unsupported feature must carry a reason, because the settings pane
    // shows that string next to the disabled control. A blank one would render
    // as a control that is off for no stated reason.
    static func featureMatrixIsHonest() {
        section("every unsupported feature states why")
        check("cloudflare is unchanged", CloudProviderFeatures.of(.cloudflare) == CloudProviderFeatures(
            language: .supported, vocabulary: .supported, audioSpeed: .supported,
            cleanup: .supported, usage: .supported
        ))

        let hf = CloudProviderFeatures.of(.huggingface)
        check("hugging face cannot pin a language", !hf.language.isSupported)
        check("hugging face cannot take a vocabulary", !hf.vocabulary.isSupported)
        check("hugging face compresses audio locally", hf.audioSpeed.isSupported)
        check("hugging face can clean up", hf.cleanup.isSupported)
        check("hugging face has no neuron usage", !hf.usage.isSupported)

        let or = CloudProviderFeatures.of(.openrouter)
        check("openrouter can pin a language", or.language.isSupported)
        check("openrouter cannot take a vocabulary", !or.vocabulary.isSupported)
        check("openrouter can clean up", or.cleanup.isSupported)
        check("openrouter has no neuron usage", !or.usage.isSupported)

        for provider in CloudProvider.allCases {
            let f = CloudProviderFeatures.of(provider)
            for (name, support) in [
                ("language", f.language), ("vocabulary", f.vocabulary),
                ("audio speed", f.audioSpeed), ("cleanup", f.cleanup), ("usage", f.usage),
            ] {
                if let reason = support.reason {
                    check("\(provider.rawValue) \(name) reason is a sentence", reason.count > 20 && reason.hasSuffix("."), reason)
                }
            }
        }

        // Each provider owns a distinct Keychain entry, so no two share a key.
        let accounts = CloudProvider.allCases.map(\.keychainAccount)
        check("keychain accounts are distinct", Set(accounts).count == accounts.count, "\(accounts)")
        check("an unknown stored value falls back to cloudflare", CloudProvider.named("nonsense") == .cloudflare)
        check("a known stored value is honoured", CloudProvider.named("openrouter") == .openrouter)
    }

    // Test Connection has to tell these three apart, so they must not collapse
    // into one string.
    static func errorsSeparateKeyFromReachability() {
        section("test connection separates a bad key from an unreachable host")
        let invalid = CloudProviderError.invalidKey(.huggingface, "Invalid username or password.")
        let unreachable = CloudProviderError.unreachable(.openrouter, "A server with the specified hostname could not be found.")
        let refused = CloudProviderError.badStatus(.huggingface, 400, "Model not supported by provider hf-inference")

        check("invalid key names the provider", invalid.errorDescription?.contains("Hugging Face rejected this API key") == true, invalid.errorDescription ?? "")
        check("unreachable is worded differently", unreachable.errorDescription?.hasPrefix("Could not reach OpenRouter") == true, unreachable.errorDescription ?? "")
        check("bad status carries the code", refused.errorDescription?.contains("returned 400") == true, refused.errorDescription ?? "")
        check("the three are distinct", invalid != unreachable && invalid != refused && unreachable != refused)
        check(
            "not configured points at settings",
            CloudProviderError.notConfigured(.openrouter).errorDescription?.contains("Settings > Models > Engine") == true
        )

        // The two vendors nest their message differently, and both must reduce
        // to the same one-line detail.
        check(
            "hugging face error shape",
            CloudHTTP.errorMessage(from: #"{"error":"Invalid username or password."}"#) == "Invalid username or password."
        )
        check(
            "openrouter error shape",
            CloudHTTP.errorMessage(from: #"{"error":{"message":"User not found.","code":401}}"#) == "User not found."
        )
        check("a non-JSON body survives", CloudHTTP.errorMessage(from: "Not Found") == "Not Found")
    }
}

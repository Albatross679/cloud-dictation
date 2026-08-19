import Foundation

// Executable contract for the Direct API wire shapes. Every expectation here
// was verified against live Workers AI REST calls on a real account; see the
// per-model table in the PR that introduced CloudflareDirectRequest.swift.
//
// Run with scripts/test_client.sh.
@main
enum DirectRequestTests {
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

    static func plan(_ model: String, language: String = "auto", terms: [String] = []) -> CloudflareDirectPlan {
        try! CloudflareDirectRequest.transcription(
            model: model, audio: audio, language: language, terms: terms
        )
    }

    static func queryString(_ p: CloudflareDirectPlan) -> String {
        p.percentEncodedQuery ?? ""
    }

    static func payload(_ p: CloudflareDirectPlan) -> [String: Any] {
        (try? JSONSerialization.jsonObject(with: p.body)) as? [String: Any] ?? [:]
    }

    static func main() {
        novaSendsRawBytes()
        novaPinsLanguage()
        turboSendsBase64JSON()
        whisperFamilySendsByteArray()
        reservedCharactersInTermsAreEscaped()
        noModelReusesTheBindingShape()
        unknownModelIsRejected()
        eachModelReadsItsOwnResponse()
        pickerMatchesTheEncoder()

        print("")
        if failures == 0 {
            print("all checks passed")
        } else {
            print("\(failures) check(s) failed")
            exit(1)
        }
    }

    // The reported failure: Cloudflare 400 "required properties at '/audio'
    // are 'body,contentType'". Every JSON form of audio is rejected by this
    // model's REST endpoint, including the Worker binding's own object.
    static func novaSendsRawBytes() {
        section("nova-3 sends raw audio bytes with options in the query string")
        let p = plan("nova-3")
        check("model id", p.modelID == "@cf/deepgram/nova-3", p.modelID)
        check("body is the audio itself", p.body == audio)
        check("content type", p.contentType == "audio/wav", p.contentType)
        check("body is not JSON", (try? JSONSerialization.jsonObject(with: p.body)) == nil)
        check(
            "auto-detect options",
            queryString(p) == "punctuate=true&smart_format=true&numerals=true&detect_language=true",
            queryString(p)
        )
    }

    static func novaPinsLanguage() {
        section("nova-3 pins a language and boosts vocabulary only when pinned")
        let pinned = plan("nova-3", language: "en", terms: ["Cloudflare", "Deepgram"])
        check(
            "pinned language with repeated keyterm",
            queryString(pinned)
                == "punctuate=true&smart_format=true&numerals=true&language=en&keyterm=Cloudflare&keyterm=Deepgram",
            queryString(pinned)
        )
        check("no detect_language when pinned", !queryString(pinned).contains("detect_language"))

        // Live: detect_language with keyterm returns 400 "The selected Nova-3
        // model does not support keyterm prompting".
        let auto = plan("nova-3", language: "auto", terms: ["Cloudflare"])
        check("keyterm dropped under auto-detect", !queryString(auto).contains("keyterm"), queryString(auto))
    }

    static func turboSendsBase64JSON() {
        section("whisper-turbo sends base64 audio in a JSON body")
        let p = plan("whisper-turbo")
        check("model id", p.modelID == "@cf/openai/whisper-large-v3-turbo", p.modelID)
        check("content type", p.contentType == "application/json", p.contentType)
        check("no query options", p.percentEncodedQuery == nil)
        let body = payload(p)
        check("audio is base64", body["audio"] as? String == audio.base64EncodedString())
        check("task", body["task"] as? String == "transcribe")
        check("vad_filter", body["vad_filter"] as? Bool == true)
        check("no language under auto", body["language"] == nil)

        let pinned = payload(plan("whisper-turbo", language: "ja", terms: ["Cloudflare", "Deepgram"]))
        check("pinned language", pinned["language"] as? String == "ja")
        check(
            "vocabulary as decoder prompt",
            pinned["initial_prompt"] as? String == "Glossary: Cloudflare, Deepgram.",
            String(describing: pinned["initial_prompt"])
        )
    }

    static func whisperFamilySendsByteArray() {
        section("whisper and whisper-tiny-en send a byte array and take no options")
        let expected = [("whisper", "@cf/openai/whisper"), ("whisper-tiny-en", "@cf/openai/whisper-tiny-en")]
        for (key, id) in expected {
            let p = plan(key, language: "en", terms: ["Cloudflare"])
            check("\(key) model id", p.modelID == id, p.modelID)
            check("\(key) content type", p.contentType == "application/json", p.contentType)
            check("\(key) no query options", p.percentEncodedQuery == nil)
            let body = payload(p)
            check("\(key) audio is a byte array", body["audio"] as? [Int] == audio.map(Int.init))
            check("\(key) carries nothing else", body.count == 1, "\(body.keys.sorted())")
        }
    }

    // URLComponents leaves "+" literal in a query value, and a form decoder
    // reads that as a space, so a term like "C++" would reach Deepgram as "C  ".
    static func reservedCharactersInTermsAreEscaped() {
        section("vocabulary terms are escaped down to the unreserved set")
        let p = plan("nova-3", language: "en", terms: ["C++", "R&D", "a b", "50%", "naïve"])
        check(
            "every reserved character encoded",
            queryString(p)
                == "punctuate=true&smart_format=true&numerals=true&language=en"
                + "&keyterm=C%2B%2B&keyterm=R%26D&keyterm=a%20b&keyterm=50%25&keyterm=na%C3%AFve",
            queryString(p)
        )
    }

    static func noModelReusesTheBindingShape() {
        section("no model reuses the Worker binding's stream shape")
        for key in CloudflareDirectRequest.modelKeys {
            let body = payload(plan(key))
            check("\(key) audio is not a {body, contentType} object", (body["audio"] as? [String: Any]) == nil)
        }
    }

    static func unknownModelIsRejected() {
        section("an unknown model is rejected before the request goes out")
        do {
            _ = try CloudflareDirectRequest.transcription(
                model: "whisper-medium", audio: audio, language: "auto", terms: []
            )
            check("throws", false, "no error thrown")
        } catch let error as CloudflareDirectRequest.EncodingError {
            check("throws unknownModel", error == .unknownModel("whisper-medium"), "\(error)")
        } catch {
            check("throws unknownModel", false, "\(error)")
        }
    }

    static func eachModelReadsItsOwnResponse() {
        section("each model reads the transcript out of its own response shape")
        let nova = try! CloudflareDirectRequest.model("nova-3")
        let novaResponse: [String: Any] = [
            "results": ["channels": [["alternatives": [["transcript": "And so, my fellow Americans."]]]]],
        ]
        check("nova-3", nova.readText(novaResponse) == "And so, my fellow Americans.")

        let turbo = try! CloudflareDirectRequest.model("whisper-turbo")
        check("whisper-turbo text", turbo.readText(["text": "ask not"]) == "ask not")
        check(
            "whisper-turbo fallback",
            turbo.readText(["transcription_info": ["text": "ask not"]]) == "ask not"
        )

        let base = try! CloudflareDirectRequest.model("whisper")
        check("whisper", base.readText(["text": "ask not"]) == "ask not")
        check("whisper on an empty result", base.readText([:]) == nil)
    }

    static func pickerMatchesTheEncoder() {
        section("the picker offers exactly the models the encoder can encode")
        check(
            "registry covers the picker",
            Set(CloudflareDirectRequest.modelKeys) == Set(CloudflareDirectRequest.models.keys)
        )
        check("nova-3 languages", try! CloudflareDirectRequest.model("nova-3").languages?.contains("en") == true)
        check("whisper-turbo takes any language", try! CloudflareDirectRequest.model("whisper-turbo").languages == nil)
        check("whisper cannot be pinned", try! CloudflareDirectRequest.model("whisper").languages == [])
        check("whisper-tiny-en is English only", try! CloudflareDirectRequest.model("whisper-tiny-en").languages == ["en"])
    }
}

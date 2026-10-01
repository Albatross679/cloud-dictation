import Foundation

/// No real network or credentials. Exercise the same URLSession and client
/// methods used by dictation, including failures and cleanup fallback.
final class ProviderStub: URLProtocol {
    static var responses: [(Int, String)] = []
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        guard !Self.responses.isEmpty else {
            client?.urlProtocol(self, didFailWithError: URLError(.cannotConnectToHost))
            return
        }
        let (status, body) = Self.responses.removeFirst()
        let response = HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: ["Content-Type": "application/json"])!
        client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
        client?.urlProtocol(self, didLoad: Data(body.utf8))
        client?.urlProtocolDidFinishLoading(self)
    }
    override func stopLoading() {}
}

@main enum ProviderClientTests {
    static var failures = 0
    static func check(_ name: String, _ ok: Bool) {
        print("  \(ok ? "ok  " : "FAIL") \(name)")
        if !ok { failures += 1 }
    }
    static func main() async throws {
        URLProtocol.registerClass(ProviderStub.self)
        defer { URLProtocol.unregisterClass(ProviderStub.self) }
        let file = FileManager.default.temporaryDirectory.appendingPathComponent("client-test-\(UUID()).wav")
        try HuggingFaceClient.silentWAV.write(to: file)
        defer { try? FileManager.default.removeItem(at: file) }
        let or = OpenRouterClient(token: "fake-test-key")
        let model = [URLQueryItem(name: "model", value: "muse-voice-transcribe-1.0")]
        ProviderStub.responses = [(403, #"{"error":{"message":"18+ age confirmation required"}}"#)]
        do {
            _ = try await or.transcribe(fileURL: file, query: model)
            check("gated model throws", false)
        } catch let error as CloudProviderError {
            check("403 identifies unavailable exact model", error == .modelUnavailable(.openrouter, "meta/muse-voice-transcribe-1.0", "18+ age confirmation required"))
        }
        ProviderStub.responses = [(401, #"{"error":{"message":"User not found."}}"#)]
        do {
            _ = try await or.transcribe(fileURL: file, query: model)
            check("invalid key throws", false)
        } catch let error as CloudProviderError {
            check("401 still identifies invalid key", error == .invalidKey(.openrouter, "User not found."))
        }
        ProviderStub.responses = [(200, #"{"text":"<|speaker:0|> Hello world."}"#)]
        let fish = try await or.transcribe(fileURL: file, query: [URLQueryItem(name: "model", value: "fish-audio-transcribe-1-pro")])
        check("fish client returns only spoken words", fish == "Hello world.")
        for body in [#"{"text":"   "}"#, #"{"text":42}"#, "{}"] {
            ProviderStub.responses = [(200, body)]
            do {
                _ = try await or.transcribe(fileURL: file, query: [])
                check("empty or malformed transcript throws", false)
            } catch let error as CloudProviderError {
                check("empty or malformed transcript throws", error == .emptyTranscript(.openrouter))
            }
        }
        ProviderStub.responses = [(503, #"{"error":{"message":"Upstream unavailable"}}"#)]
        do {
            _ = try await or.transcribe(fileURL: file, query: [])
            check("upstream error throws", false)
        } catch let error as CloudProviderError {
            check("503 is not an invalid key", error == .badStatus(.openrouter, 503, "Upstream unavailable"))
        }
        ProviderStub.responses = [(200, #"{"text":"Keep this transcript."}"#), (503, "cleanup unavailable")]
        let fallback = try await or.transcribe(fileURL: file, query: [URLQueryItem(name: "cleanup", value: "1")])
        check("failed cleanup preserves original transcript", fallback == "Keep this transcript.")
        ProviderStub.responses = [(200, #"{"text":"Original."}"#), (200, #"{"choices":[{"message":{"content":"Cleaned."}}]}"#)]
        let hf = try await HuggingFaceClient(token: "fake-test-key").transcribe(fileURL: file, query: [URLQueryItem(name: "cleanup", value: "1")])
        check("HF cleanup uses returned content", hf == "Cleaned.")
        check("all expected requests consumed", ProviderStub.responses.isEmpty)
        if failures != 0 { exit(1) }
        print("all client checks passed")
    }
}

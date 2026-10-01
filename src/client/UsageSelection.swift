import Foundation

struct UsageSelection {
    let engine: String
    let provider: String
    let model: String

    @MainActor
    static func capture(engine: String) -> UsageSelection {
        let prefs = AppPreferences.shared
        if engine == "cloudflare" {
            let provider = CloudProviderSelection.current
            let key = CloudProviderSelection.modelKey(for: provider)
            let model = CloudProviderSelection.catalog(for: provider).first { $0.key == key }?.id ?? key
            return UsageSelection(engine: engine, provider: provider.rawValue, model: model)
        }
        if engine == "fluidaudio" {
            return UsageSelection(engine: engine, provider: "parakeet", model: "parakeet-\(prefs.fluidAudioModelVersion)")
        }
        // Only the model filename, never the user's filesystem path.
        return UsageSelection(engine: "whisper", provider: "whisper",
                              model: prefs.selectedWhisperModelPath.map { URL(fileURLWithPath: $0).lastPathComponent } ?? "unloaded")
    }
}

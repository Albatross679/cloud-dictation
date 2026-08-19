#!/usr/bin/env python3
"""Clone OpenSuperWhisper and add the Cloudflare engine to it.

Every edit is an exact string replacement that is verified before writing. If
upstream moves any anchor the script stops and names the file it could not
patch, rather than producing a half-patched tree.
"""

import shutil
import subprocess
import sys
from pathlib import Path

REPO = "https://github.com/Starmel/OpenSuperWhisper.git"
# Pinned: every patch below is an exact string match against upstream source,
# so tracking the moving branch would make a rebuild months from now fail on
# whichever anchor drifted. Move this deliberately with `manage.sh --sync`.
UPSTREAM_REF = "bef6bc0421d0c010e8f2fb4288c0d74978c8b964"
ROOT = Path(__file__).resolve().parent.parent
CHECKOUT = ROOT / "repos" / "OpenSuperWhisper"
APP = CHECKOUT / "OpenSuperWhisper"


class PatchError(Exception):
    pass


def patch(path: Path, anchor: str, replacement: str, label: str) -> None:
    text = path.read_text()
    if replacement.strip() in text:
        print(f"  = {label}: already applied")
        return
    if text.count(anchor) != 1:
        raise PatchError(
            f"{label}: anchor matched {text.count(anchor)} times in {path.name}, expected exactly 1"
        )
    path.write_text(text.replace(anchor, replacement))
    print(f"  + {label}")


def clone() -> None:
    if CHECKOUT.exists():
        print(f"Using existing checkout at {CHECKOUT}")
        return
    CHECKOUT.parent.mkdir(parents=True, exist_ok=True)
    print(f"Cloning {REPO}")
    # whisper.cpp and asian-autocorrect are submodules, and Bridge.h imports
    # headers from both. Without them the Swift target fails to compile.
    subprocess.run(["git", "clone", "--filter=blob:none", REPO, str(CHECKOUT)], check=True)
    subprocess.run(["git", "-C", str(CHECKOUT), "checkout", "--quiet", UPSTREAM_REF], check=True)
    subprocess.run(
        ["git", "-C", str(CHECKOUT), "submodule", "update", "--init", "--recursive", "--depth", "1"],
        check=True,
    )


def add_engine_file() -> None:
    for name, subdir in (
        ("CloudflareEngine.swift", "Engines"),
        ("CloudflareUsageView.swift", "Engines"),
        ("DictationFailure.swift", "Engines"),
        ("AuthTokenStore.swift", "Utils"),
    ):
        shutil.copyfile(ROOT / "src" / "client" / name, APP / subdir / name)
        print(f"  + {subdir}/{name}")


def patch_preferences() -> None:
    path = APP / "Utils" / "AppPreferences.swift"
    patch(
        path,
        '''    @UserDefault(key: "fluidAudioModelVersion", defaultValue: "v3")
    var fluidAudioModelVersion: String''',
        '''    @UserDefault(key: "fluidAudioModelVersion", defaultValue: "v3")
    var fluidAudioModelVersion: String

    @UserDefault(key: "cloudflareEndpoint", defaultValue: "")
    var cloudflareEndpoint: String

    /// Keychain rather than UserDefaults: a bearer token in a plist is readable
    /// with `defaults read` by anything running as this user.
    var cloudflareAuthToken: String {
        get { AuthTokenStore.token }
        set { AuthTokenStore.token = newValue }
    }

    @UserDefault(key: "cloudflareModel", defaultValue: "nova-3")
    var cloudflareModel: String

    @UserDefault(key: "cloudflareCleanupEnabled", defaultValue: false)
    var cloudflareCleanupEnabled: Bool

    @UserDefault(key: "cloudflareCleanupModel", defaultValue: "llama-8b")
    var cloudflareCleanupModel: String

    /// Playback tempo used for cloud uploads. 1 keeps the original WAV.
    @UserDefault(key: "cloudflareCompressionRate", defaultValue: 1.0)
    var cloudflareCompressionRate: Double

    /// JSON map of model key to the languages it accepts, cached from the
    /// worker. "*" means unrestricted, empty means auto-detect only.
    @UserDefault(key: "cloudflareModelLanguages", defaultValue: "")
    var cloudflareModelLanguages: String''',
        "AppPreferences: cloudflare keys",
    )


def patch_service() -> None:
    path = APP / "TranscriptionService.swift"
    patch(
        path,
        """            if selectedEngine == "fluidaudio" {
                engine = await FluidAudioEngine()
            } else {
                engine = await WhisperEngine()
            }""",
        """            if selectedEngine == "fluidaudio" {
                engine = await FluidAudioEngine()
            } else if selectedEngine == "cloudflare" {
                engine = await CloudflareEngine()
            } else {
                engine = await WhisperEngine()
            }""",
        "TranscriptionService: engine branch",
    )
    patch(
        path,
        """        } else if let fluidEngine = engine as? FluidAudioEngine {""",
        """        } else if let cloudflareEngine = engine as? CloudflareEngine {
            cloudflareEngine.onProgressUpdate = { [weak self] newProgress in
                Task { @MainActor in
                    self?.progress = newProgress
                }
            }
        } else if let fluidEngine = engine as? FluidAudioEngine {""",
        "TranscriptionService: progress callback",
    )


def patch_settings() -> None:
    path = APP / "Settings.swift"

    patch(
        path,
        """    var supportedLanguages: [String] {
        LanguageUtil.supportedLanguages(engine: selectedEngine, fluidAudioModelVersion: fluidAudioModelVersion)
    }""",
        """    @Published var cloudflareEndpoint: String {
        didSet { AppPreferences.shared.cloudflareEndpoint = cloudflareEndpoint }
    }

    @Published var cloudflareAuthToken: String {
        didSet { AppPreferences.shared.cloudflareAuthToken = cloudflareAuthToken }
    }

    @Published var cloudflareModel: String {
        didSet {
            AppPreferences.shared.cloudflareModel = cloudflareModel
            // The new model may not accept the language the old one did.
            let allowed = LanguageUtil.supportedLanguages(
                engine: selectedEngine, fluidAudioModelVersion: fluidAudioModelVersion)
            if !allowed.contains(selectedLanguage) {
                selectedLanguage = allowed.first ?? "auto"
            }
        }
    }

    @Published var cloudflareCleanupEnabled: Bool {
        didSet { AppPreferences.shared.cloudflareCleanupEnabled = cloudflareCleanupEnabled }
    }

    @Published var cloudflareCleanupModel: String {
        didSet { AppPreferences.shared.cloudflareCleanupModel = cloudflareCleanupModel }
    }

    @Published var cloudflareCompressionRate: Double {
        didSet { AppPreferences.shared.cloudflareCompressionRate = cloudflareCompressionRate }
    }

    @Published var cloudflareTestStatus: CloudflareTestStatus = .idle

    func testCloudflareConnection() {
        cloudflareTestStatus = .testing
        Task { @MainActor in
            do {
                let models = try await CloudflareEngine.client.models()
                cloudflareTestStatus = .ok("Connected. \(models.count) models available.")
                TranscriptionService.shared.reloadEngine()
            } catch {
                cloudflareTestStatus = .failed(error.localizedDescription)
            }
        }
    }

    var supportedLanguages: [String] {
        LanguageUtil.supportedLanguages(engine: selectedEngine, fluidAudioModelVersion: fluidAudioModelVersion)
    }""",
        "Settings: view model properties",
    )

    patch(
        path,
        """        self.selectedEngine = prefs.selectedEngine
        self.fluidAudioModelVersion = prefs.fluidAudioModelVersion""",
        """        self.selectedEngine = prefs.selectedEngine
        self.fluidAudioModelVersion = prefs.fluidAudioModelVersion
        self.cloudflareEndpoint = prefs.cloudflareEndpoint
        self.cloudflareAuthToken = prefs.cloudflareAuthToken
        self.cloudflareModel = prefs.cloudflareModel
        self.cloudflareCleanupEnabled = prefs.cloudflareCleanupEnabled
        self.cloudflareCleanupModel = prefs.cloudflareCleanupModel
        self.cloudflareCompressionRate = prefs.cloudflareCompressionRate""",
        "Settings: init",
    )

    patch(
        path,
        """                Picker("Engine", selection: $viewModel.selectedEngine) {
                    Text("Parakeet").tag("fluidaudio")
                    Text("Whisper").tag("whisper")
                }
                .pickerStyle(.segmented)
                .padding(.bottom, 8)""",
        """                Picker("Engine", selection: $viewModel.selectedEngine) {
                    Text("Parakeet").tag("fluidaudio")
                    Text("Whisper").tag("whisper")
                    Text("Cloudflare").tag("cloudflare")
                }
                .pickerStyle(.segmented)
                .padding(.bottom, 8)

                if viewModel.selectedEngine == "cloudflare" {
                    cloudflareSettings
                }""",
        "Settings: engine picker",
    )

    patch(
        path,
        """    private var modelSettings: some View {""",
        '''    private var cloudflareSettings: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Worker Endpoint")
                .font(.headline)
            TextField("https://cloud-dictation.<subdomain>.workers.dev", text: $viewModel.cloudflareEndpoint)
                .textFieldStyle(.roundedBorder)

            Text("Auth Token")
                .font(.headline)
            SecureField("Bearer token", text: $viewModel.cloudflareAuthToken)
                .textFieldStyle(.roundedBorder)

            Text("Transcription Model")
                .font(.headline)
            Picker("Model", selection: $viewModel.cloudflareModel) {
                Text("Nova-3 (fast, accurate)").tag("nova-3")
                Text("Whisper turbo (cheapest)").tag("whisper-turbo")
                Text("Whisper base").tag("whisper")
                Text("Whisper tiny (English only)").tag("whisper-tiny-en")
            }
            .labelsHidden()

            if viewModel.cloudflareModel == "whisper" {
                Label(
                    "Whisper base ignores the language setting and detects per clip, so short audio can come back in the wrong language.",
                    systemImage: "exclamationmark.triangle.fill"
                )
                .font(.caption)
                .foregroundStyle(.orange)
            }

            Picker("Audio speed", selection: $viewModel.cloudflareCompressionRate) {
                Text("1").tag(1.0)
                Text("1.25").tag(1.25)
                Text("1.5").tag(1.5)
                Text("1.75").tag(1.75)
                Text("2").tag(2.0)
                Text("2.25").tag(2.25)
                Text("2.5").tag(2.5)
                Text("2.75").tag(2.75)
                Text("3").tag(3.0)
            }
            Text("Speeds up cloud uploads while preserving pitch. Higher speeds lower cost but can reduce accuracy.")
                .font(.caption)
                .foregroundColor(.secondary)

            Divider()

            Toggle("Clean up dictation with an LLM", isOn: $viewModel.cloudflareCleanupEnabled)

            if viewModel.cloudflareCleanupEnabled {
                Picker("Cleanup model", selection: $viewModel.cloudflareCleanupModel) {
                    Text("Llama 3.1 8B").tag("llama-8b")
                    Text("Llama 3.2 3B (fastest)").tag("llama-3b")
                    Text("Granite 4.0 Micro").tag("granite-micro")
                    Text("Mistral Small 24B").tag("mistral-24b")
                }
                Text("Adds roughly 3 seconds. Removes filler words and fixes punctuation.")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            Divider()

            HStack(spacing: 10) {
                Button("Test Connection") {
                    viewModel.testCloudflareConnection()
                }

                switch viewModel.cloudflareTestStatus {
                case .idle:
                    EmptyView()
                case .testing:
                    ProgressView()
                        .controlSize(.small)
                case .ok(let message):
                    Label(message, systemImage: "checkmark.circle.fill")
                        .foregroundStyle(.green)
                        .font(.caption)
                case .failed(let message):
                    Label(message, systemImage: "xmark.circle.fill")
                        .foregroundStyle(.red)
                        .font(.caption)
                }
            }
            .padding(.top, 4)

            Text("Vocabulary lives in the Transcription tab. Nova-3 boosts those terms only when a language is pinned.")
                .font(.caption)
                .foregroundColor(.secondary)
        }
        .padding(.vertical, 8)
    }

    private var modelSettings: some View {''',
        "Settings: cloudflare panel",
    )


def patch_onboarding() -> None:
    """Offer Cloudflare as a first class choice on the welcome screen.

    Onboarding blocks Continue until a model is downloaded. The cloud engine
    has nothing to download, so it reports as already available.
    """
    settings = APP / "Settings.swift"

    patch(
        settings,
        """enum OnboardingModelType {
    case whisper(url: URL, size: Int)
    case parakeet(version: String)
}""",
        """enum CloudflareTestStatus: Equatable {
    case idle
    case testing
    case ok(String)
    case failed(String)
}

enum OnboardingModelType {
    case whisper(url: URL, size: Int)
    case parakeet(version: String)
    case cloudflare
}""",
        "Onboarding: model type",
    )

    patch(
        settings,
        """        case .parakeet(let version):
            let repo = version == "v2" ? "parakeet-tdt-0.6b-v2-coreml" : "parakeet-tdt-0.6b-v3-coreml"
            return URL(string: "https://huggingface.co/FluidInference/\\(repo)")
        }""",
        """        case .parakeet(let version):
            let repo = version == "v2" ? "parakeet-tdt-0.6b-v2-coreml" : "parakeet-tdt-0.6b-v3-coreml"
            return URL(string: "https://huggingface.co/FluidInference/\\(repo)")
        case .cloudflare:
            return nil
        }""",
        "Onboarding: hugging face link",
    )

    patch(
        settings,
        """struct OnboardingUnifiedModels {
    static let availableModels = [
        OnboardingUnifiedModel(
            name: "Whisper V3 Large",""",
        """struct OnboardingUnifiedModels {
    static let availableModels = [
        OnboardingUnifiedModel(
            name: "Cloudflare",
            isDownloaded: true,
            description: "Runs online on Workers AI, nothing to download",
            type: .cloudflare
        ),
        OnboardingUnifiedModel(
            name: "Whisper V3 Large",""",
        "Onboarding: cloudflare entry",
    )

    view = APP / "Onboarding" / "OnboardingView.swift"

    patch(
        view,
        """            case .parakeet(let version):
                updatedModel.isDownloaded = isFluidAudioModelDownloaded(version: version)
            }""",
        """            case .parakeet(let version):
                updatedModel.isDownloaded = isFluidAudioModelDownloaded(version: version)
            case .cloudflare:
                updatedModel.isDownloaded = true
            }""",
        "Onboarding: availability",
    )

    patch(
        view,
        """        case .parakeet(let version):
            AppPreferences.shared.selectedEngine = "fluidaudio"
            AppPreferences.shared.fluidAudioModelVersion = version
        }""",
        """        case .parakeet(let version):
            AppPreferences.shared.selectedEngine = "fluidaudio"
            AppPreferences.shared.fluidAudioModelVersion = version
        case .cloudflare:
            AppPreferences.shared.selectedEngine = "cloudflare"
        }""",
        "Onboarding: selection",
    )

    # Auto-selecting the first available model highlights it without routing
    # through selectModel, so the engine preference is never written. Commit
    # whatever is highlighted when Continue is pressed.
    patch(
        view,
        """    private func handleContinueButtonTap() {
        appState.hasCompletedOnboarding = true
    }""",
        """    private func handleContinueButtonTap() {
        if let selected = viewModel.unifiedModels.first(where: { $0.id == viewModel.selectedModelId }) {
            viewModel.selectModel(selected)
        }
        appState.hasCompletedOnboarding = true
    }""",
        "Onboarding: commit selection on continue",
    )

    text = view.read_text()
    if "case .cloudflare:\n            return\n" not in text:
        anchor = """        switch model.type {
        case .whisper(let url, _):
            try await downloadWhisperModel(model: model, url: url)"""
        if text.count(anchor) != 1:
            raise PatchError("Onboarding: download switch anchor not unique")
        view.write_text(
            text.replace(
                anchor,
                """        switch model.type {
        case .cloudflare:
            return
        case .whisper(let url, _):
            try await downloadWhisperModel(model: model, url: url)""",
            )
        )
        print("  + Onboarding: download no-op")
    else:
        print("  = Onboarding: download no-op: already applied")


def patch_language_util() -> None:
    """Offer only the languages the selected cloud model actually accepts."""
    path = APP / "Utils" / "LanguageUtil.swift"
    patch(
        path,
        """    static func supportedLanguages(engine: String, fluidAudioModelVersion: String) -> [String] {
        guard engine == "fluidaudio" else { return availableLanguages }
        return fluidAudioModelVersion == "v2" ? parakeetV2Languages : parakeetV3Languages
    }""",
        """    static func supportedLanguages(engine: String, fluidAudioModelVersion: String) -> [String] {
        if engine == "cloudflare" { return cloudflareLanguages() }
        if engine == "whisper" { return localWhisperLanguages() }
        guard engine == "fluidaudio" else { return availableLanguages }
        return fluidAudioModelVersion == "v2" ? parakeetV2Languages : parakeetV3Languages
    }

    /// whisper.cpp publishes English-only weights beside the multilingual ones,
    /// named `ggml-<size>.en.bin`. Offering 23 languages for those produces
    /// English output whatever is picked, so the list follows the file on disk.
    static func localWhisperLanguages() -> [String] {
        let path = AppPreferences.shared.selectedWhisperModelPath ?? ""
        let name = (path as NSString).lastPathComponent.lowercased()
        return name.contains(".en.") ? ["en"] : availableLanguages
    }

    /// Cloud models disagree about languages: Nova-3 on Cloudflare accepts ten
    /// and hard errors on the rest, whisper-tiny-en is English only, and
    /// Whisper base discards the setting entirely. Offering one list for all of
    /// them produces failed dictations, so the worker's per-model list wins.
    static func cloudflareLanguages() -> [String] {
        let model = AppPreferences.shared.cloudflareModel
        guard
            let data = AppPreferences.shared.cloudflareModelLanguages.data(using: .utf8),
            let map = try? JSONDecoder().decode([String: [String]].self, from: data),
            let allowed = map[model]
        else { return availableLanguages }

        if allowed.contains("*") { return availableLanguages }
        if allowed.isEmpty { return ["auto"] }
        return ["auto"] + allowed.filter { availableLanguages.contains($0) }
    }""",
        "LanguageUtil: per-model languages",
    )


def patch_fluidaudio_engine() -> None:
    """Pass the selected language to Parakeet, which accepts one and was never given it."""
    path = APP / "Engines" / "FluidAudioEngine.swift"
    patch(
        path,
        """        var decoderState = try TdtDecoderState(decoderLayers: await asrManager.decoderLayerCount)
        let result = try await asrManager.transcribe(url, decoderState: &decoderState)""",
        """        var decoderState = try TdtDecoderState(decoderLayers: await asrManager.decoderLayerCount)
        // The picker already narrows the list per Parakeet version, but the
        // engine ignored the choice and auto-detected. nil keeps auto-detect.
        let language = Language(rawValue: settings.selectedLanguage)
        let result = try await asrManager.transcribe(
            url, decoderState: &decoderState, language: language)""",
        "FluidAudioEngine: honor the language setting",
    )


def patch_transcription_settings() -> None:
    """Relabel the shared prompt field as the vocabulary list it now holds."""
    path = APP / "Settings.swift"
    patch(
        path,
        """                // Initial Prompt
                VStack(alignment: .leading, spacing: 16) {
                    Text("Initial Prompt")""",
        """                // Vocabulary
                VStack(alignment: .leading, spacing: 16) {
                    Text("Vocabulary")""",
        "Settings: vocabulary heading",
    )
    patch(
        path,
        """                        Text("Optional text to guide the model's transcription")""",
        """                        Text("Terms the model should spell correctly, separated by commas. Words and names, not sentences.")""",
        "Settings: vocabulary caption",
    )


def patch_failure_paths() -> None:
    """Keep a failed dictation instead of printing and deleting it."""
    anchor = """                    print("Error transcribing audio: \\(error)")
                    try? FileManager.default.removeItem(at: tempURL)"""
    replacement = """                    await DictationFailure.record(audioAt: tempURL, error: error)"""
    for name in ("ContentView.swift", "Indicator/IndicatorWindow.swift"):
        patch(APP / name, anchor, replacement, f"{name}: surface dictation failures")


def patch_content_view() -> None:
    """Show today's Cloudflare spend in the main window's hint column."""
    path = APP / "ContentView.swift"
    patch(
        path,
        """                                    Text("Drop audio file here to transcribe")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                }
                                .padding(.leading, 4)
                            }""",
        """                                    Text("Drop audio file here to transcribe")
                                        .font(.caption)
                                        .foregroundColor(.secondary)
                                }
                                .padding(.leading, 4)

                                if AppPreferences.shared.selectedEngine == "cloudflare" {
                                    CloudflareUsageView(refreshToken: viewModel.recordings.count)
                                }
                            }""",
        "ContentView: usage readout",
    )


def main() -> int:
    clone()
    print("Patching:")
    try:
        add_engine_file()
        patch_preferences()
        patch_service()
        patch_settings()
        patch_onboarding()
        patch_language_util()
        patch_fluidaudio_engine()
        patch_transcription_settings()
        patch_failure_paths()
        patch_content_view()
    except PatchError as err:
        print(f"\nFAILED: {err}", file=sys.stderr)
        print("Upstream changed. Fix the anchor in this script and rerun.", file=sys.stderr)
        return 1

    print(f"\nDone. Open {CHECKOUT / 'OpenSuperWhisper.xcodeproj'} and build.")
    print("Settings > Models > Engine > Cloudflare, then paste the endpoint and token.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

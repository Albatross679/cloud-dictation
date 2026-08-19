import AVFoundation
import AppKit
import SwiftUI

/// First-launch connection flow for the no-deploy Workers AI path. The token is
/// held in this view until an actual microphone recording has been transcribed.
@MainActor
final class CloudflareSetupViewModel: ObservableObject {
    enum TestState: Equatable {
        case idle
        case recording
        case transcribing
        case succeeded(String)
        case failed(String)
    }

    @Published var token: String
    @Published var accounts: [CloudflareClient.Account] = []
    @Published var accountID: String
    @Published var state: TestState = .idle

    private var accountDiscoveryTask: Task<Void, Never>?

    init() {
        token = AppPreferences.shared.cloudflareDirectAPIToken
        accountID = AppPreferences.shared.cloudflareAccountID
        if !token.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
            discoverAccounts()
        }
    }

    func discoverAccounts() {
        accountDiscoveryTask?.cancel()
        let candidate = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !candidate.isEmpty else {
            accounts = []
            return
        }

        accountDiscoveryTask = Task { [weak self] in
            try? await Task.sleep(nanoseconds: 700_000_000)
            guard !Task.isCancelled, let self else { return }
            do {
                let found = try await self.client(token: candidate, accountID: "").accounts()
                guard !Task.isCancelled else { return }
                self.accounts = found
                if !found.contains(where: { $0.id == self.accountID }) {
                    self.accountID = found[0].id
                }
            } catch {
                // The Test action explains invalid credentials after a user
                // deliberately finishes pasting, rather than per keystroke.
                self.accounts = []
            }
        }
    }

    func test() {
        guard state != .recording && state != .transcribing else { return }
        state = .idle
        let candidate = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !candidate.isEmpty else {
            state = .failed("Paste a Workers AI API token first.")
            return
        }

        Task {
            do {
                try await ensureMicrophoneAccess()
                guard MicrophoneService.shared.getActiveMicrophone() != nil else {
                    throw SetupError.noMicrophone
                }

                let found = try await client(token: candidate, accountID: "").accounts()
                accounts = found
                if !found.contains(where: { $0.id == accountID }) {
                    accountID = found[0].id
                }

                state = .recording
                AudioRecorder.shared.startRecording()
                try await Task.sleep(nanoseconds: 2_200_000_000)
                guard let recording = await AudioRecorder.shared.stopRecording() else {
                    throw SetupError.noAudio
                }
                defer { try? FileManager.default.removeItem(at: recording) }

                state = .transcribing
                let text = try await client(token: candidate, accountID: accountID).transcribe(
                    fileURL: recording,
                    query: [
                        URLQueryItem(name: "model", value: "nova-3"),
                        URLQueryItem(name: "language", value: "auto"),
                    ]
                ).trimmingCharacters(in: .whitespacesAndNewlines)
                guard !text.isEmpty else { throw SetupError.emptyTranscript }

                let prefs = AppPreferences.shared
                prefs.cloudflareConnectionMode = "direct"
                prefs.cloudflareDirectAPIToken = candidate
                prefs.cloudflareAccountID = accountID
                prefs.selectedEngine = "cloudflare"
                state = .succeeded(text)
            } catch {
                state = .failed(message(for: error))
            }
        }
    }

    private func client(token: String, accountID: String) -> CloudflareClient {
        CloudflareClient(endpoint: "", token: token, accountID: accountID, mode: .directAPI)
    }

    private func ensureMicrophoneAccess() async throws {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return
        case .notDetermined:
            guard await AVCaptureDevice.requestAccess(for: .audio) else { throw SetupError.microphoneDenied }
        case .denied, .restricted:
            throw SetupError.microphoneDenied
        @unknown default:
            throw SetupError.microphoneDenied
        }
    }

    private func message(for error: Error) -> String {
        if let error = error as? SetupError { return error.localizedDescription }
        if let error = error as? URLError {
            return "Network error: \(error.localizedDescription). Check your internet connection and try again."
        }
        let detail = error.localizedDescription
        if detail.contains("401") || detail.contains("403") {
            return "Cloudflare rejected this token. Create a Workers AI API Token with Workers AI – Read and Workers AI – Edit permissions."
        }
        return "Couldn't transcribe the test recording: \(detail)"
    }

    private enum SetupError: LocalizedError {
        case microphoneDenied
        case noMicrophone
        case noAudio
        case emptyTranscript

        var errorDescription: String? {
            switch self {
            case .microphoneDenied:
                return "Microphone access is required. Allow OSW Cloud in System Settings > Privacy & Security > Microphone, then try again."
            case .noMicrophone:
                return "No microphone is available. Connect or select a microphone, then try again."
            case .noAudio:
                return "The microphone did not capture a recording. Check the selected microphone and try again."
            case .emptyTranscript:
                return "Cloudflare returned no words. Speak during the test and try again."
            }
        }
    }
}

struct CloudflareSetupView: View {
    @EnvironmentObject private var appState: AppState
    @StateObject private var viewModel = CloudflareSetupViewModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Spacer(minLength: 8)

            Image(systemName: "waveform.badge.mic")
                .font(.system(size: 38))
                .foregroundStyle(.tint)
            Text("Set up Cloud Dictation")
                .font(.title.bold())
            Text("Dictation runs on your own Cloudflare account, whose free tier covers hours of daily use.")
                .fixedSize(horizontal: false, vertical: true)

            Divider()

            VStack(alignment: .leading, spacing: 8) {
                Button("Create API token") {
                    NSWorkspace.shared.open(URL(string: "https://dash.cloudflare.com/profile/api-tokens")!)
                }
                .buttonStyle(.borderedProminent)

                Text("In Cloudflare, click Workers AI, then Use REST API, then Create a Workers AI API Token. For a custom token, add Workers AI – Read and Workers AI – Edit.")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            VStack(alignment: .leading, spacing: 8) {
                Text("Paste your API token")
                    .font(.headline)
                SecureField("Workers AI API token", text: $viewModel.token)
                    .textFieldStyle(.roundedBorder)
                    .onChange(of: viewModel.token) { _, _ in
                        viewModel.discoverAccounts()
                    }

                if viewModel.accounts.count > 1 {
                    Picker("Cloudflare account", selection: $viewModel.accountID) {
                        ForEach(viewModel.accounts) { account in
                            Text(account.name).tag(account.id)
                        }
                    }
                } else if let account = viewModel.accounts.first {
                    Text("Using Cloudflare account: \(account.name)")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }

            HStack(alignment: .top, spacing: 12) {
                Button(testButtonTitle) {
                    viewModel.test()
                }
                .buttonStyle(.borderedProminent)
                .disabled(isTesting)

                testResult
            }

            if case .succeeded = viewModel.state {
                Button("Start dictating") {
                    appState.completeCloudflareSetup()
                }
                .buttonStyle(.borderedProminent)
            }

            Spacer(minLength: 0)

            Button("Use a local model or Worker mode instead") {
                appState.skipCloudflareSetupToSettings()
            }
            .buttonStyle(.link)
        }
        .padding(28)
    }

    private var isTesting: Bool {
        switch viewModel.state {
        case .recording, .transcribing: true
        default: false
        }
    }

    private var testButtonTitle: String {
        switch viewModel.state {
        case .recording: "Recording…"
        case .transcribing: "Transcribing…"
        default: "Test"
        }
    }

    @ViewBuilder
    private var testResult: some View {
        switch viewModel.state {
        case .idle:
            Text("Records about two seconds from the selected microphone.")
                .font(.caption)
                .foregroundStyle(.secondary)
        case .recording, .transcribing:
            ProgressView()
                .controlSize(.small)
        case .succeeded(let text):
            VStack(alignment: .leading, spacing: 3) {
                Label("Cloudflare heard:", systemImage: "checkmark.circle.fill")
                    .foregroundStyle(.green)
                    .font(.caption.bold())
                Text("“\(text)”")
                    .font(.caption)
                    .textSelection(.enabled)
            }
        case .failed(let message):
            Label(message, systemImage: "xmark.circle.fill")
                .foregroundStyle(.red)
                .font(.caption)
                .fixedSize(horizontal: false, vertical: true)
        }
    }
}

import Foundation

/// Keeps a failed dictation instead of discarding it.
///
/// Upstream logged the error with `print` and deleted the audio, so a failure
/// was indistinguishable from having said nothing: no message, no recording,
/// no way to tell a bad token from a quiet microphone. The history list already
/// renders `.failed` with the reason attached, so the attempt is stored there.
enum DictationFailure {
    @MainActor
    static func record(audioAt tempURL: URL, error: Error) async {
        let duration = await AudioUtil.audioDuration(url: tempURL)
        let timestamp = Date()
        let recording = Recording(
            id: UUID(),
            timestamp: timestamp,
            fileName: "\(Int(timestamp.timeIntervalSince1970)).wav",
            transcription: message(for: error),
            duration: duration,
            status: .failed,
            progress: 0,
            sourceFileURL: nil
        )

        do {
            try AudioRecorder.shared.moveTemporaryRecording(from: tempURL, to: recording.url)
            RecordingStore.shared.addRecording(recording)
        } catch {
            // Storing it failed too, so there is nothing left to keep.
            try? FileManager.default.removeItem(at: tempURL)
        }
    }

    /// `localizedDescription` on a bare Swift error reads as "The operation
    /// couldn't be completed", which says nothing. Errors that carry their own
    /// wording keep it.
    private static func message(for error: Error) -> String {
        if let local = error as? LocalizedError, let description = local.errorDescription {
            return description
        }
        if error is CancellationError {
            return "Cancelled before the transcription finished."
        }
        return "Transcription failed: \(error)"
    }
}

import SwiftUI

struct CloudflareUsage: Decodable {
    struct Today: Decodable {
        let requests: Int
        let audio_seconds: Double
        let neurons: Double
        let free_used_fraction: Double
        let billable_usd: Double
    }
    let today: Today
    let free_neurons_per_day: Double
}

@MainActor
final class CloudflareUsageModel: ObservableObject {
    @Published var usage: CloudflareUsage?
    @Published var failed = false

    func refresh() {
        Task {
            do {
                usage = try await CloudflareEngine.client.usage()
                failed = false
            } catch {
                failed = true
            }
        }
    }
}

/// Compact spend readout for the main window, shown only when transcription
/// runs on Cloudflare. Neurons are Cloudflare's billing unit. The window is a
/// UTC day, not a local one, so it is labelled as such: west of UTC it rolls
/// over during the evening and "today" would read as a bug.
struct CloudflareUsageView: View {
    @StateObject private var model = CloudflareUsageModel()

    /// Bumped by the caller whenever a transcription lands, so the readout
    /// follows dictation without polling.
    let refreshToken: Int

    private var tint: Color {
        guard let fraction = model.usage?.today.free_used_fraction else { return .secondary }
        if fraction >= 1 { return .orange }
        if fraction >= 0.8 { return .yellow }
        return .secondary
    }

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "cloud")
                .foregroundColor(.secondary)
                .imageScale(.medium)

            if let today = model.usage?.today {
                Text(summary(today))
                    .font(.caption)
                    .foregroundColor(tint)
            } else if model.failed {
                Text("Usage unavailable")
                    .font(.caption)
                    .foregroundColor(.secondary)
            } else {
                Text("Loading usage...")
                    .font(.caption)
                    .foregroundColor(.secondary)
            }
        }
        .padding(.leading, 4)
        .help("Neurons are Cloudflare's billing unit. The daily free allowance resets at 00:00 UTC.")
        .onAppear { model.refresh() }
        .onChange(of: refreshToken) { _, _ in model.refresh() }
    }

    private func summary(_ today: CloudflareUsage.Today) -> String {
        let minutes = today.audio_seconds / 60
        let duration = minutes < 1
            ? String(format: "%.0fs", today.audio_seconds)
            : String(format: "%.1f min", minutes)
        let percent = Int((today.free_used_fraction * 100).rounded())

        var line = "Since 00:00 UTC: \(duration) · \(Int(today.neurons.rounded())) neurons · \(percent)% of free tier"
        if today.billable_usd > 0 {
            line += String(format: " · $%.3f billable", today.billable_usd)
        }
        return line
    }
}

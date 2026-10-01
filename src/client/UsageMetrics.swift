import Foundation
import Darwin
import CoreFoundation
import AVFoundation

struct UsageCost: Codable, Equatable {
    enum Kind: String, Codable { case actual, estimate, unknown }
    var kind: Kind = .unknown
    var usd: Double?
    var source: String?
    var rateDate: String?
    var usdPerAudioMinute: Double?

    static func reported(_ data: Data, provider: String) -> UsageCost? {
        guard provider == "openrouter",
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let usage = object["usage"] as? [String: Any],
              let number = usage["cost"] as? NSNumber,
              CFGetTypeID(number) != CFBooleanGetTypeID(),
              number.doubleValue.isFinite, number.doubleValue >= 0 else { return nil }
        return UsageCost(kind: .actual, usd: number.doubleValue,
                         source: "OpenRouter response usage.cost, USD")
    }

    static func audioEstimate(provider: String, model: String, seconds: Double?) -> UsageCost {
        // Gross list-rate value, NOT an invoice or account-wide free-tier calculation.
        let rates = ["@cf/deepgram/nova-3": 0.0052,
                     "@cf/openai/whisper-large-v3-turbo": 0.0005,
                     "@cf/openai/whisper": 0.0005]
        guard provider == "cloudflare", let rate = rates[model],
              let seconds, seconds.isFinite, seconds > 0 else { return UsageCost() }
        return UsageCost(kind: .estimate, usd: seconds / 60 * rate,
                         source: "https://developers.cloudflare.com/workers-ai/platform/pricing/#audio-model-pricing",
                         rateDate: "2026-10-01", usdPerAudioMinute: rate)
    }
}

struct UsageDictation: Codable, Identifiable {
    let id: UUID
    let recordedAt: Date
    let originalSeconds: Double?
    let engine: String
    var provider: String
    var model: String
}

struct UsageRun: Codable, Identifiable {
    let id: UUID
    let dictationID: UUID
    let startedAt: Date
    let engine: String
    var provider: String
    var model: String
    var finishedAt: Date?
    var outcome: String = "inFlight"
}

struct UsageRequest: Codable, Identifiable {
    let id: UUID
    let dictationID: UUID
    let runID: UUID
    let startedAt: Date
    let provider: String
    let model: String
    let phase: String
    let uploadedSeconds: Double?
    var finishedAt: Date?
    var outcome: String = "inFlight"
    var httpStatus: Int?
    var cost = UsageCost()
}

struct UsageArchive: Codable {
    var version = 1
    var trackingStartedAt = Date()
    var incompleteSince: Date?
    var dictations: [UsageDictation] = []
    var runs: [UsageRun] = []
    var requests: [UsageRequest] = []
}

/// Contains identifiers and numbers only. Never accepts transcript/error text,
/// credentials, endpoint URLs, file paths, request bodies or audio bytes.
final class UsageMetricsStore: @unchecked Sendable {
    static let changed = Notification.Name("OSWUsageMetricsChanged")
    static let shared = UsageMetricsStore(url: FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("OSW Cloud/usage-metrics-v1.json"))
    let url: URL
    private let lock = NSLock()
    private var archive = UsageArchive()
    private var storageError: String?
    private var readable = true
    private static let incompleteMessage = "Some usage metrics could not be saved after a storage failure. Totals may be incomplete even though later writes succeeded."

    init(url: URL) {
        self.url = url
        if FileManager.default.fileExists(atPath: url.path) {
            do {
                archive = try JSONDecoder().decode(UsageArchive.self, from: Data(contentsOf: url))
                guard archive.version == 1 else { throw CocoaError(.fileReadCorruptFile) }
                if archive.incompleteSince != nil { storageError = Self.incompleteMessage }
            } catch {
                readable = false
                storageError = "Usage history could not be read. The original file was preserved. Repair it and restart the app."
            }
        }
    }

    func snapshot() -> (archive: UsageArchive, error: String?) {
        lock.lock(); defer { lock.unlock() }
        return (archive, storageError)
    }

    private func update(_ change: (inout UsageArchive) -> Void) {
        lock.lock()
        if readable {
            var next = archive
            change(&next)
            do {
                let parent = url.deletingLastPathComponent()
                try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true,
                                                        attributes: [.posixPermissions: 0o700])
                let temporary = parent.appendingPathComponent(".usage-\(UUID().uuidString).tmp")
                defer { try? FileManager.default.removeItem(at: temporary) }
                let data = try JSONEncoder().encode(next)
                guard FileManager.default.createFile(atPath: temporary.path, contents: data,
                                                     attributes: [.posixPermissions: 0o600]) else {
                    throw CocoaError(.fileWriteUnknown)
                }
                guard rename(temporary.path, url.path) == 0 else { throw CocoaError(.fileWriteUnknown) }
                archive = next
                storageError = archive.incompleteSince == nil ? nil : Self.incompleteMessage
            } catch {
                archive.incompleteSince = archive.incompleteSince ?? Date()
                storageError = "Usage history could not be saved. Dictation still works, but these metrics may be incomplete. Check the local usage file permissions and disk space."
            }
        }
        lock.unlock()
        DispatchQueue.main.async { NotificationCenter.default.post(name: Self.changed, object: nil) }
    }

    func begin(_ dictation: UsageDictation, runID: UUID = UUID(), at date: Date = Date(),
               engine: String, provider: String, model: String) -> UUID {
        update { archive in
            if !archive.dictations.contains(where: { $0.id == dictation.id }) { archive.dictations.append(dictation) }
            if !archive.runs.contains(where: { $0.id == runID }) {
                archive.runs.append(UsageRun(id: runID, dictationID: dictation.id, startedAt: date,
                                            engine: engine, provider: provider, model: model))
            }
        }
        return runID
    }

    func finishRun(_ id: UUID, outcome: String, at date: Date = Date()) {
        update { archive in
            guard let i = archive.runs.firstIndex(where: { $0.id == id }), archive.runs[i].finishedAt == nil else { return }
            archive.runs[i].finishedAt = date
            archive.runs[i].outcome = outcome
        }
    }

    func beginRequest(_ request: UsageRequest) {
        update { archive in
            guard !archive.requests.contains(where: { $0.id == request.id }) else { return }
            // Settings can change while an audio duration is loading. The
            // encoded transcription request identifies the route actually used.
            if request.phase == "transcription" || request.phase == "workerPipeline",
               !archive.requests.contains(where: { $0.runID == request.runID && ($0.phase == "transcription" || $0.phase == "workerPipeline") }),
               let run = archive.runs.firstIndex(where: { $0.id == request.runID }), archive.runs[run].engine == "cloudflare" {
                archive.runs[run].provider = request.provider
                archive.runs[run].model = request.model
                if archive.runs.first(where: { $0.dictationID == request.dictationID })?.id == request.runID,
                   let dictation = archive.dictations.firstIndex(where: { $0.id == request.dictationID }) {
                    archive.dictations[dictation].provider = request.provider
                    archive.dictations[dictation].model = request.model
                }
            }
            archive.requests.append(request)
        }
    }

    func finishRequest(_ id: UUID, outcome: String, status: Int? = nil, cost: UsageCost = UsageCost(), at date: Date = Date()) {
        update { archive in
            guard let i = archive.requests.firstIndex(where: { $0.id == id }), archive.requests[i].finishedAt == nil else { return }
            archive.requests[i].finishedAt = date
            archive.requests[i].outcome = outcome
            archive.requests[i].httpStatus = status
            archive.requests[i].cost = cost
        }
    }
}

struct UsageContext: Sendable {
    let dictationID: UUID
    let runID: UUID
    let store: UsageMetricsStore
}

enum UsageTracking {
    @TaskLocal static var context: UsageContext?

    static func audioSeconds(_ url: URL) async -> Double? {
        let asset = AVURLAsset(url: url)
        guard let duration = try? await asset.load(.duration) else { return nil }
        let seconds = CMTimeGetSeconds(duration)
        return seconds.isFinite && seconds >= 0 ? seconds : nil
    }

    /// A new ID is persisted before every send. Retries get distinct IDs;
    /// duplicate completion callbacks cannot add another charge.
    static func send(provider: String, model: String, phase: String, seconds: Double? = nil,
                     operation: () async throws -> (Data, URLResponse)) async throws -> (Data, URLResponse) {
        guard let context else { return try await operation() }
        let id = UUID()
        context.store.beginRequest(UsageRequest(id: id, dictationID: context.dictationID, runID: context.runID,
                                               startedAt: Date(), provider: provider, model: model,
                                               phase: phase, uploadedSeconds: seconds))
        do {
            let (data, response) = try await operation()
            let status = (response as? HTTPURLResponse)?.statusCode
            let success = status.map { (200...299).contains($0) } ?? false
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let estimateAllowed = success && object != nil && object?["success"] as? Bool != false
            let cost = UsageCost.reported(data, provider: provider)
                ?? (estimateAllowed ? UsageCost.audioEstimate(provider: provider, model: model, seconds: seconds) : UsageCost())
            context.store.finishRequest(id, outcome: success ? "httpSuccess" : "httpFailure", status: status, cost: cost)
            return (data, response)
        } catch {
            context.store.finishRequest(id, outcome: error is CancellationError ? "cancelled" : "transportFailure")
            throw error
        }
    }
}

enum UsagePeriod: String, CaseIterable { case daily = "Daily", weekly = "Weekly", monthly = "Monthly"
    var component: Calendar.Component { self == .daily ? .day : self == .weekly ? .weekOfYear : .month }
}

struct UsageBucket: Identifiable {
    let start: Date
    var id: Date { start }
    var minutes = 0.0
    var actualUSD = 0.0
    var estimatedUSD = 0.0
    var unknownCosts = 0
    var unknownDurations = 0
    var dictations = 0
    var requests = 0
}

enum UsageAggregation {
    /// Calendar boundaries, not fixed 24-hour/7-day intervals. The caller's
    /// calendar supplies the timezone, DST and locale's first weekday.
    static func buckets(_ archive: UsageArchive, start: Date, end: Date, period: UsagePeriod,
                        provider: String? = nil, calendar: Calendar = .current) -> [UsageBucket] {
        guard start < end, let first = calendar.dateInterval(of: period.component, for: start)?.start else { return [] }
        var result: [UsageBucket] = []
        var cursor = first
        while cursor < end {
            result.append(UsageBucket(start: cursor))
            guard let next = calendar.date(byAdding: period.component, value: 1, to: cursor), next > cursor else { break }
            cursor = next
        }
        var indexes: [Date: Int] = [:]
        for i in result.indices { indexes[result[i].start] = i }
        func index(_ date: Date) -> Int? {
            guard date >= start, date < end, let boundary = calendar.dateInterval(of: period.component, for: date)?.start else { return nil }
            return indexes[boundary]
        }
        for dictation in archive.dictations where provider == nil || dictation.provider == provider {
            guard let i = index(dictation.recordedAt) else { continue }
            result[i].dictations += 1
            if let seconds = dictation.originalSeconds, seconds.isFinite, seconds >= 0 { result[i].minutes += seconds / 60 }
            else { result[i].unknownDurations += 1 }
        }
        for request in archive.requests where provider == nil || request.provider == provider {
            guard let i = index(request.startedAt) else { continue }
            result[i].requests += 1
            if let usd = request.cost.usd, usd.isFinite, usd >= 0 {
                switch request.cost.kind {
                case .actual: result[i].actualUSD += usd
                case .estimate: result[i].estimatedUSD += usd
                case .unknown: result[i].unknownCosts += 1
                }
            } else { result[i].unknownCosts += 1 }
        }
        return result
    }
}

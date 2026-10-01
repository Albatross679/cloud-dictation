import SwiftUI
import Charts
import AppKit

@MainActor
enum UsageDashboardWindow {
    private static var controller: NSWindowController?
    static func show() {
        if controller == nil {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 800, height: 820),
                                  styleMask: [.titled, .closable, .resizable, .miniaturizable], backing: .buffered, defer: false)
            window.title = "Dictation usage"
            window.contentMinSize = NSSize(width: 440, height: 500)
            window.contentView = NSHostingView(rootView: UsageDashboard(store: .shared))
            window.isReleasedWhenClosed = false
            window.center()
            controller = NSWindowController(window: window)
        }
        controller?.showWindow(nil)
        controller?.window?.makeKeyAndOrderFront(nil)
    }
}

struct UsageDashboardButton: View {
    var body: some View {
        Button { UsageDashboardWindow.show() } label: {
            Label("Dictation usage", systemImage: "chart.bar.xaxis")
        }
        .buttonStyle(.link)
        .font(.caption)
        .help("Local recording minutes and request costs for every engine")
    }
}

struct UsageDashboard: View {
    let store: UsageMetricsStore
    @State private var archive = UsageArchive()
    @State private var error: String?
    @State private var period: UsagePeriod = .daily
    @State private var provider = "all"
    @State private var range = "30"
    @State private var customStart = Calendar.current.date(byAdding: .day, value: -30, to: Date())!
    @State private var customEnd = Date()
    init(store: UsageMetricsStore, initialProvider: String = "all", initialPeriod: UsagePeriod = .daily, initialRange: String = "30") {
        self.store = store
        _provider = State(initialValue: initialProvider)
        _period = State(initialValue: initialPeriod)
        _range = State(initialValue: initialRange)
    }

    private let providers = [("all", "All providers"), ("parakeet", "Parakeet · local"), ("whisper", "Whisper · local"),
                             ("cloudflare", "Cloudflare"), ("huggingface", "Hugging Face"), ("openrouter", "OpenRouter")]
    private var calendar: Calendar { .current }
    private var bounds: (Date, Date) {
        let end = calendar.date(byAdding: .day, value: 1, to: calendar.startOfDay(for: range == "custom" ? customEnd : Date()))!
        let start: Date
        if range == "custom" { start = calendar.startOfDay(for: customStart) }
        else if range == "all" {
            start = calendar.startOfDay(for: min(archive.dictations.map(\.recordedAt).min() ?? Date(),
                                                archive.requests.map(\.startedAt).min() ?? Date()))
        } else { start = calendar.date(byAdding: .day, value: -(Int(range)! - 1), to: calendar.startOfDay(for: Date()))! }
        return (start, end)
    }
    private var selectedProvider: String? { provider == "all" ? nil : provider }
    private var buckets: [UsageBucket] {
        UsageAggregation.buckets(archive, start: bounds.0, end: bounds.1, period: period, provider: selectedProvider, calendar: calendar)
    }
    private var requests: [UsageRequest] {
        archive.requests.filter { $0.startedAt >= bounds.0 && $0.startedAt < bounds.1 && (selectedProvider == nil || $0.provider == selectedProvider) }
            .sorted { $0.startedAt > $1.startedAt }
    }
    private var controls: some View {
        VStack(alignment: .leading, spacing: 10) {
            ViewThatFits(in: .horizontal) {
                HStack { providerPicker; rangePicker }
                VStack(alignment: .leading) { providerPicker; rangePicker }
            }
            if range == "custom" {
                HStack {
                    DatePicker("From", selection: $customStart, displayedComponents: .date)
                    DatePicker("Through", selection: $customEnd, displayedComponents: .date)
                }
            }
            Picker("Calendar totals", selection: $period) {
                ForEach(UsagePeriod.allCases, id: \.self) { Text($0.rawValue).tag($0) }
            }.pickerStyle(.segmented)
        }
    }
    private var providerPicker: some View {
        Picker("Provider", selection: $provider) {
            ForEach(providers, id: \.0) { Text($0.1).tag($0.0) }
        }.frame(minWidth: 210)
    }
    private var rangePicker: some View {
        Picker("Range", selection: $range) {
            Text("Last 7 days").tag("7")
            Text("Last 30 days").tag("30")
            Text("Last 90 days").tag("90")
            Text("All tracked history").tag("all")
            Text("Custom dates").tag("custom")
        }.frame(minWidth: 210)
    }
    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("Dictation usage").font(.title2.weight(.semibold))
                controls
                if let error { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(.orange) }
                if bounds.0 >= bounds.1 { Text("Choose an end date on or after the start date.").foregroundStyle(.orange) }
                if archive.dictations.isEmpty && archive.requests.isEmpty {
                    ContentUnavailableView("No tracked dictations yet", systemImage: "chart.bar",
                                           description: Text("New dictations from every engine appear here. Existing recordings and Worker history are not imported as cross-provider metrics."))
                }
                GroupBox("Selected range") {
                    VStack(alignment: .leading, spacing: 6) {
                        Text(String(format: "%.1f original minutes · %d dictations", buckets.reduce(0) { $0 + $1.minutes }, buckets.reduce(0) { $0 + $1.dictations }))
                        Text("Reported " + money(buckets.reduce(0) { $0 + $1.actualUSD }) + " · Estimated " + money(buckets.reduce(0) { $0 + $1.estimatedUSD }))
                        Text("\(buckets.reduce(0) { $0 + $1.unknownCosts }) requests with unknown cost · \(buckets.reduce(0) { $0 + $1.requests }) request attempts")
                            .foregroundStyle(.secondary)
                        if buckets.contains(where: { $0.unknownDurations > 0 }) {
                            Text("Some original durations are unavailable; minutes are incomplete.").foregroundStyle(.orange)
                        }
                    }.frame(maxWidth: .infinity, alignment: .leading).padding(4)
                }
                GroupBox("Original recording minutes") {
                    Chart(buckets) { bucket in
                        BarMark(x: .value("Calendar period", bucket.start, unit: period.component, calendar: calendar), y: .value("Minutes", bucket.minutes))
                            .foregroundStyle(Color.accentColor)
                    }
                    .chartYAxisLabel("Minutes")
                    .frame(height: 165).padding(8)
                    .accessibilityLabel("Original recording duration by \(period.rawValue.lowercased()) calendar period")
                }
                GroupBox("Request costs · USD") {
                    VStack(alignment: .leading, spacing: 8) {
                        Chart(buckets) { bucket in
                            BarMark(x: .value("Calendar period", bucket.start, unit: period.component, calendar: calendar), y: .value("USD", bucket.actualUSD), stacking: .unstacked)
                                .foregroundStyle(by: .value("Cost type", "Provider reported"))
                                .position(by: .value("Cost type", "Provider reported"))
                            BarMark(x: .value("Calendar period", bucket.start, unit: period.component, calendar: calendar), y: .value("USD", bucket.estimatedUSD), stacking: .unstacked)
                                .foregroundStyle(by: .value("Cost type", "Estimated list rate"))
                                .position(by: .value("Cost type", "Estimated list rate"))
                        }
                        .chartForegroundStyleScale(["Provider reported": Color.accentColor, "Estimated list rate": Color.orange])
                        .chartYAxisLabel("USD").frame(height: 165)
                        Text("Unknown costs are excluded from dollar bars, not treated as free. Estimates are gross list-rate values before account credits or free allowances.")
                            .font(.caption).foregroundStyle(.secondary)
                    }.padding(8)
                }
                calendarTotals
                DisclosureGroup("\(period.rawValue) totals for selected range") {
                    VStack(spacing: 8) {
                        HStack { Text("Period / minutes"); Spacer(); Text("Reported / estimated / unknown") }.font(.caption).foregroundStyle(.secondary)
                        ForEach(buckets) { bucket in
                            HStack {
                                VStack(alignment: .leading) {
                                    Text(bucket.start, format: .dateTime.year().month().day())
                                    Text(String(format: "%.1f min", bucket.minutes)).foregroundStyle(.secondary)
                                }
                                Spacer()
                                VStack(alignment: .trailing) {
                                    Text(money(bucket.actualUSD) + " / " + money(bucket.estimatedUSD))
                                    Text("\(bucket.unknownCosts) unknown").foregroundStyle(.secondary)
                                }
                            }.font(.caption)
                            Divider()
                        }
                    }.padding(.top, 8)
                }
                DisclosureGroup("Request attempts, cleanup and retries") {
                    VStack(alignment: .leading, spacing: 10) {
                        if requests.isEmpty { Text("No cloud request attempts in this range. Local inference has no cloud API charge.").foregroundStyle(.secondary) }
                        ForEach(requests.prefix(200)) { request in
                            VStack(alignment: .leading, spacing: 3) {
                                Text(request.startedAt, format: .dateTime.year().month().day().hour().minute().second())
                                Text("\(request.provider) · \(request.phase) · \(outcomeLabel(request.outcome))")
                                Text(request.model).foregroundStyle(.secondary).textSelection(.enabled)
                                Text(costDescription(request.cost)).help(request.cost.source ?? "Provider did not supply a usable charge; no verified estimate is available.")
                                if let source = request.cost.source { Text(source).font(.caption2).foregroundStyle(.secondary).textSelection(.enabled) }
                                let runs = archive.runs.filter { $0.dictationID == request.dictationID }.sorted { $0.startedAt < $1.startedAt }
                                if let index = runs.firstIndex(where: { $0.id == request.runID }) { Text("Dictation run \(index + 1)").foregroundStyle(.secondary) }
                            }.font(.caption)
                            Divider()
                        }
                        if requests.count > 200 { Text("Showing the latest 200 attempts. Charts and totals include all attempts.").font(.caption) }
                    }.padding(.top, 8)
                }
                DisclosureGroup("Dictations and outcomes") {
                    let dictations = archive.dictations.filter { $0.recordedAt >= bounds.0 && $0.recordedAt < bounds.1 && (selectedProvider == nil || $0.provider == selectedProvider) }
                        .sorted { $0.recordedAt > $1.recordedAt }
                    VStack(alignment: .leading, spacing: 10) {
                        ForEach(dictations.prefix(200)) { dictation in
                            let runs = archive.runs.filter { $0.dictationID == dictation.id }
                            VStack(alignment: .leading, spacing: 3) {
                                Text(dictation.recordedAt, format: .dateTime.year().month().day().hour().minute())
                                Text("\(dictation.provider) · \(dictation.model)")
                                Text(dictation.originalSeconds.map { String(format: "%.1f original seconds", $0) } ?? "Original duration unavailable")
                                Text("\(runs.count) runs · \(outcomeLabel(runs.last?.outcome ?? "unknown"))").foregroundStyle(.secondary)
                                if dictation.provider == "parakeet" || dictation.provider == "whisper" { Text("Local inference: no cloud API charge").foregroundStyle(.secondary) }
                            }.font(.caption)
                            Divider()
                        }
                        if dictations.count > 200 { Text("Showing the latest 200 dictations. Totals include all dictations.").font(.caption) }
                    }.padding(.top, 8)
                }
                Text("Tracking started \(archive.trackingStartedAt.formatted(date: .abbreviated, time: .shortened)). Calendar timezone: \(calendar.timeZone.identifier). Minutes use the original recording and its first tracked engine, once per dictation, including failures. Costs use each request's provider and send date, including cleanup and retries. Local inference has no cloud API charge. Worker pipelines may hide internal execution when a response is missing.")
                    .font(.caption).foregroundStyle(.secondary)
                Text("Metrics stay on this Mac. No transcript text, recording bytes or credentials are stored in the usage file. Existing history and settings are unchanged.")
                    .font(.caption).foregroundStyle(.secondary)
            }.padding(20).frame(maxWidth: .infinity, alignment: .leading)
        }
        .background(Color(nsColor: .windowBackgroundColor))
        .onAppear(perform: refresh)
        .onReceive(NotificationCenter.default.publisher(for: UsageMetricsStore.changed)) { _ in refresh() }
    }
    private var calendarTotals: some View {
        GroupBox("Current calendar totals") {
            VStack(alignment: .leading, spacing: 8) {
                ForEach(UsagePeriod.allCases, id: \.self) { item in
                    let interval = calendar.dateInterval(of: item.component, for: Date())!
                    let values = UsageAggregation.buckets(archive, start: interval.start, end: interval.end, period: item,
                                                         provider: selectedProvider, calendar: calendar)
                    let value = values.first ?? UsageBucket(start: interval.start)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item == .daily ? "Today" : item == .weekly ? "This week" : "This month").font(.caption.weight(.semibold))
                        Text(String(format: "%.1f min", value.minutes) + " · " + money(value.actualUSD) + " reported · " + money(value.estimatedUSD) + " estimated · \(value.unknownCosts) unknown")
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }.frame(maxWidth: .infinity, alignment: .leading).padding(4)
        }
    }
    private func refresh() { let snapshot = store.snapshot(); archive = snapshot.archive; error = snapshot.error }
    private func outcomeLabel(_ outcome: String) -> String {
        outcome == "inFlight" ? "in flight or interrupted" : outcome
    }
    private func money(_ value: Double) -> String { String(format: "$%.5f", value) }
    private func costDescription(_ cost: UsageCost) -> String {
        switch cost.kind {
        case .actual: return money(cost.usd ?? 0) + " provider reported"
        case .estimate: return money(cost.usd ?? 0) + " estimated · $\(cost.usdPerAudioMinute ?? 0)/uploaded min · rate \(cost.rateDate ?? "unknown date")"
        case .unknown: return "Cost unknown, not zero"
        }
    }
}

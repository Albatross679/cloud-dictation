// Appended to the exact compressor declarations from CloudflareEngine.swift
// by test_client.sh, so this tests private production code, not a reimplementation.
@main enum AudioCompressorTests {
    static func main() throws {
        let folder = FileManager.default.temporaryDirectory.appendingPathComponent("audio-test-\(UUID())")
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: folder) }
        for (sampleRate, channels, frames) in [(16_000.0, 1, 192_973), (44_100.0, 2, 132_301), (48_000.0, 1, 12_000)] {
            let source = folder.appendingPathComponent("\(Int(sampleRate)).wav")
            let format = AVAudioFormat(standardFormatWithSampleRate: sampleRate, channels: AVAudioChannelCount(channels))!
            let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))!
            buffer.frameLength = AVAudioFrameCount(frames)
            for channel in 0..<channels {
                for frame in 0..<frames {
                    buffer.floatChannelData![channel][frame] = Float(sin(Double(frame) * 2 * .pi * 440 / sampleRate) * 0.2)
                }
            }
            do {
                let file = try AVAudioFile(forWriting: source, settings: format.settings)
                try file.write(from: buffer)
            }
            let original = try Data(contentsOf: source)
            let unchanged = try CloudflareAudioCompressor.compressForUpload(source: source, rate: 1)
            precondition(unchanged == source, "1x must return the original file")
            for rate in [1.25, 1.5, 1.75, 2, 2.25, 2.5, 2.75, 3] {
                let output = try CloudflareAudioCompressor.compressForUpload(source: source, rate: rate)
                defer { try? FileManager.default.removeItem(at: output) }
                let file = try AVAudioFile(forReading: output)
                let expected = Int64((Double(frames) / rate).rounded(.up))
                precondition(file.length == expected, "compression must drain the time-pitch tail")
                precondition(file.fileFormat.sampleRate == sampleRate)
                precondition(file.fileFormat.channelCount == channels)
                let read = AVAudioPCMBuffer(pcmFormat: file.processingFormat, frameCapacity: AVAudioFrameCount(file.length))!
                try file.read(into: read)
                let samples = UnsafeBufferPointer(start: read.floatChannelData![0], count: Int(read.frameLength))
                precondition(samples.contains { abs($0) > 0.05 }, "output must contain audio, not just padding")
                let after = try Data(contentsOf: source)
                precondition(after == original, "recorder file must not change")
                print("  ok   \(Int(sampleRate)) Hz \(channels) channel \(rate)x: \(expected) frames")
            }
        }
        print("all audio speed checks passed")
    }
}

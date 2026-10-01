import Foundation
import Darwin

/// User-approved plaintext storage, outside the app bundle. No Keychain reads
/// occur after the first missing-file migration. Tests supply their own path
/// and migration closure, so they never access real credentials.
final class LocalCredentialStore {
    struct Migration {
        var credentials: [String: String] = [:]
        var importNeeded: [String] = []
    }
    private struct Document: Codable {
        let version: Int
        var credentials: [String: String]
        var importNeeded: [String]
    }
    enum StoreError: LocalizedError {
        case readFailed, invalidFile, writeFailed, unsafePath
        var errorDescription: String? {
            switch self {
            case .readFailed: return "Could not read the local API key settings file. Check its permissions before retrying."
            case .invalidFile: return "The local API key settings file is invalid. It was not overwritten. Repair it or import your keys into a valid file."
            case .writeFailed: return "Could not save the local API key settings file. Your key changes were not saved. Check Application Support permissions."
            case .unsafePath: return "The local API key settings path is a symbolic link. Choose a regular private settings file."
            }
        }
    }

    let fileURL: URL
    private let migrate: () -> Migration
    private let lock = NSLock()
    private var document: Document?
    private var attemptedLoad = false
    private var failure: StoreError?

    init(fileURL: URL, migrate: @escaping () -> Migration) {
        self.fileURL = fileURL
        self.migrate = migrate
    }

    var error: StoreError? {
        lock.lock(); defer { lock.unlock() }
        loadIfNeeded()
        return failure
    }

    func value(for account: String) -> String {
        lock.lock(); defer { lock.unlock() }
        loadIfNeeded()
        return document?.credentials[account] ?? ""
    }

    func needsImport(_ account: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        loadIfNeeded()
        return document?.importNeeded.contains(account) ?? false
    }

    @discardableResult
    func set(_ value: String, for account: String) -> Bool {
        lock.lock(); defer { lock.unlock() }
        loadIfNeeded()
        guard var next = document else { return false }
        if value.isEmpty { next.credentials.removeValue(forKey: account) }
        else { next.credentials[account] = value }
        next.importNeeded.removeAll { $0 == account }
        return persist(next)
    }

    /// Check and fill missing keys under the same lock. Explicit environment
    /// import cannot race with a pasted key and overwrite that new selection.
    @discardableResult
    func importMissing(_ values: [String: String]) -> Bool {
        lock.lock(); defer { lock.unlock() }
        loadIfNeeded()
        guard var next = document else { return false }
        var changed = false
        for (account, candidate) in values {
            let value = candidate.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !value.isEmpty, (next.credentials[account] ?? "").isEmpty else { continue }
            next.credentials[account] = value
            next.importNeeded.removeAll { $0 == account }
            changed = true
        }
        return changed ? persist(next) : failure == nil
    }

    private func persist(_ next: Document) -> Bool {
        do {
            try save(next)
            document = next
            failure = nil
            return true
        } catch let error as StoreError {
            failure = error
            return false
        } catch {
            failure = .writeFailed
            return false
        }
    }

    /// Explicit retry after the user repairs a file or its permissions. Normal
    /// getters never loop on failures or fall back to the Keychain.
    func reload() {
        lock.lock(); defer { lock.unlock() }
        attemptedLoad = false
        document = nil
        failure = nil
        loadIfNeeded()
    }

    private func loadIfNeeded() {
        guard !attemptedLoad else { return }
        attemptedLoad = true
        do {
            try rejectSymlinks()
            if FileManager.default.fileExists(atPath: fileURL.path) {
                try secureDirectory()
                try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: fileURL.path)
                let data: Data
                do { data = try Data(contentsOf: fileURL) }
                catch { throw StoreError.readFailed }
                guard let loaded = try? JSONDecoder().decode(Document.self, from: data), loaded.version == 1 else {
                    throw StoreError.invalidFile
                }
                document = loaded
            } else {
                let imported = migrate()
                let next = Document(version: 1, credentials: imported.credentials, importNeeded: imported.importNeeded)
                try save(next)
                document = next
            }
        } catch let error as StoreError {
            failure = error
        } catch {
            failure = .readFailed
        }
    }

    private func rejectSymlinks() throws {
        for url in [fileURL.deletingLastPathComponent(), fileURL] {
            if (try? url.resourceValues(forKeys: [.isSymbolicLinkKey]).isSymbolicLink) == true {
                throw StoreError.unsafePath
            }
        }
    }

    private func secureDirectory() throws {
        let parent = fileURL.deletingLastPathComponent()
        try FileManager.default.createDirectory(at: parent, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: parent.path)
    }

    private func save(_ next: Document) throws {
        try rejectSymlinks()
        do {
            try secureDirectory()
            let encoder = JSONEncoder()
            encoder.outputFormatting = [.sortedKeys]
            let bytes = try encoder.encode(next)
            let temporary = fileURL.deletingLastPathComponent().appendingPathComponent(".credentials-\(UUID().uuidString).tmp")
            defer { try? FileManager.default.removeItem(at: temporary) }
            guard FileManager.default.createFile(atPath: temporary.path, contents: bytes, attributes: [.posixPermissions: 0o600]) else {
                throw StoreError.writeFailed
            }
            // Same-directory POSIX rename atomically replaces only our settings
            // file. The temporary and final file both have mode 0600.
            guard rename(temporary.path, fileURL.path) == 0 else { throw StoreError.writeFailed }
        } catch let error as StoreError { throw error }
        catch { throw StoreError.writeFailed }
    }
}

import Foundation
import Security

/// User-approved local plaintext settings. Existing Keychain accounts are read
/// once, without UI, only when creating a missing local file. They are never
/// modified or deleted. Regular startup/settings/dictation use the file cache.
enum AuthTokenStore {
    private static let service = "local.clouddictation.OpenSuperWhisper"
    private static let workerAccount = "cloudflareAuthToken"
    private static let directAPIAccount = "cloudflareDirectAPIToken"
    static let fileURL = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        .appendingPathComponent("OSW Cloud", isDirectory: true)
        .appendingPathComponent("credentials.json")
    private static let store = LocalCredentialStore(fileURL: fileURL, migrate: migrateLegacy)

    static var token: String {
        get { store.value(for: workerAccount) }
        set { store.set(newValue, for: workerAccount) }
    }
    static var directAPIToken: String {
        get { store.value(for: directAPIAccount) }
        set { store.set(newValue, for: directAPIAccount) }
    }
    static func key(for provider: CloudProvider) -> String {
        store.value(for: provider.keychainAccount)
    }
    static func setKey(_ value: String, for provider: CloudProvider) {
        store.set(value, for: provider.keychainAccount)
    }

    static var persistenceError: String? { store.error?.localizedDescription }

    static func importMessage(for provider: CloudProvider, connectionMode: String) -> String? {
        let account = provider == .cloudflare && connectionMode != "direct" ? workerAccount : provider.keychainAccount
        guard store.needsImport(account), store.value(for: account).isEmpty else { return nil }
        return "The previous \(provider.label) key could not be imported without a Keychain prompt. Paste it here or import it from your environment. Existing Keychain entries were left untouched."
    }

    static func validatePersistence() throws {
        if let error = store.error { throw error }
    }

    static func validateStorage(for provider: CloudProvider, connectionMode: String) throws {
        try validatePersistence()
        if let message = importMessage(for: provider, connectionMode: connectionMode) {
            throw ImportError(message: message)
        }
    }

    /// Explicit Settings action only. Never silently override an existing key
    /// or use an environment variable as an implicit transcription fallback.
    static func importEnvironment() {
        let environment = ProcessInfo.processInfo.environment
        var candidates: [String: String] = [:]
        for (account, name) in [
            (workerAccount, "CLOUD_DICTATION_WORKER_TOKEN"),
            (directAPIAccount, "CLOUD_DICTATION_DIRECT_API_TOKEN"),
            (CloudProvider.huggingface.keychainAccount, "HF_TOKEN"),
            (CloudProvider.openrouter.keychainAccount, "OPENROUTER_API_KEY"),
        ] {
            if let value = environment[name] { candidates[account] = value }
        }
        store.importMissing(candidates)
    }

    static func reload() { store.reload() }

    private struct ImportError: LocalizedError {
        let message: String
        var errorDescription: String? { message }
    }

    /// Pure query contract for the one-time migration, testable without calling
    /// Security or reading a real credential. Approval-required reads fail.
    static func legacyMigrationQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
            kSecUseAuthenticationUI as String: kSecUseAuthenticationUIFail,
        ]
    }

    private static func migrateLegacy() -> LocalCredentialStore.Migration {
        var result = LocalCredentialStore.Migration()
        // A pre-Keychain Worker token already present in defaults is supported
        // once. Leave defaults and all existing Keychain entries untouched.
        if let old = UserDefaults.standard.string(forKey: workerAccount), !old.isEmpty {
            result.credentials[workerAccount] = old
        }
        for account in [workerAccount, directAPIAccount, CloudProvider.huggingface.keychainAccount, CloudProvider.openrouter.keychainAccount] {
            guard result.credentials[account] == nil else { continue }
            let query = legacyMigrationQuery(account: account)
            var item: CFTypeRef?
            let status = SecItemCopyMatching(query as CFDictionary, &item)
            if status == errSecSuccess, let data = item as? Data,
               let value = String(data: data, encoding: .utf8), !value.isEmpty {
                result.credentials[account] = value
            } else if status != errSecItemNotFound {
                result.importNeeded.append(account)
            }
        }
        return result
    }
}

import Foundation
import Security

/// Keychain storage for the worker bearer token.
///
/// UserDefaults writes a plain plist under ~/Library/Preferences, so the token
/// was readable with `defaults read` by anything running as the user. The
/// Keychain item is created with `kSecAttrAccessibleAfterFirstUnlock` so a
/// launch-at-login app can still reach it without the login keychain being
/// unlocked interactively.
enum AuthTokenStore {
    private static let service = "local.clouddictation.OpenSuperWhisper"
    private static let workerAccount = "cloudflareAuthToken"
    private static let directAPIAccount = "cloudflareDirectAPIToken"
    private static let legacyDefaultsKey = "cloudflareAuthToken"

    /// The token used by the self-hosted Worker connection mode.
    static var token: String {
        get {
            if let stored = read(account: workerAccount) { return stored }
            // One-time move of a token written before Keychain storage existed.
            if let legacy = UserDefaults.standard.string(forKey: legacyDefaultsKey), !legacy.isEmpty {
                write(legacy, account: workerAccount)
                UserDefaults.standard.removeObject(forKey: legacyDefaultsKey)
                return legacy
            }
            return ""
        }
        set { write(newValue, account: workerAccount) }
    }

    /// Kept separately so switching connection modes never overwrites a
    /// working Worker secret with the user's Cloudflare API token.
    static var directAPIToken: String {
        get { read(account: directAPIAccount) ?? "" }
        set { write(newValue, account: directAPIAccount) }
    }

    private static func baseQuery(account: String) -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    private static func read(account: String) -> String? {
        var query = baseQuery(account: account)
        query[kSecReturnData as String] = true
        query[kSecMatchLimit as String] = kSecMatchLimitOne

        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess,
              let data = item as? Data,
              let value = String(data: data, encoding: .utf8),
              !value.isEmpty
        else { return nil }
        return value
    }

    private static func write(_ value: String, account: String) {
        let query = baseQuery(account: account)
        guard !value.isEmpty else {
            SecItemDelete(query as CFDictionary)
            return
        }

        let data = Data(value.utf8)
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]

        if SecItemUpdate(query as CFDictionary, attributes as CFDictionary) == errSecItemNotFound {
            var insert = query
            insert.merge(attributes) { current, _ in current }
            SecItemAdd(insert as CFDictionary, nil)
        }
    }
}

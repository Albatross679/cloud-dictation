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
    private static let account = "cloudflareAuthToken"
    private static let legacyDefaultsKey = "cloudflareAuthToken"

    static var token: String {
        get {
            if let stored = read() { return stored }
            // One-time move of a token written before Keychain storage existed.
            if let legacy = UserDefaults.standard.string(forKey: legacyDefaultsKey), !legacy.isEmpty {
                write(legacy)
                UserDefaults.standard.removeObject(forKey: legacyDefaultsKey)
                return legacy
            }
            return ""
        }
        set { write(newValue) }
    }

    private static func baseQuery() -> [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
    }

    private static func read() -> String? {
        var query = baseQuery()
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

    private static func write(_ value: String) {
        let query = baseQuery()
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

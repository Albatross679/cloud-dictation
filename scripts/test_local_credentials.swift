import Foundation
import Security

@main enum LocalCredentialTests {
    static func check(_ name: String, _ ok: Bool) {
        print("  \(ok ? "ok  " : "FAIL") \(name)")
        if !ok { exit(1) }
    }
    static func mode(_ path: URL) throws -> Int {
        let attributes = try FileManager.default.attributesOfItem(atPath: path.path)
        return (attributes[.posixPermissions] as? NSNumber)?.intValue ?? 0
    }
    static func main() throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("credentials-test-\(UUID())")
        defer { try? FileManager.default.removeItem(at: root) }
        let file = root.appendingPathComponent("settings/credentials.json")
        var migrations = 0
        let store = LocalCredentialStore(fileURL: file) {
            migrations += 1
            return .init(credentials: ["cloudflareAuthToken": "fake-worker", "cloudflareDirectAPIToken": "fake-direct", "huggingFaceAPIToken": "fake-hf"], importNeeded: ["openRouterAPIToken"])
        }
        for _ in 0..<8 { check("cached migrated key", store.value(for: "cloudflareAuthToken") == "fake-worker") }
        check("migration runs once", migrations == 1)
        check("file is private0600", try mode(file) & 0o777 == 0o600)
        check("parent is private0700", try mode(file.deletingLastPathComponent()) & 0o777 == 0o700)
        check("blocked legacy import stays explicit", store.needsImport("openRouterAPIToken"))
        check("paste saves missing provider", store.set("fake-or", for: "openRouterAPIToken"))
        check("paste clears import notice", !store.needsImport("openRouterAPIToken"))
        check("explicit import succeeds", store.importMissing(["openRouterAPIToken": "must-not-overwrite", "environment-only": " fake-imported ", "blank": " "]))
        check("environment import preserves existing key", store.value(for: "openRouterAPIToken") == "fake-or")
        check("environment fills only nonempty missing keys", store.value(for: "environment-only") == "fake-imported" && store.value(for: "blank").isEmpty)
        let existing = LocalCredentialStore(fileURL: file) { fatalError("existing local file must not query legacy storage") }
        check("existing file reads provider key", existing.value(for: "openRouterAPIToken") == "fake-or")
        check("other provider keys preserved", existing.value(for: "huggingFaceAPIToken") == "fake-hf" && existing.value(for: "cloudflareDirectAPIToken") == "fake-direct")
        DispatchQueue.concurrentPerform(iterations: 12) { index in
            precondition(store.set("fake-\(index)", for: "test-\(index)"))
        }
        let concurrent = LocalCredentialStore(fileURL: file) { fatalError("no remigration") }
        check("concurrent atomic writes preserve every key", (0..<12).allSatisfy { concurrent.value(for: "test-\($0)") == "fake-\($0)" })
        check("explicit clear affects only local selected key", store.set("", for: "openRouterAPIToken") && store.value(for: "huggingFaceAPIToken") == "fake-hf")
        let query = AuthTokenStore.legacyMigrationQuery(account: "test-only-account")
        check("legacy query never prompts", query[kSecUseAuthenticationUI as String] as? String == kSecUseAuthenticationUIFail as String)
        check("legacy query reads one exact account", query[kSecAttrAccount as String] as? String == "test-only-account")
        check("readable current key beats stale defaults", AuthTokenStore.legacyCredential(status: errSecSuccess, data: Data("current-fake".utf8), defaults: "stale-fake") == "current-fake")
        check("missing Worker item imports old defaults once", AuthTokenStore.legacyCredential(status: errSecItemNotFound, data: nil, defaults: "old-fake") == "old-fake")
        check("denied item is not overridden by stale defaults", AuthTokenStore.legacyCredential(status: errSecInteractionNotAllowed, data: nil, defaults: "stale-fake") == nil)

        let corrupt = root.appendingPathComponent("invalid/credentials.json")
        try FileManager.default.createDirectory(at: corrupt.deletingLastPathComponent(), withIntermediateDirectories: true)
        let bytes = Data("not valid settings".utf8)
        try bytes.write(to: corrupt)
        let invalid = LocalCredentialStore(fileURL: corrupt) { fatalError("invalid local file must not fall back to legacy storage") }
        check("corrupt file exposes an error", invalid.error != nil)
        check("corrupt file rejects writes", !invalid.set("fake", for: "test"))
        check("corrupt data not overwritten", try Data(contentsOf: corrupt) == bytes)

        let backup = root.appendingPathComponent("backup.json")
        try FileManager.default.moveItem(at: file, to: backup)
        try FileManager.default.createDirectory(at: file, withIntermediateDirectories: false)
        check("persistence failure is visible", !store.set("not-saved", for: "huggingFaceAPIToken") && store.error != nil)
        check("failed persistence does not replace cache", store.value(for: "huggingFaceAPIToken") == "fake-hf")
        let leftovers = try FileManager.default.contentsOfDirectory(atPath: file.deletingLastPathComponent().path)
        check("atomic staging files removed", !leftovers.contains { $0.hasPrefix(".credentials-") })
        print("all local credential checks passed; no real secrets or Keychain operations")
    }
}

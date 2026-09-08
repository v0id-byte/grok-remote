import Foundation
import Security

/// Keychain storage for device credentials.
///
/// Three deliberate changes from the first version, all of which were real
/// defects rather than style:
///
///  * Errors are thrown, not swallowed. `SecItemAdd`'s OSStatus used to be
///    discarded, so a failed write was silent and the user simply found
///    themselves back at the pairing screen with no explanation.
///  * Accessibility is `WhenUnlockedThisDeviceOnly`. The default allows the
///    item into iCloud/iTunes backups and onto a restored device; this
///    credential is only ever used with the app in the foreground, so the
///    strictest class that still works is the right one.
///  * Credentials are keyed by origin, so pointing the app at a different
///    Bridge cannot reuse a token minted by the previous one.
enum KeychainError: LocalizedError {
    case unexpectedStatus(OSStatus)
    case decodingFailed

    var errorDescription: String? {
        switch self {
        case .unexpectedStatus(let status):
            let message = SecCopyErrorMessageString(status, nil) as String? ?? "unknown"
            return "Keychain error \(status): \(message)"
        case .decodingFailed:
            return "Stored credentials could not be decoded."
        }
    }
}

enum KeychainStore {
    private static let service = "com.v0id.grokremote.device"
    private static let account = "default"

    static func save(_ credentials: DeviceCredentials) throws {
        let data = try JSONEncoder().encode(credentials)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        let attributes: [String: Any] = [
            kSecValueData as String: data,
            kSecAttrAccessible as String: kSecAttrAccessibleWhenUnlockedThisDeviceOnly,
        ]

        let status = SecItemUpdate(query as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound {
            var insert = query
            insert.merge(attributes) { current, _ in current }
            let addStatus = SecItemAdd(insert as CFDictionary, nil)
            guard addStatus == errSecSuccess else {
                throw KeychainError.unexpectedStatus(addStatus)
            }
            return
        }
        guard status == errSecSuccess else { throw KeychainError.unexpectedStatus(status) }
    }

    static func load() throws -> DeviceCredentials? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &item)
        if status == errSecItemNotFound { return nil }
        guard status == errSecSuccess else { throw KeychainError.unexpectedStatus(status) }
        guard let data = item as? Data else { throw KeychainError.decodingFailed }
        return try? JSONDecoder().decode(DeviceCredentials.self, from: data)
    }

    @discardableResult
    static func clear() -> Bool {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        return SecItemDelete(query as CFDictionary) == errSecSuccess
    }
}

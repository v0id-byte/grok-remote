import Foundation
import Observation
import SwiftUI

@Observable
final class AppState {
    let client = BridgeClient()

    private(set) var credentials: DeviceCredentials?
    var isPaired: Bool { credentials != nil }

    /// Surfaced so the pairing screen can explain a failed Keychain write
    /// instead of silently returning the user to it on next launch.
    private(set) var keychainProblem: String?

    /// Remembered separately from the Keychain so the pairing screen can
    /// pre-fill the last address even if the credential itself is gone.
    @ObservationIgnored
    @AppStorage("lastServerURL") var lastServerURL: String = ""

    @ObservationIgnored
    @AppStorage("defaultModel") var defaultModel: String = ""

    @ObservationIgnored
    @AppStorage("defaultEffort") var defaultEffort: String = ""

    init() {
        do {
            if let stored = try KeychainStore.load() {
                credentials = stored
                client.credentials = stored
                lastServerURL = stored.baseURL.absoluteString
            }
        } catch {
            keychainProblem = error.localizedDescription
        }
    }

    func completePairing(_ creds: DeviceCredentials) {
        do {
            try KeychainStore.save(creds)
            keychainProblem = nil
        } catch {
            // Pair anyway so this session works, but say plainly that it will
            // not survive a relaunch -- the old code failed silently here.
            keychainProblem = error.localizedDescription
        }
        credentials = creds
        client.credentials = creds
        lastServerURL = creds.baseURL.absoluteString
    }

    func unpair() {
        KeychainStore.clear()
        credentials = nil
        client.credentials = nil
    }

    /// Changing the server means the current token is no longer valid for it.
    /// The credential is bound to its origin, so it is discarded rather than
    /// sent somewhere it was never issued for.
    func changeServer(to url: URL) {
        lastServerURL = url.absoluteString
        if let creds = credentials, !creds.matches(origin: url) {
            unpair()
        }
    }
}

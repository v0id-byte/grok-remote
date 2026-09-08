import Foundation
import Observation

@Observable
final class AppState {
    var client = BridgeClient()
    var isPaired = false

    init() {
        if let creds = KeychainStore.load() {
            client.credentials = creds
            isPaired = true
        }
    }

    func completePairing(_ credentials: DeviceCredentials) {
        KeychainStore.save(credentials)
        client.credentials = credentials
        isPaired = true
    }

    func unpair() {
        KeychainStore.clear()
        client.credentials = nil
        isPaired = false
    }
}

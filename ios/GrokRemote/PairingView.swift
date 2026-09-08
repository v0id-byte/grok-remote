import SwiftUI

/// One-time pairing (plan v1.3 §4). Manual entry for now -- QR camera
/// scanning is a Phase 4 addition once install.sh actually prints a real
/// QR code to scan; the payload shape (url + pairing_token) is the same
/// either way, so this form exercises the exact same /v1/pair flow.
struct PairingView: View {
    @Environment(AppState.self) private var appState
    @State private var urlText = "http://localhost:8899"
    @State private var pairingToken = ""
    @State private var isPairing = false
    @State private var errorMessage: String?

    var body: some View {
        NavigationStack {
            Form {
                Section("Bridge 地址") {
                    TextField("https://grok-remote.void1211.com", text: $urlText)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                        .keyboardType(.URL)
                }
                Section("配对码") {
                    TextField("pairing_token", text: $pairingToken)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                if let errorMessage {
                    Text(errorMessage).foregroundStyle(.red)
                }
                Button {
                    Task { await pair() }
                } label: {
                    if isPairing {
                        ProgressView()
                    } else {
                        Text("配对")
                    }
                }
                .disabled(urlText.isEmpty || pairingToken.isEmpty || isPairing)
            }
            .navigationTitle("Grok Remote")
        }
    }

    private func pair() async {
        guard let url = URL(string: urlText) else {
            errorMessage = "地址无效"
            return
        }
        isPairing = true
        errorMessage = nil
        do {
            let creds = try await appState.client.pair(baseURL: url, pairingToken: pairingToken)
            appState.completePairing(creds)
        } catch {
            errorMessage = "配对失败: \(error.localizedDescription)"
        }
        isPairing = false
    }
}

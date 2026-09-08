import SwiftUI

struct PairingView: View {
    @Environment(AppState.self) private var appState

    @State private var urlText = ""
    @State private var pairingToken = ""
    @State private var isPairing = false
    @State private var errorMessage: String?
    @State private var showScanner = false

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.Space.l) {
                VStack(alignment: .leading, spacing: DS.Space.s) {
                    Text("Grok Remote")
                        .font(DS.Font.display)
                        .dsDisplayTracking()
                        .foregroundStyle(DS.Color.text)
                    Text("Drive the agent on your Mac from here.")
                        .font(DS.Font.callout)
                        .foregroundStyle(DS.Color.textSecondary)
                }
                .dsReveal(0)

                VStack(alignment: .leading, spacing: DS.Space.s) {
                    DSSectionLabel(text: "Bridge address")
                    DSTextField(placeholder: "https://grok-remote.example.com",
                                text: $urlText, systemImage: DS.Icon.device)
                        .keyboardType(.URL)
                }
                .dsReveal(1)

                VStack(alignment: .leading, spacing: DS.Space.s) {
                    DSSectionLabel(text: "Pairing code")
                    DSTextField(placeholder: "one-time code", text: $pairingToken,
                                systemImage: DS.Icon.command)
                    Text("Run install.sh on the Mac to print a code. It expires after "
                         + "10 minutes.")
                        .font(DS.Font.footnote)
                        .foregroundStyle(DS.Color.textTertiary)
                }
                .dsReveal(2)

                if let keychainProblem = appState.keychainProblem {
                    Text(keychainProblem)
                        .font(DS.Font.footnote)
                        .foregroundStyle(DS.Color.danger)
                        .dsCard(.danger)
                }

                if let errorMessage {
                    Text(errorMessage)
                        .font(DS.Font.footnote)
                        .foregroundStyle(DS.Color.danger)
                        .dsCard(.danger)
                        .dsReveal(3)
                }

                VStack(spacing: DS.Space.s) {
                    DSPrimaryButton(title: isPairing ? "Pairing…" : "Pair",
                                    isEnabled: canPair) {
                        Task { await pair() }
                    }
                    Button {
                        showScanner = true
                    } label: {
                        Label("Scan QR code", systemImage: DS.Icon.qr)
                    }
                    .buttonStyle(DSButtonStyle(kind: .secondary))
                }
                .dsReveal(4)
            }
            .padding(DS.Space.l)
        }
        .background(DS.Color.bg)
        .onAppear {
            if urlText.isEmpty { urlText = appState.lastServerURL }
        }
        .sheet(isPresented: $showScanner) {
            scannerSheet
        }
    }

    private var scannerSheet: some View {
        ZStack(alignment: .top) {
            QRScannerView(
                onScan: { payload in
                    apply(payload: payload)
                    showScanner = false
                },
                onError: { message in
                    errorMessage = message
                    showScanner = false
                })
            .ignoresSafeArea()

            HStack {
                Spacer()
                Button("Cancel") { showScanner = false }
                    .font(DS.Font.headline)
                    .foregroundStyle(.white)
                    .padding(DS.Space.m)
            }
        }
    }

    private var canPair: Bool {
        !urlText.isEmpty && !pairingToken.isEmpty && !isPairing
    }

    /// install.sh prints `{"url": "...", "pairing_token": "..."}`.
    private func apply(payload: String) {
        guard let data = payload.data(using: .utf8),
              let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let url = object["url"] as? String,
              let token = object["pairing_token"] as? String else {
            errorMessage = "That QR code is not a Grok Remote pairing code."
            return
        }
        // Never accept an arbitrary address from a scanned code: a QR is
        // untrusted input, and this one decides where the device token goes.
        guard let parsed = URL(string: url), let scheme = parsed.scheme,
              scheme == "https" || isLocal(parsed) else {
            errorMessage = "Pairing codes must point at an https address."
            return
        }
        urlText = url
        pairingToken = token
        errorMessage = nil
    }

    private func isLocal(_ url: URL) -> Bool {
        guard let host = url.host else { return false }
        return host == "localhost" || host.hasSuffix(".local")
            || host.hasPrefix("127.") || host.hasPrefix("192.168.")
    }

    private func pair() async {
        guard var text = urlText.trimmingCharacters(in: .whitespaces) as String?,
              !text.isEmpty else { return }
        if !text.contains("://") { text = "https://\(text)" }
        guard let url = URL(string: text), url.host != nil else {
            errorMessage = "That does not look like a valid address."
            return
        }

        isPairing = true
        errorMessage = nil
        defer { isPairing = false }
        do {
            let creds = try await appState.client.pair(
                baseURL: url,
                pairingToken: pairingToken.trimmingCharacters(in: .whitespaces))
            appState.completePairing(creds)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

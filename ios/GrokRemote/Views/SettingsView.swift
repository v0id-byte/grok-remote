import SwiftUI

/// Server address, credentials and defaults.
///
/// None of this was reachable before: the app had no settings screen at all,
/// `unpair()` was dead code, and changing servers meant deleting the app.
struct SettingsView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss

    @State private var serverText = ""
    @State private var health: BridgeClient.Health?
    @State private var healthError: String?
    @State private var isChecking = false
    @State private var showTokenAlert = false
    @State private var confirmUnpair = false
    @State private var models: [ModelInfo] = []

    var body: some View {
        DSScreen(title: "Settings",
                 leading: AnyView(DSBackButton { dismiss() })) {
            ScrollView {
                VStack(alignment: .leading, spacing: DS.Space.l) {
                    connection.dsReveal(0)
                    device.dsReveal(1)
                    defaults.dsReveal(2)
                    diagnostics.dsReveal(3)
                }
                .padding(DS.Space.l)
            }
        }
        .background(DS.Color.bg)
        .task {
            serverText = appState.credentials?.baseURL.absoluteString ?? appState.lastServerURL
            await refresh()
            models = (try? await appState.client.models().models) ?? []
        }
        .alert("Device token", isPresented: $showTokenAlert) {
            Button("Copy") {
                UIPasteboard.general.string = appState.credentials?.bearerToken
            }
            Button("Done", role: .cancel) {}
        } message: {
            Text("This is the only credential protecting your Mac. Treat it like a "
                 + "password.\n\n\(maskedToken)")
        }
        .alert("Unpair this device?", isPresented: $confirmUnpair) {
            Button("Unpair", role: .destructive) {
                Task {
                    try? await appState.client.revokeSelf()
                    appState.unpair()
                }
            }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text("The Bridge will revoke this device. You will need a new pairing "
                 + "code to connect again.")
        }
    }

    // MARK: Sections

    private var connection: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            DSSectionLabel(text: "Connection")
            DSTextField(placeholder: "https://…", text: $serverText,
                        systemImage: DS.Icon.device)
                .keyboardType(.URL)

            if serverChanged {
                Text("Changing the address unpairs this device: the current token "
                     + "was issued by a different server and is not sent anywhere else.")
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textSecondary)
            }

            HStack(spacing: DS.Space.s) {
                Button(isChecking ? "Checking…" : "Check connection") {
                    Task { await applyAndRefresh() }
                }
                .buttonStyle(DSButtonStyle(kind: .secondary))
                .disabled(isChecking)
            }

            statusLine
        }
        .dsCard(health == nil && healthError != nil ? .danger : .idle)
    }

    @ViewBuilder
    private var statusLine: some View {
        if let health {
            VStack(alignment: .leading, spacing: DS.Space.xs) {
                DSStatusLine(text: "Bridge reachable", status: .inTune)
                DSStatusLine(
                    text: health.grokReachable ? "grok binary found"
                                               : "grok binary missing on the Mac",
                    status: health.grokReachable ? .inTune : .warning)
                if let agents = health.activeAgents {
                    DSStatusLine(text: "\(agents) agent\(agents == 1 ? "" : "s") running",
                                 status: agents > 0 ? .active : .idle)
                }
            }
        } else if let healthError {
            DSStatusLine(text: healthError, status: .danger)
        }
    }

    private var device: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            DSSectionLabel(text: "This device")
            if let creds = appState.credentials {
                DSRowLine(label: "Server", value: creds.origin)
                DSRowLine(label: "Device ID", value: String(creds.deviceId.prefix(8)) + "…")
                Button {
                    showTokenAlert = true
                } label: {
                    HStack {
                        Text("Token").font(DS.Font.footnote)
                            .foregroundStyle(DS.Color.textSecondary)
                        Spacer()
                        Text(maskedToken).font(DS.Font.codeInline)
                            .foregroundStyle(DS.Color.textTertiary)
                        Image(systemName: DS.Icon.chevron)
                            .symbolRenderingMode(.monochrome)
                            .font(.system(size: 11, weight: .light))
                            .foregroundStyle(DS.Color.textTertiary)
                    }
                }
                .buttonStyle(.plain)
            }
            Button("Unpair") { confirmUnpair = true }
                .buttonStyle(DSButtonStyle(kind: .danger))
                .padding(.top, DS.Space.xs)
        }
        .dsCard()
    }

    private var defaults: some View {
        @Bindable var state = appState
        return VStack(alignment: .leading, spacing: DS.Space.s) {
            DSSectionLabel(text: "Defaults for new sessions")
            Picker("Model", selection: $state.defaultModel) {
                Text("Bridge default").tag("")
                ForEach(models) { model in Text(model.name).tag(model.id) }
            }
            .pickerStyle(.menu)
            .tint(DS.Color.accent)

            Picker("Reasoning effort", selection: $state.defaultEffort) {
                Text("Model default").tag("")
                ForEach(effortOptions, id: \.self) { Text($0.capitalized).tag($0) }
            }
            .pickerStyle(.menu)
            .tint(DS.Color.accent)

            Text("Kept between launches. Reasoning effort restarts the agent when "
                 + "changed mid-session; the conversation is preserved.")
                .font(DS.Font.footnote)
                .foregroundStyle(DS.Color.textTertiary)
        }
        .dsCard()
    }

    private var diagnostics: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            DSSectionLabel(text: "Diagnostics")
            if let health {
                if let uptime = health.uptimeS {
                    DSRowLine(label: "Bridge uptime", value: formatted(uptime))
                }
                if let failures = health.authFailures {
                    DSRowLine(label: "Auth failures", value: "\(failures)")
                }
                if let open = health.pairingOpen {
                    DSRowLine(label: "Pairing open", value: open ? "yes" : "no")
                }
            } else {
                Text("Not connected.").font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textTertiary)
            }
        }
        .dsCard()
    }

    // MARK: Helpers

    private var effortOptions: [String] {
        let current = models.first { $0.id == appState.defaultModel }
        return current?.reasoningEfforts ?? ["low", "medium", "high", "xhigh"]
    }

    private var maskedToken: String {
        guard let creds = appState.credentials else { return "—" }
        return String(creds.secret.prefix(4)) + "••••" + String(creds.secret.suffix(4))
    }

    private var serverChanged: Bool {
        guard let creds = appState.credentials else { return false }
        guard let url = URL(string: normalized(serverText)) else { return false }
        return !creds.matches(origin: url)
    }

    private func normalized(_ text: String) -> String {
        let trimmed = text.trimmingCharacters(in: .whitespaces)
        return trimmed.contains("://") ? trimmed : "https://\(trimmed)"
    }

    private func applyAndRefresh() async {
        if let url = URL(string: normalized(serverText)), url.host != nil {
            appState.changeServer(to: url)
        }
        await refresh()
    }

    private func refresh() async {
        isChecking = true
        defer { isChecking = false }
        do {
            let target = URL(string: normalized(serverText))
            health = try await appState.client.health(at: target)
            healthError = nil
        } catch {
            health = nil
            healthError = error.localizedDescription
        }
    }

    private func formatted(_ seconds: Double) -> String {
        let hours = Int(seconds) / 3600
        let minutes = (Int(seconds) % 3600) / 60
        return hours > 0 ? "\(hours)h \(minutes)m" : "\(minutes)m"
    }
}

struct DSRowLine: View {
    let label: String
    let value: String
    var body: some View {
        HStack {
            Text(label).font(DS.Font.footnote).foregroundStyle(DS.Color.textSecondary)
            Spacer()
            Text(value).font(DS.Font.footnote).foregroundStyle(DS.Color.text)
                .lineLimit(1).truncationMode(.middle)
        }
    }
}

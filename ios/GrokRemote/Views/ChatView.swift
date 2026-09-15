import PhotosUI
import SwiftUI

struct ChatView: View {
    let session: ChatSession

    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss

    @State private var model = ChatModel()
    @State private var draft = ""
    @State private var showPalette = false
    @State private var photoItem: PhotosPickerItem?
    @State private var pendingAttachments: [String] = []
    @State private var isUploading = false
    @State private var showMenu = false
    @State private var showSessionSettings = false

    var body: some View {
        DSScreen(
            title: session.displayTitle,
            subtitle: subtitle,
            leading: AnyView(DSBackButton { dismiss() }),
            trailing: AnyView(
                DSIconButton(systemName: DS.Icon.settings,
                             accessibilityLabel: "Session options") { showMenu = true })
        ) {
            VStack(spacing: 0) {
                transcript
                if showPalette {
                    CommandPaletteView(commands: model.commands,
                                       acpAvailable: model.acpAvailable,
                                       onRun: runCommand,
                                       query: $draft)
                        .transition(DS.Transition.overlay)
                }
                usageBar
                inputBar
            }
        }
        .background(DS.Color.bg)
        .toolbar(.hidden, for: .navigationBar)
        .navigationBarBackButtonHidden(true)
        .task {
            await model.start(client: appState.client, session: session,
                              defaultModel: session.isNew ? appState.defaultModel.nilIfEmpty : nil,
                              defaultEffort: session.isNew ? appState.defaultEffort.nilIfEmpty : nil)
        }
        .onDisappear { model.stop() }
        .onChange(of: draft) { _, value in
            withAnimation(DS.Motion.settle) {
                showPalette = value.hasPrefix("/")
            }
        }
        .onChange(of: photoItem) { _, item in
            guard let item else { return }
            Task { await upload(item) }
        }
        .confirmationDialog("Session", isPresented: $showMenu) {
            Button("Session settings") { showSessionSettings = true }
            if model.canRun {
                Button("Clear conversation") { runNamedCommand("clear", nil) }
            }
            Button("Show working directory") { showSessionSettings = true }
        }
        .sheet(isPresented: $showSessionSettings) {
            SessionSettingsView(model: model)
        }
    }

    private var subtitle: String? {
        var parts: [String] = [session.folderName]
        if let branch = session.headBranch { parts.append(branch) }
        if let current = model.currentModel { parts.append(current) }
        return parts.joined(separator: " · ")
    }

    private var usageMetrics: [(String, String)] {
        guard let usage = model.usage else { return [] }
        var out: [(String, String)] = []
        if let total = usage.totalTokens { out.append(("tokens", "\(total)")) }
        if let cached = usage.cachedReadTokens, cached > 0 {
            out.append(("cached", "\(cached)"))
        }
        if let percent = model.contextPercent { out.append(("context", "\(percent)%")) }
        if let cost = usage.costUSD, cost > 0 {
            out.append(("cost", String(format: "$%.4f", cost)))
        }
        return out
    }

    // MARK: Transcript

    private var transcript: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: DS.Space.l) {
                    if model.hasMoreHistory {
                        Button("Load earlier messages") {
                            Task { await model.loadMoreHistory() }
                        }
                        .buttonStyle(DSButtonStyle(kind: .quiet))
                        .frame(maxWidth: .infinity)
                    }
                    ForEach(model.messages) { message in
                        MessageView(message: message).id(message.id)
                    }
                    if let banner = model.banner {
                        Text(banner)
                            .font(DS.Font.footnote)
                            .foregroundStyle(DS.Color.textSecondary)
                            .frame(maxWidth: .infinity, alignment: .center)
                    }
                }
                .padding(DS.Space.l)
            }
            .onChange(of: model.messages.last?.text) { _, _ in
                guard let last = model.messages.last?.id else { return }
                withAnimation(DS.Motion.settle) { proxy.scrollTo(last, anchor: .bottom) }
            }
        }
    }

    // MARK: Usage

    @ViewBuilder
    private var usageBar: some View {
        if model.usage != nil {
            VStack(spacing: 0) {
                DSDivider().padding(.horizontal, DS.Space.l)
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: DS.Space.m) {
                        ForEach(usageMetrics, id: \.0) { metric($0.0, $0.1) }
                    }
                    .padding(.horizontal, DS.Space.l)
                    .padding(.vertical, DS.Space.s)
                }
            }
        }
    }

    private func metric(_ label: String, _ value: String) -> some View {
        // fixedSize: these are short labels in a tight row, and letting them
        // wrap turns "cached" into "cache / d".
        HStack(spacing: DS.Space.xs) {
            Text(label).font(DS.Font.label).foregroundStyle(DS.Color.textTertiary)
            Text(value).font(DS.Font.label).foregroundStyle(DS.Color.textSecondary)
                .dsMetric()
        }
        .fixedSize(horizontal: true, vertical: false)
    }

    // MARK: Input

    private var inputBar: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            if !model.canRun {
                Text(model.readOnlyReason ?? "This session is read-only.")
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textSecondary)
                    .padding(.horizontal, DS.Space.l)
            }
            if !pendingAttachments.isEmpty || isUploading {
                HStack(spacing: DS.Space.s) {
                    if isUploading { ProgressView().controlSize(.mini).tint(DS.Color.accent) }
                    ForEach(pendingAttachments, id: \.self) { path in
                        DSChip(text: (path as NSString).lastPathComponent,
                               systemImage: DS.Icon.image, status: .active)
                    }
                }
                .padding(.horizontal, DS.Space.l)
            }

            if let error = model.errorBanner {
                Text(error)
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.danger)
                    .padding(.horizontal, DS.Space.l)
            }

            HStack(alignment: .bottom, spacing: DS.Space.s) {
                PhotosPicker(selection: $photoItem, matching: .images) {
                    Image(systemName: DS.Icon.image)
                        .symbolRenderingMode(.monochrome)
                        .font(.system(size: 17, weight: .light))
                        .foregroundStyle(DS.Color.textSecondary)
                        .frame(width: DS.minTapTarget, height: DS.minTapTarget)
                }
                .accessibilityLabel("Attach an image")
                .disabled(!model.canRun)

                TextField("Message, or / for commands", text: $draft, axis: .vertical)
                    .font(DS.Font.body)
                    .foregroundStyle(DS.Color.text)
                    .lineLimit(1...6)
                    .padding(.horizontal, DS.Space.m)
                    .padding(.vertical, DS.Space.s)
                    .background(DS.Color.surface)
                    .dsHairline()
                    .disabled(!model.canRun)

                if model.isStreaming {
                    DSIconButton(systemName: DS.Icon.stop,
                                 accessibilityLabel: "Stop generating",
                                 status: .warning) {
                        Task { await model.cancel() }
                    }
                } else {
                    DSIconButton(systemName: DS.Icon.send,
                                 accessibilityLabel: "Send",
                                 status: canSend ? .inTune : .idle) { send() }
                        .disabled(!canSend)
                }
            }
            .padding(.horizontal, DS.Space.l)
            .padding(.bottom, DS.Space.s)
        }
        .padding(.top, DS.Space.s)
        .background(DS.Color.bg)
    }

    private var canSend: Bool {
        model.canRun
            && !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
            && model.isConnected
    }

    // MARK: Actions

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        let attachments = pendingAttachments
        draft = ""
        pendingAttachments = []
        showPalette = false
        Task {
            await model.send(text: text,
                             model: model.currentModel,
                             effort: model.currentEffort,
                             attachments: attachments)
        }
    }

    private func runCommand(_ command: CommandSpec, _ argument: String?) {
        draft = ""
        showPalette = false
        Task { await model.run(command: command, argument: argument) }
    }

    private func runNamedCommand(_ name: String, _ argument: String?) {
        guard let command = model.commands.first(where: { $0.name == name }) else { return }
        runCommand(command, argument)
    }

    private func upload(_ item: PhotosPickerItem) async {
        isUploading = true
        defer { isUploading = false; photoItem = nil }
        guard let data = try? await item.loadTransferable(type: Data.self) else { return }
        do {
            let path = try await appState.client.upload(
                data: data, filename: "photo.jpg", mimeType: "image/jpeg")
            pendingAttachments.append(path)
        } catch {
            model.errorBanner = error.localizedDescription
        }
    }
}

private extension String {
    var nilIfEmpty: String? { isEmpty ? nil : self }
}

struct SessionSettingsView: View {
    let model: ChatModel

    @Environment(\.dismiss) private var dismiss
    @State private var selectedModel = ""
    @State private var selectedEffort = ""

    var body: some View {
        DSScreen(
            title: "Session settings",
            subtitle: "Applies to this conversation",
            leading: AnyView(DSBackButton { dismiss() })) {
                ScrollView {
                    VStack(alignment: .leading, spacing: DS.Space.l) {
                        workingDirectory
                        configuration
                        if let error = model.errorBanner {
                            Text(error)
                                .font(DS.Font.footnote)
                                .foregroundStyle(DS.Color.danger)
                        }
                    }
                    .padding(DS.Space.l)
                }
            }
            .background(DS.Color.bg)
            .task {
                selectedModel = model.currentModel ?? ""
                selectedEffort = model.currentEffort ?? ""
            }
            .onChange(of: model.currentModel) { _, value in
                if let value { selectedModel = value }
            }
            .onChange(of: model.currentEffort) { _, value in
                selectedEffort = value ?? ""
            }
    }

    private var workingDirectory: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            DSSectionLabel(text: "Working directory")
            Text(model.workingDirectory)
                .font(DS.Font.code)
                .foregroundStyle(DS.Color.text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if !model.canRun {
                DSStatusLine(
                    text: model.readOnlyReason ?? "History is read-only.",
                    status: .warning)
            }
        }
        .dsCard(model.canRun ? .idle : .warning)
    }

    private var configuration: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            DSSectionLabel(text: "Model")
            if model.modelOptions.isEmpty {
                Text("No model catalogue available.")
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textSecondary)
            } else {
                Picker("Model", selection: $selectedModel) {
                    ForEach(model.modelOptions) { option in
                        Text(option.name).tag(option.id)
                    }
                }
                .pickerStyle(.menu)
                .tint(DS.Color.accent)
                .disabled(model.isUpdatingConfig || model.isStreaming || !model.canRun)
                .onChange(of: selectedModel) { _, value in
                    guard !value.isEmpty, value != model.currentModel,
                          !model.isUpdatingConfig, !model.isStreaming else { return }
                    Task {
                        if !(await model.updateModel(value)) {
                            selectedModel = model.currentModel ?? ""
                            selectedEffort = model.currentEffort ?? ""
                        }
                    }
                }
            }

            DSSectionLabel(text: "Reasoning effort")
            Picker("Reasoning effort", selection: $selectedEffort) {
                Text("Model default").tag("")
                ForEach(effortOptions, id: \.self) { effort in
                    Text(effort.capitalized).tag(effort)
                }
            }
            .pickerStyle(.menu)
            .tint(DS.Color.accent)
            .disabled(effortOptions.isEmpty || model.isUpdatingConfig
                      || model.isStreaming || !model.canRun)
            .onChange(of: selectedEffort) { _, value in
                guard value != model.currentEffort,
                      !model.isUpdatingConfig, !model.isStreaming, model.canRun else { return }
                Task {
                    if !(await model.updateEffort(value.isEmpty ? nil : value)) {
                        selectedEffort = model.currentEffort ?? ""
                    }
                }
            }

            if model.isUpdatingConfig {
                DSStatusLine(text: "Updating session…", status: .active)
            } else {
                Text("Changing reasoning effort restarts the agent and reloads the "
                     + "same conversation.")
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textTertiary)
            }
        }
        .dsCard()
        .disabled(!model.canRun || model.isStreaming)
    }

    private var effortOptions: [String] {
        model.modelOptions.first { $0.id == selectedModel }?.reasoningEfforts ?? []
    }
}

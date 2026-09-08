import SwiftUI
import PhotosUI

private struct ToolRow: Identifiable {
    var id: String
    var tool: String
    var status: String  // "running" | "completed" | "failed"
}

private struct ChatMessage: Identifiable {
    var id = UUID()
    var role: String  // "user" | "assistant"
    var text: String = ""
    var thought: String = ""
    var showThought = false
    var tools: [ToolRow] = []
    var isStreaming = false
    var errorMessage: String?
}

struct ChatView: View {
    let session: ChatSession
    @Environment(AppState.self) private var appState

    @State private var messages: [ChatMessage] = []
    @State private var draft = ""
    @State private var models: [ModelInfo] = []
    @State private var selectedModel: String?
    @State private var reasoningEffort: ReasoningEffort = .high
    @State private var activeStream: ChatStream?
    @State private var photoPickerItem: PhotosPickerItem?
    @State private var pendingAttachments: [String] = []
    @State private var isUploading = false
    @State private var uploadError: String?

    var body: some View {
        VStack(spacing: 0) {
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 12) {
                        ForEach(messages) { message in
                            MessageBubble(message: message)
                                .id(message.id)
                        }
                    }
                    .padding()
                }
                .onChange(of: messages.last?.text) { _, _ in
                    if let last = messages.last?.id {
                        proxy.scrollTo(last, anchor: .bottom)
                    }
                }
            }
            inputBar
        }
        .navigationTitle(session.title ?? session.cwd)
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Menu {
                    Picker("模型", selection: $selectedModel) {
                        ForEach(models) { model in
                            Text(model.id).tag(Optional(model.id))
                        }
                    }
                    Picker("思考深度", selection: $reasoningEffort) {
                        ForEach(ReasoningEffort.allCases) { effort in
                            Text(effort.label).tag(effort)
                        }
                    }
                } label: {
                    Image(systemName: "slider.horizontal.3")
                }
            }
        }
        .task {
            if let resp = try? await appState.client.fetchModels() {
                models = resp.models
                selectedModel = resp.defaultModel
            }
        }
    }

    private var currentModelVision: Bool {
        models.first(where: { $0.id == selectedModel })?.vision ?? false
    }

    private var inputBar: some View {
        VStack(alignment: .leading, spacing: 4) {
            if !pendingAttachments.isEmpty || isUploading {
                HStack(spacing: 6) {
                    if isUploading {
                        ProgressView().controlSize(.mini)
                    }
                    ForEach(pendingAttachments, id: \.self) { path in
                        Label((path as NSString).lastPathComponent, systemImage: "photo")
                            .font(.caption)
                            .padding(.horizontal, 8).padding(.vertical, 4)
                            .background(Color.gray.opacity(0.15))
                            .clipShape(Capsule())
                    }
                }
                .padding(.horizontal)
            }
            if let uploadError {
                Text(uploadError).font(.caption).foregroundStyle(.red).padding(.horizontal)
            }

            HStack(alignment: .bottom, spacing: 8) {
                PhotosPicker(selection: $photoPickerItem, matching: .images) {
                    Image(systemName: "photo.badge.plus").font(.title2)
                }
                .disabled(!currentModelVision)
                .opacity(currentModelVision ? 1 : 0.3)

                TextField("发送消息…", text: $draft, axis: .vertical)
                    .textFieldStyle(.roundedBorder)
                    .lineLimit(1...5)

                if activeStream != nil {
                    Button {
                        activeStream?.cancel()
                    } label: {
                        Image(systemName: "stop.circle.fill").font(.title2)
                    }
                } else {
                    Button {
                        send()
                    } label: {
                        Image(systemName: "arrow.up.circle.fill").font(.title2)
                    }
                    .disabled(draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
            .padding(.horizontal).padding(.bottom)
        }
        .padding(.top, 8)
        .onChange(of: photoPickerItem) { _, item in
            guard let item else { return }
            Task { await uploadPickedPhoto(item) }
        }
    }

    private func uploadPickedPhoto(_ item: PhotosPickerItem) async {
        isUploading = true
        uploadError = nil
        defer { isUploading = false; photoPickerItem = nil }
        do {
            guard let data = try await item.loadTransferable(type: Data.self) else { return }
            let path = try await appState.client.upload(data: data, filename: "photo.jpg", mimeType: "image/jpeg")
            pendingAttachments.append(path)
        } catch {
            uploadError = "上传失败: \(error.localizedDescription)"
        }
    }

    private func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        draft = ""
        let attachments = pendingAttachments
        pendingAttachments = []

        messages.append(ChatMessage(role: "user", text: text))
        let assistantIndex = messages.count
        messages.append(ChatMessage(role: "assistant", isStreaming: true))

        let stream = appState.client.chat(
            sessionId: session.id,
            text: text,
            model: selectedModel,
            reasoningEffort: reasoningEffort.rawValue,
            attachments: attachments
        )
        activeStream = stream

        Task {
            do {
                for try await event in stream.events() {
                    apply(event, at: assistantIndex)
                }
            } catch {
                messages[assistantIndex].errorMessage = error.localizedDescription
            }
            messages[assistantIndex].isStreaming = false
            activeStream = nil
        }
    }

    private func apply(_ event: BridgeEvent, at index: Int) {
        guard messages.indices.contains(index) else { return }
        switch event.type {
        case "message.delta":
            messages[index].text += event.data ?? ""
        case "message.thought":
            messages[index].thought += event.data ?? ""
        case "tool.started":
            messages[index].tools.append(ToolRow(id: event.toolCallId ?? UUID().uuidString, tool: event.tool ?? "tool", status: "running"))
        case "tool.finished":
            if let i = messages[index].tools.firstIndex(where: { $0.id == event.toolCallId }) {
                messages[index].tools[i].status = event.status ?? "completed"
            }
        case "message.error":
            messages[index].errorMessage = event.message
        case "cancelled":
            messages[index].errorMessage = "已取消"
        default:
            break
        }
    }
}

private struct MessageBubble: View {
    let message: ChatMessage

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if !message.thought.isEmpty {
                DisclosureGroup("思考中…") {
                    Text(message.thought)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            ForEach(message.tools) { tool in
                HStack(spacing: 6) {
                    Image(systemName: tool.status == "completed" ? "checkmark.circle.fill" :
                                      tool.status == "failed" ? "xmark.circle.fill" : "circle.dashed")
                        .foregroundStyle(tool.status == "failed" ? .red : .secondary)
                    Text("\(tool.tool)").font(.footnote.monospaced())
                }
            }
            if !message.text.isEmpty {
                Text(message.text)
            }
            if message.isStreaming && message.text.isEmpty && message.tools.isEmpty {
                ProgressView().controlSize(.small)
            }
            if let error = message.errorMessage {
                Text(error).foregroundStyle(.red).font(.footnote)
            }
        }
        .padding(10)
        .background(message.role == "user" ? Color.accentColor.opacity(0.15) : Color.gray.opacity(0.1))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .frame(maxWidth: .infinity, alignment: message.role == "user" ? .trailing : .leading)
    }
}

import SwiftUI

struct SessionsListView: View {
    @Environment(AppState.self) private var appState
    @State private var sessions: [ChatSession] = []
    @State private var healthOK: Bool?
    @State private var showNewSession = false
    @State private var isLoading = false

    var body: some View {
        NavigationStack {
            Group {
                if healthOK == false {
                    ContentUnavailableView(
                        "无法连接到 Bridge",
                        systemImage: "wifi.slash",
                        description: Text("Mac 可能已休眠或离线，请检查后重试")
                    )
                } else if sessions.isEmpty && !isLoading {
                    ContentUnavailableView("还没有会话", systemImage: "bubble.left.and.bubble.right")
                } else {
                    List(sessions) { session in
                        NavigationLink(value: session) {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(session.title ?? session.cwd).font(.headline)
                                Text(session.lastMessagePreview ?? session.cwd)
                                    .font(.subheadline)
                                    .foregroundStyle(.secondary)
                                    .lineLimit(1)
                            }
                        }
                    }
                }
            }
            .navigationTitle("会话")
            .navigationDestination(for: ChatSession.self) { ChatView(session: $0) }
            .toolbar {
                ToolbarItem(placement: .primaryAction) {
                    Button { showNewSession = true } label: { Image(systemName: "plus") }
                }
            }
            .sheet(isPresented: $showNewSession) {
                NewSessionView { session in
                    sessions.insert(session, at: 0)
                }
            }
            .task { await refresh() }
            .refreshable { await refresh() }
        }
    }

    private func refresh() async {
        isLoading = true
        defer { isLoading = false }
        healthOK = (try? await appState.client.health()) ?? false
        guard healthOK == true else { return }
        sessions = (try? await appState.client.fetchSessions()) ?? []
    }
}

private struct NewSessionView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    @State private var cwd = ""
    @State private var recentDirs: [String] = []
    @State private var errorMessage: String?
    @State private var isCreating = false
    var onCreated: (ChatSession) -> Void

    var body: some View {
        NavigationStack {
            Form {
                Section("项目目录") {
                    TextField("/Users/v0id/my-project", text: $cwd)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                }
                if !recentDirs.isEmpty {
                    Section("最近用过") {
                        ForEach(recentDirs, id: \.self) { dir in
                            Button(dir) { cwd = dir }
                        }
                    }
                }
                if let errorMessage {
                    Text(errorMessage).foregroundStyle(.red)
                }
            }
            .navigationTitle("新建会话")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("取消") { dismiss() }
                }
                ToolbarItem(placement: .confirmationAction) {
                    Button("创建") { Task { await create() } }
                        .disabled(cwd.isEmpty || isCreating)
                }
            }
            .task { recentDirs = (try? await appState.client.fetchRecentDirs()) ?? [] }
        }
    }

    private func create() async {
        isCreating = true
        do {
            let session = try await appState.client.createSession(cwd: cwd)
            onCreated(session)
            dismiss()
        } catch {
            errorMessage = "创建失败: \(error.localizedDescription)"
        }
        isCreating = false
    }
}

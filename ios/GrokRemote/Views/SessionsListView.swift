import SwiftUI

struct SessionsListView: View {
    @Environment(AppState.self) private var appState

    @State private var sessions: [ChatSession] = []
    @State private var isLoading = true
    @State private var offline = false
    @State private var showPicker = false
    @State private var showSettings = false
    @State private var search = ""
    @State private var renaming: ChatSession?
    @State private var renameText = ""
    @State private var path: [ChatSession] = []

    var body: some View {
        NavigationStack(path: $path) {
            DSScreen(
                title: "Sessions",
                subtitle: sessions.isEmpty ? nil : "\(sessions.count) on this Mac",
                trailing: AnyView(
                    HStack(spacing: 0) {
                        DSIconButton(systemName: DS.Icon.gear,
                                     accessibilityLabel: "Settings") { showSettings = true }
                        DSIconButton(systemName: DS.Icon.plus,
                                     accessibilityLabel: "New session") { showPicker = true }
                    })
            ) {
                content
            }
            // DSScreen draws its own header; the system bar would double it.
            .toolbar(.hidden, for: .navigationBar)
            .navigationDestination(for: ChatSession.self) { ChatView(session: $0) }
        }
        .task { await refresh() }
        .sheet(isPresented: $showPicker) {
            ProjectPickerView { session in
                sessions.insert(session, at: 0)
                showPicker = false
                path.append(session)
            }
        }
        .sheet(isPresented: $showSettings) { SettingsView() }
        .alert("Rename session", isPresented: Binding(
            get: { renaming != nil },
            set: { if !$0 { renaming = nil } })) {
            TextField("Title", text: $renameText)
            Button("Save") { Task { await commitRename() } }
            Button("Cancel", role: .cancel) { renaming = nil }
        }
    }

    @ViewBuilder
    private var content: some View {
        if offline {
            DSEmptyState(
                title: "Can't reach the Bridge",
                systemImage: DS.Icon.offline,
                detail: "The Mac may be asleep or offline. Check the address in Settings.",
                action: (title: "Retry", run: { Task { await refresh() } }))
        } else if sessions.isEmpty && !isLoading {
            DSEmptyState(
                title: "No sessions yet",
                systemImage: DS.Icon.repository,
                detail: "Pick a project directory on the Mac to start one.",
                action: (title: "New session", run: { showPicker = true }))
        } else {
            ScrollView {
                LazyVStack(spacing: DS.Space.m) {
                    DSTextField(placeholder: "Search sessions", text: $search,
                                systemImage: "magnifyingglass")
                    ForEach(Array(filtered.enumerated()), id: \.element.id) { index, session in
                        NavigationLink(value: session) {
                            SessionRow(session: session)
                        }
                        .buttonStyle(.plain)
                        .dsReveal(min(index, 6))
                        .contextMenu {
                            Button("Rename") {
                                renameText = session.title ?? ""
                                renaming = session
                            }
                            Button("Delete", role: .destructive) {
                                Task { await delete(session) }
                            }
                        }
                    }
                }
                .padding(DS.Space.l)
            }
            .refreshable { await refresh() }
        }
    }

    private var filtered: [ChatSession] {
        guard !search.isEmpty else { return sessions }
        let needle = search.lowercased()
        return sessions.filter {
            $0.displayTitle.lowercased().contains(needle)
                || $0.cwd.lowercased().contains(needle)
        }
    }

    private func refresh() async {
        isLoading = true
        defer { isLoading = false }
        do {
            sessions = try await appState.client.sessions()
            offline = false
        } catch {
            offline = true
        }
    }

    private func commitRename() async {
        guard let session = renaming else { return }
        renaming = nil
        try? await appState.client.patchSession(session.id, fields: ["title": renameText])
        await refresh()
    }

    private func delete(_ session: ChatSession) async {
        try? await appState.client.deleteSession(session.id)
        sessions.removeAll { $0.id == session.id }
    }
}

private struct SessionRow: View {
    let session: ChatSession

    var body: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            HStack(alignment: .firstTextBaseline) {
                Text(session.displayTitle)
                    .font(DS.Font.headline)
                    .foregroundStyle(DS.Color.text)
                    .lineLimit(1)
                Spacer(minLength: DS.Space.s)
                if session.state == "running" {
                    DSChip(text: "running", status: .active)
                }
            }

            Text(session.cwd)
                .font(DS.Font.footnote)
                .foregroundStyle(DS.Color.textTertiary)
                .lineLimit(1)
                .truncationMode(.head)

            if let summary = session.lastTurnSummary ?? session.lastMessagePreview,
               !summary.isEmpty {
                Text(summary)
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textSecondary)
                    .lineLimit(2)
            }

            HStack(spacing: DS.Space.s) {
                if let branch = session.headBranch {
                    DSChip(text: branch, systemImage: DS.Icon.branch)
                }
                if let model = session.model {
                    DSChip(text: model)
                }
                if let count = session.messageCount {
                    Text("\(count)")
                        .font(DS.Font.label)
                        .foregroundStyle(DS.Color.textTertiary)
                        .dsMetric()
                }
                Spacer(minLength: 0)
                if session.cwdExists == false {
                    DSChip(text: "folder missing", status: .warning)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .dsCard(session.cwdExists == false ? .warning : .idle)
    }
}

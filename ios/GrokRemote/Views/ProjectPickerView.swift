import SwiftUI

/// Choose a working directory.
///
/// Replaces a bare text field that wanted an absolute path typed on a phone.
/// Three routes, because none alone covers everything: discovered repositories
/// (what you want almost always), a directory browser (for anything else), and
/// manual entry as the escape hatch.
struct ProjectPickerView: View {
    @Environment(AppState.self) private var appState
    @Environment(\.dismiss) private var dismiss
    let onCreate: (ChatSession) -> Void

    private enum Tab: String, CaseIterable { case repos = "Repos", browse = "Browse", manual = "Manual" }

    @State private var tab: Tab = .repos
    @State private var repos: [RepoInfo] = []
    @State private var statuses: [String: RepoStatus] = [:]
    @State private var recent: [String] = []
    @State private var search = ""
    @State private var listing: FSListing?
    @State private var manualPath = ""
    @State private var isWorking = false
    @State private var errorMessage: String?
    @AppStorage("starredRepos") private var starredRaw = ""

    var body: some View {
        DSScreen(title: "New session",
                 leading: AnyView(DSBackButton(accessibilityLabel: "Close") { dismiss() })) {
            VStack(spacing: 0) {
                Picker("", selection: $tab) {
                    ForEach(Tab.allCases, id: \.self) { Text($0.rawValue).tag($0) }
                }
                .pickerStyle(.segmented)
                .padding(DS.Space.l)

                if let errorMessage {
                    Text(errorMessage)
                        .font(DS.Font.footnote)
                        .foregroundStyle(DS.Color.danger)
                        .padding(.horizontal, DS.Space.l)
                }

                switch tab {
                case .repos:  reposTab
                case .browse: browseTab
                case .manual: manualTab
                }
            }
        }
        .background(DS.Color.bg)
        .task {
            repos = (try? await appState.client.repos()) ?? []
            recent = (try? await appState.client.recentDirs()) ?? []
            await loadStatuses(for: Array(visibleRepos.prefix(25)))
            listing = try? await appState.client.listDirectory(NSHomeDirectory())
        }
    }

    // MARK: Repos

    private var reposTab: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: DS.Space.m) {
                DSTextField(placeholder: "Filter repositories", text: $search)
                    .padding(.horizontal, DS.Space.l)

                if !recent.isEmpty && search.isEmpty {
                    VStack(alignment: .leading, spacing: DS.Space.s) {
                        DSSectionLabel(text: "Recent")
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: DS.Space.s) {
                                ForEach(recent, id: \.self) { dir in
                                    Button { Task { await create(cwd: dir) } } label: {
                                        DSChip(text: (dir as NSString).lastPathComponent,
                                               systemImage: DS.Icon.history)
                                    }
                                    .buttonStyle(.plain)
                                }
                            }
                        }
                    }
                    .padding(.horizontal, DS.Space.l)
                }

                LazyVStack(spacing: DS.Space.m) {
                    ForEach(visibleRepos) { repo in
                        Button { Task { await create(cwd: repo.path) } } label: {
                            RepoRow(repo: repo, status: statuses[repo.path],
                                    starred: starred.contains(repo.path),
                                    toggleStar: { toggleStar(repo.path) })
                        }
                        .buttonStyle(.plain)
                    }
                }
                .padding(.horizontal, DS.Space.l)
            }
            .padding(.bottom, DS.Space.xl)
        }
    }

    private var visibleRepos: [RepoInfo] {
        let starredSet = starred
        let matching = search.isEmpty ? repos : repos.filter {
            $0.name.lowercased().contains(search.lowercased())
                || $0.path.lowercased().contains(search.lowercased())
        }
        return matching.sorted { lhs, rhs in
            let l = starredSet.contains(lhs.path), r = starredSet.contains(rhs.path)
            if l != r { return l }
            return lhs.name.lowercased() < rhs.name.lowercased()
        }
    }

    // MARK: Browse

    @ViewBuilder
    private var browseTab: some View {
        if let listing {
            ScrollView {
                VStack(alignment: .leading, spacing: 0) {
                    HStack {
                        Text(listing.path)
                            .font(DS.Font.footnote)
                            .foregroundStyle(DS.Color.textSecondary)
                            .lineLimit(1).truncationMode(.head)
                        Spacer()
                        Button("Use this") { Task { await create(cwd: listing.path) } }
                            .buttonStyle(DSButtonStyle(kind: .quiet))
                    }
                    .padding(.horizontal, DS.Space.l)
                    .padding(.bottom, DS.Space.s)

                    if let parent = listing.parent {
                        browseRow(name: "..", path: parent, isRepo: false, denied: false)
                    }
                    ForEach(listing.entries) { entry in
                        browseRow(name: entry.name, path: entry.path,
                                  isRepo: entry.isRepo, denied: entry.isDenied)
                    }
                }
            }
        } else {
            ProgressView().tint(DS.Color.accent)
                .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func browseRow(name: String, path: String, isRepo: Bool, denied: Bool) -> some View {
        Button {
            guard !denied else { return }
            Task { listing = try? await appState.client.listDirectory(path) }
        } label: {
            HStack(spacing: DS.Space.s) {
                Image(systemName: isRepo ? DS.Icon.repository : DS.Icon.folder)
                    .symbolRenderingMode(.monochrome)
                    .font(.system(size: 14, weight: .light))
                    .foregroundStyle(isRepo ? DS.Color.accent : DS.Color.textTertiary)
                Text(name)
                    .font(DS.Font.body)
                    .foregroundStyle(denied ? DS.Color.textTertiary : DS.Color.text)
                Spacer()
                if denied {
                    DSChip(text: "off-limits", status: .warning)
                } else {
                    Image(systemName: DS.Icon.chevron)
                        .symbolRenderingMode(.monochrome)
                        .font(.system(size: 11, weight: .light))
                        .foregroundStyle(DS.Color.textTertiary)
                }
            }
            .padding(.horizontal, DS.Space.l)
            .frame(minHeight: DS.minTapTarget)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(denied)
    }

    // MARK: Manual

    private var manualTab: some View {
        VStack(alignment: .leading, spacing: DS.Space.m) {
            DSSectionLabel(text: "Absolute path on the Mac")
            DSTextField(placeholder: "/Users/you/my-project", text: $manualPath,
                        systemImage: DS.Icon.folder)
            DSPrimaryButton(title: "Create session",
                            isEnabled: !manualPath.isEmpty && !isWorking) {
                Task { await create(cwd: manualPath) }
            }
            Spacer()
        }
        .padding(DS.Space.l)
    }

    // MARK: Actions

    private var starred: Set<String> {
        Set(starredRaw.split(separator: "\n").map(String.init))
    }

    private func toggleStar(_ path: String) {
        var current = starred
        if current.contains(path) { current.remove(path) } else { current.insert(path) }
        starredRaw = current.sorted().joined(separator: "\n")
    }

    private func loadStatuses(for repos: [RepoInfo]) async {
        guard !repos.isEmpty else { return }
        if let fetched = try? await appState.client.repoStatus(paths: repos.map(\.path)) {
            statuses.merge(fetched) { _, new in new }
        }
    }

    private func create(cwd: String) async {
        guard !isWorking else { return }
        isWorking = true
        defer { isWorking = false }
        do {
            onCreate(try await appState.client.createSession(cwd: cwd))
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}

private struct RepoRow: View {
    let repo: RepoInfo
    let status: RepoStatus?
    let starred: Bool
    let toggleStar: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            HStack {
                Text(repo.name)
                    .font(DS.Font.headline)
                    .foregroundStyle(DS.Color.text)
                Spacer()
                Button(action: toggleStar) {
                    Image(systemName: starred ? "star.fill" : "star")
                        .symbolRenderingMode(.monochrome)
                        .font(.system(size: 13, weight: .light))
                        .foregroundStyle(starred ? DS.Color.accent : DS.Color.textTertiary)
                }
                .buttonStyle(.plain)
                .accessibilityLabel(starred ? "Unstar \(repo.name)" : "Star \(repo.name)")
            }

            Text(shortPath)
                .font(DS.Font.footnote)
                .foregroundStyle(DS.Color.textTertiary)
                .lineLimit(1).truncationMode(.head)

            HStack(spacing: DS.Space.s) {
                if let branch = status?.branch {
                    DSChip(text: branch, systemImage: DS.Icon.branch)
                }
                if repo.isWorktree { DSChip(text: "worktree") }
                if let dirty = status?.dirtyCount, dirty > 0 {
                    DSChip(text: "\(dirty) changed", status: .warning)
                }
                if status?.statusUnavailable != nil {
                    // `git status` genuinely hangs in some worktrees; saying so
                    // beats a row that looks like a clean checkout.
                    DSChip(text: "status unavailable", status: .warning)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .dsCard()
    }

    private var shortPath: String {
        repo.path.replacingOccurrences(of: NSHomeDirectory(), with: "~")
    }
}

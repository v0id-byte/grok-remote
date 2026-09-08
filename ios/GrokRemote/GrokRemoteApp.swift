import SwiftUI

@main
struct GrokRemoteApp: App {
    @State private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(appState)
        }
    }
}

struct RootView: View {
    @Environment(AppState.self) private var appState

    var body: some View {
        if appState.isPaired {
            SessionsListView()
        } else {
            PairingView()
        }
    }
}

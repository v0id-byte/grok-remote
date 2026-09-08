import SwiftUI

@main
struct GrokRemoteApp: App {
    @State private var appState = AppState()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(appState)
                .tint(DS.Color.accent)
        }
    }
}

struct RootView: View {
    @Environment(AppState.self) private var appState

    var body: some View {
        ZStack {
            DS.Color.bg.ignoresSafeArea()
            if appState.isPaired {
                SessionsListView()
            } else {
                PairingView()
            }
        }
        .animation(DS.Motion.screen, value: appState.isPaired)
    }
}

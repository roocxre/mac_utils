import AppKit
import SwiftUI

@main
@MainActor
enum Main {
    static func main() {
        let args = CommandLine.arguments
        if let i = args.firstIndex(of: "--self-test") {
            exit(SelfTest.run(Array(args[(i + 1)...])))
        }
        MacUtilsApp.main()
    }
}

struct MacUtilsApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @StateObject private var model = AppModel()

    var body: some Scene {
        Window("Mac Utils", id: "main") {
            ContentView()
                .environmentObject(model)
                .frame(minWidth: 860, minHeight: 580)
                .onAppear { delegate.model = model }
        }
        .windowResizability(.contentMinSize)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    weak var model: AppModel?

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool { true }

    /// Quitting stops whatever is running rather than leaving it to finish unseen.
    func applicationWillTerminate(_ notification: Notification) {
        model?.install.cancel()
        model?.setupCommand.cancel()
        model?.manageCommand.cancel()
    }
}

enum Screen: String, CaseIterable, Identifiable {
    case components, install, setup, manage

    var id: String { rawValue }
    var title: String { rawValue.capitalized }
    var symbol: String {
        switch self {
        case .components: return "checklist"
        case .install: return "arrow.down.circle"
        case .setup: return "hand.raised"
        case .manage: return "wrench.and.screwdriver"
        }
    }
}

/// What every screen shares: the catalog, the install record, the ticked components and the commands.
@MainActor
final class AppModel: ObservableObject {
    @Published var catalog: Catalog?
    @Published var loadError: String?
    @Published var installed: [String: String] = [:]   // component id -> version, from the record
    @Published var selection = Selection()
    @Published var screen: Screen? = .components
    let install = InstallModel()
    let setupCommand = CommandModel()
    let manageCommand = CommandModel()

    init() {
        do {
            catalog = try Catalog.load(payload: Paths.payload)
        } catch {
            loadError = "Couldn't read the components in \(Paths.payload.path): \(error.localizedDescription)"
        }
        refresh()
        install.onFinish = { [weak self] ok in
            self?.refresh()
            if ok { self?.selection = Selection() }
        }
    }

    func refresh() { installed = Record.load() ?? [:] }

    var installedIDs: Set<String> { Set(installed.keys) }

    /// True once install.sh has made the virtualenv that manage.py runs in.
    var hasPython: Bool { FileManager.default.isExecutableFile(atPath: Paths.python) }

    var planned: [String] { catalog?.ordered(selection.ids) ?? [] }

    func toggle(_ id: String, _ on: Bool) {
        guard let catalog else { return }
        selection.set(id, on, catalog: catalog, installed: installedIDs)
    }

    func startInstall() {
        install.start(installCommand(payload: Paths.payload, uv: Paths.uv, ids: planned))
        screen = .install
    }
}

struct ContentView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        NavigationSplitView {
            List(Screen.allCases, selection: $model.screen) { screen in
                Label(screen.title, systemImage: screen.symbol).tag(screen)
            }
            .navigationSplitViewColumnWidth(170)
        } detail: {
            if let error = model.loadError {
                ContentUnavailableView("The app is incomplete", systemImage: "exclamationmark.triangle",
                                       description: Text(error))
            } else {
                switch model.screen ?? .components {
                case .components: ComponentsView()
                case .install: InstallView(install: model.install)
                case .setup: SetupView(command: model.setupCommand)
                case .manage: ManageView(command: model.manageCommand)
                }
            }
        }
    }
}

/// A command's output in a small scrolling box that follows the newest line.
struct LogView: View {
    let lines: [String]

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 1) {
                    ForEach(lines.indices, id: \.self) { i in
                        Text(lines[i]).font(.system(.caption, design: .monospaced)).id(i)
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(8)
                .textSelection(.enabled)
            }
            .background(Color(nsColor: .textBackgroundColor))
            .clipShape(RoundedRectangle(cornerRadius: 6))
            .onChange(of: lines.count) { proxy.scrollTo(lines.count - 1, anchor: .bottom) }
        }
    }
}

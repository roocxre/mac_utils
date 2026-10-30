import AppKit
import SwiftUI
import UniformTypeIdentifiers

/// After installing: the permissions the installed components need and each one's own setup.
struct SetupView: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject var command: CommandModel

    private var installed: [Component] {
        model.catalog?.components.filter { model.installed[$0.id] != nil } ?? []
    }

    var body: some View {
        let ids = Set(installed.map(\.id))
        Form {
            if installed.isEmpty {
                Text("Nothing is installed yet. Install components first, then come back here.")
                    .foregroundStyle(.secondary)
            } else {
                permissions
                if ids.contains("mail") { GmailSection(command: command) }
                if ids.contains("dataflow") { EdgeSection() }
                if ids.contains("brokers") {
                    Section("Data broker details") {
                        Text("Enter your name and addresses and choose which brokers to contact. Nothing is sent until you approve it.")
                        Button("Open data broker details") { command.python(["-m", "brokers", "open"]) }
                            .disabled(command.running)
                    }
                }
                if command.output.count > 1 || command.running {
                    Section("Output") {
                        LogView(lines: command.output).frame(minHeight: 120, maxHeight: 220)
                        if command.running { Button("Stop", role: .cancel) { command.cancel() } }
                    }
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Setup")
        .onAppear { model.refresh() }
    }

    /// Titles of the installed components that need this permission.
    private func users(of kind: Permission) -> [String] {
        installed.filter { $0.permissions.contains { Permission.kind(of: $0) == kind } }.map(\.title)
    }

    private var permissions: some View {
        let needed = Permission.allCases.filter { !users(of: $0).isEmpty }
        return Section {
            if needed.isEmpty {
                Text("None of the installed components needs a permission.").foregroundStyle(.secondary)
            }
            ForEach(needed) { kind in
                HStack(alignment: .top) {
                    Image(systemName: kind.symbol).frame(width: 20)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(kind.title).font(.headline)
                        Text(kind.why).font(.callout)
                        Text("Needed by " + users(of: kind).joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Button("Open Settings") { NSWorkspace.shared.open(kind.settingsURL) }
                }
            }
        } header: {
            Text("Permissions")
        } footer: {
            Text("macOS usually asks the first time an agent needs a permission. If you said no, or nothing asked, turn it on in the pane that opens. The agents run as Python, so look for an entry named python.")
        }
    }
}

struct GmailSection: View {
    @ObservedObject var command: CommandModel
    @State private var clientFile: URL?
    @State private var choosing = false

    private let steps: [(String, String?)] = [
        ("Create a project in the Google Cloud console (any name).",
         "https://console.cloud.google.com/projectcreate"),
        ("Enable the Gmail API for that project.",
         "https://console.cloud.google.com/apis/library/gmail.googleapis.com"),
        ("Set up the OAuth consent screen: choose External, add yourself as a test user, then click Publish app so the status says \"In production\". In Testing, the sign-in expires after 7 days.",
         "https://console.cloud.google.com/apis/credentials/consent"),
        ("Create credentials > OAuth client ID > Desktop app, then download its JSON file.",
         "https://console.cloud.google.com/apis/credentials"),
        ("Choose that file below and click Sign in.", nil),
    ]

    var body: some View {
        Section {
            ForEach(steps.indices, id: \.self) { i in
                HStack(alignment: .top) {
                    Text("\(i + 1).").monospacedDigit().frame(width: 18, alignment: .trailing)
                    Text(steps[i].0)
                    Spacer()
                    if let link = steps[i].1, let url = URL(string: link) { Link("Open", destination: url) }
                }
            }
            HStack {
                Button("Choose client JSON…") { choosing = true }
                Text(clientFile?.lastPathComponent ?? "No file chosen").foregroundStyle(.secondary).lineLimit(1)
                Spacer()
                Button("Sign in") {
                    if let clientFile { command.python(["-m", "mail", "setup", clientFile.path]) }
                }
                .disabled(clientFile == nil || command.running)
            }
            Label("Google will say it hasn't verified the app. That's expected, because the app is your own: click Advanced, then Go to (your app's name), and keep both permissions ticked.",
                  systemImage: "info.circle")
                .font(.callout).foregroundStyle(.secondary)
        } header: {
            Text("Gmail sign-in")
        }
        .fileImporter(isPresented: $choosing, allowedContentTypes: [.json]) { result in
            if case .success(let url) = result { clientFile = url }
        }
    }
}

struct EdgeSection: View {
    private let edge = NSWorkspace.shared.urlForApplication(withBundleIdentifier: "com.microsoft.edgemac")

    var body: some View {
        Section("Edge extension") {
            Text("1. Reveal the extension folder.")
            Text("2. In Microsoft Edge, go to edge://extensions and turn on Developer mode.")
            Text("3. Click Load unpacked and choose the extension folder.")
            HStack {
                Button("Reveal the extension folder") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: Paths.extensionDir)])
                }
                Button("Copy edge://extensions") {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString("edge://extensions", forType: .string)
                }
                Button("Open Microsoft Edge") {
                    if let edge { NSWorkspace.shared.openApplication(at: edge, configuration: .init()) }
                }
                .disabled(edge == nil)
            }
            if edge == nil {
                Text("Microsoft Edge isn't installed.").font(.caption).foregroundStyle(.orange)
            }
        }
    }
}

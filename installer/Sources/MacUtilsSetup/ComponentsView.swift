import SwiftUI

struct ComponentsView: View {
    @EnvironmentObject var model: AppModel

    var body: some View {
        if let catalog = model.catalog {
            VStack(spacing: 0) {
                Form {
                    Section {
                        Text("Tick what you want. The core (manage.py, the shared code and Python) is always installed, and ticking a component also ticks the ones it needs.")
                            .foregroundStyle(.secondary)
                    }
                    ForEach(catalog.groups) { group in
                        Section(group.title) {
                            ForEach(catalog.components(in: group.id)) { component in
                                ComponentRow(catalog: catalog, component: component)
                            }
                        }
                    }
                }
                .formStyle(.grouped)
                Divider()
                footer(catalog)
            }
            .navigationTitle("Components")
        }
    }

    private func footer(_ catalog: Catalog) -> some View {
        let planned = model.planned
        let download = catalog.download(selected: model.selection.ids, installed: model.installedIDs)
        return HStack {
            VStack(alignment: .leading, spacing: 2) {
                Text(planned.isEmpty ? "Nothing ticked" : "\(planned.count) to install: " + planned.map(catalog.title).joined(separator: ", "))
                    .lineLimit(2)
                Text(download.bytes > 0
                     ? "About \(formatBytes(download.bytes)) of models to download, plus Python and its packages."
                     : "No models to download; Python and its packages are fetched if missing.")
                    .font(.caption).foregroundStyle(.secondary)
            }
            Spacer()
            Button("Install") { model.startInstall() }
                .keyboardShortcut(.defaultAction)
                .disabled(planned.isEmpty || model.install.running)
        }
        .padding(12)
    }
}

struct ComponentRow: View {
    @EnvironmentObject var model: AppModel
    let catalog: Catalog
    let component: Component

    private var ticked: Binding<Bool> {
        Binding(get: { model.selection.ids.contains(component.id) },
                set: { model.toggle(component.id, $0) })
    }

    var body: some View {
        if let version = model.installed[component.id] {
            HStack(alignment: .top) {
                Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
                details
                Spacer()
                VStack(alignment: .trailing, spacing: 4) {
                    Text(version.isEmpty ? "Installed" : "Installed (\(version))").font(.caption.bold())
                    Toggle("Reinstall", isOn: ticked).toggleStyle(.checkbox)
                }
            }
        } else {
            Toggle(isOn: ticked) { details }.toggleStyle(.checkbox)
        }
    }

    private var details: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(component.title).font(.headline)
            Text(component.summary)
                .font(.callout).foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                ForEach(component.permissions, id: \.self) { Tag(text: Permission.tag($0), symbol: "lock") }
                ForEach(component.apps, id: \.self) { app in
                    Tag(text: "Needs \(app)" + (appFound(app) ? "" : " (not found)"), symbol: "app")
                }
                if !component.requires.isEmpty {
                    Text("Adds " + component.requires.map(catalog.title).joined(separator: ", "))
                        .font(.caption).foregroundStyle(.secondary)
                }
            }
        }
    }

    private func appFound(_ name: String) -> Bool {
        ["/Applications", Paths.home + "/Applications"].contains {
            FileManager.default.fileExists(atPath: "\($0)/\(name).app")
        }
    }
}

struct Tag: View {
    let text: String
    let symbol: String

    var body: some View {
        Label(text, systemImage: symbol)
            .font(.caption)
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(.quaternary, in: Capsule())
    }
}

import SwiftUI

/// Remove components, uninstall everything, or check for problems, all through the installed manage.py.
struct ManageView: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject var command: CommandModel
    @State private var removing = Set<String>()
    @State private var keepData = true
    @State private var confirming = false
    @State private var checks: [Check]?

    var body: some View {
        Form {
            if !model.hasPython {
                Text("Mac Utils isn't installed yet, so there is nothing to manage.").foregroundStyle(.secondary)
            } else {
                removeSection
                Section("Check for problems") {
                    HStack {
                        Text("Looks for agents that aren't loaded, recent errors, missing permissions and missing models.")
                        Spacer()
                        Button("Check") { runDoctor() }.disabled(command.running)
                    }
                    if let checks {
                        if checks.isEmpty { Text("No problems found.").foregroundStyle(.secondary) }
                        ForEach(checks) { check in CheckRow(check: check) }
                    }
                }
                Section("Uninstall everything") {
                    Toggle("Keep my data (settings, databases and logs) for a later reinstall", isOn: $keepData)
                    HStack {
                        Text("Stops and removes every agent, the Edge helper registration, the installed code and its models. Gmail and broker sign-ins stay in the Keychain; the output says how to delete them.")
                            .font(.callout).foregroundStyle(.secondary)
                        Spacer()
                        Button("Uninstall Everything…", role: .destructive) { confirming = true }
                            .disabled(command.running)
                    }
                }
            }
            if command.output.count > 1 || command.running {
                Section("Output") {
                    LogView(lines: command.output).frame(minHeight: 120, maxHeight: 240)
                }
            }
        }
        .formStyle(.grouped)
        .navigationTitle("Manage")
        .onAppear { model.refresh() }
        .alert("Uninstall everything?", isPresented: $confirming) {
            Button("Uninstall", role: .destructive) { uninstallAll() }
            Button("Cancel", role: .cancel) {}
        } message: {
            Text(keepData ? "Your data is kept." : "Your settings, databases and logs are deleted too.")
        }
    }

    private var removeSection: some View {
        Section("Installed components") {
            let installed = model.catalog?.components.filter { model.installed[$0.id] != nil } ?? []
            if installed.isEmpty { Text("No components are installed.").foregroundStyle(.secondary) }
            ForEach(installed) { component in
                Toggle(component.title, isOn: Binding(
                    get: { removing.contains(component.id) },
                    set: { mark(component.id, $0) }))
                .toggleStyle(.checkbox)
            }
            HStack {
                Spacer()
                Button("Remove Selected") {
                    let ids = model.catalog?.ordered(removing) ?? []
                    command.python([Paths.manage, "remove"] + ids) { _ in
                        model.refresh()
                        removing = []
                    }
                }
                .disabled(removing.isEmpty || command.running)
            }
        }
    }

    /// Removing a component also removes the installed ones that require it.
    private func mark(_ id: String, _ on: Bool) {
        guard let catalog = model.catalog else { return }
        if on {
            removing.insert(id)
            for other in model.installedIDs where catalog.requirements(of: other).contains(id) {
                removing.insert(other)
            }
        } else {
            removing.remove(id)
            removing.subtract(catalog.requirements(of: id))
        }
    }

    private func runDoctor() {
        checks = nil
        command.python([Paths.manage, "doctor", "--json"]) { _ in
            checks = Check.parse(command.stdout.joined(separator: "\n"))
        }
    }

    private func uninstallAll() {
        command.python([Paths.manage, "uninstall-all"] + (keepData ? ["--keep-state"] : [])) { status in
            guard status == 0 else { return }
            command.output += deleteInstall(keepState: keepData)
            model.refresh()
            checks = nil
        }
    }
}

struct CheckRow: View {
    let check: Check

    var body: some View {
        Label {
            Text(check.text).textSelection(.enabled)
        } icon: {
            switch check.level {
            case "ok": Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
            case "warn", "warning": Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
            case "fail", "error": Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
            default: Image(systemName: "info.circle").foregroundStyle(.secondary)
            }
        }
    }
}

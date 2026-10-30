import Foundation

/// `Mac Utils --self-test <payload> [--components a,b] [--events FILE]` checks a payload and prints what
/// the app would show and run, without opening a window. --components ticks those components (default:
/// every one not installed); --events feeds a saved `install.sh --json` output through the install screen's
/// parser. Exits 1 if anything is wrong.
@MainActor
enum SelfTest {
    static func run(_ args: [String]) -> Int32 {
        guard let first = args.first, !first.hasPrefix("--") else {
            print("usage: Mac Utils --self-test <payload dir> [--components a,b] [--events FILE]")
            return 2
        }
        func option(_ name: String) -> String? {
            args.firstIndex(of: name).flatMap { $0 + 1 < args.count ? args[$0 + 1] : nil }
        }
        let payload = URL(fileURLWithPath: first).standardizedFileURL
        let uv = payload.deletingLastPathComponent().appendingPathComponent("uv")
        let catalog: Catalog
        do {
            catalog = try Catalog.load(payload: payload)
        } catch {
            print("FAIL  can't read components.json or models.lock.json in \(payload.path): \(error)")
            return 1
        }
        var problems = catalog.problems()
        let record = Record.load()
        let installed = Set((record ?? [:]).keys)

        print("payload  \(payload.path)")
        print("record   \(Paths.record) (\(record.map { "\($0.count) installed" } ?? "none"))")
        print("install  \(Paths.installDir)")

        print("\nComponents")
        for group in catalog.groups {
            print("  \(group.title) [\(group.id)]")
            for c in catalog.components(in: group.id) {
                let state = record?[c.id].map { "installed \($0)" } ?? "not installed"
                print("    \(c.id.padding(toLength: 17, withPad: " ", startingAt: 0)) \(c.title)  (\(state))")
                var facts: [String] = []
                if !c.permissions.isEmpty { facts.append("permissions: " + c.permissions.map(Permission.tag).joined(separator: ", ")) }
                if !c.models.isEmpty { facts.append("models: " + c.models.joined(separator: ", ")) }
                if !c.setup.isEmpty { facts.append("setup: " + c.setup.joined(separator: ", ")) }
                if !c.apps.isEmpty { facts.append("apps: " + c.apps.joined(separator: ", ")) }
                if !facts.isEmpty { print("        " + facts.joined(separator: "; ")) }
            }
        }

        print("\nRequires (closure)")
        for c in catalog.components where !c.requires.isEmpty {
            print("  \(c.id) -> \(catalog.requirements(of: c.id).joined(separator: ", "))")
        }

        var selection = Selection()
        let ticks = option("--components")?.split(separator: ",").map(String.init)
            ?? catalog.components.map(\.id).filter { !installed.contains($0) }
        for id in ticks {
            if catalog.component(id) == nil { problems.append("--components: unknown component \(id)") }
            selection.set(id, true, catalog: catalog, installed: installed)
        }
        let planned = catalog.ordered(selection.ids)
        let download = catalog.download(selected: selection.ids, installed: installed)
        print("\nTicked   \(ticks.joined(separator: ", "))")
        print("Planned  \(planned.joined(separator: ", "))")
        print("Models   \(download.models.isEmpty ? "none" : download.models.joined(separator: ", ")) (about \(formatBytes(download.bytes)))")
        print("Command  \(shellLine(installCommand(payload: payload, uv: uv, ids: planned)))")

        print("\nPayload")
        let files = FileManager.default
        for (path, needed) in [("install.sh", true), ("manage.py", true), ("pyproject.toml", true), ("uv.lock", true),
                               ("bin/ocr-helper", true), ("models", false), (".venv", false), (".git", false),
                               ("brokers/registry", false), ("dataflow/extension/icons", false)] {
            let present = files.fileExists(atPath: payload.appendingPathComponent(path).path)
            print("  \(present == needed ? "ok  " : "FAIL") \(path) \(present ? "present" : "absent")")
            if present != needed { problems.append("payload: \(path) should be \(needed ? "present" : "absent")") }
        }
        let helper = payload.appendingPathComponent("bin/ocr-helper").path
        if files.fileExists(atPath: helper) && !files.isExecutableFile(atPath: helper) {
            problems.append("payload: bin/ocr-helper isn't executable")
        }
        let uvOK = files.isExecutableFile(atPath: uv.path)
        print("  \(uvOK ? "ok  " : "FAIL") ../uv \(uvOK ? "present" : "missing")")
        if !uvOK { problems.append("uv is missing next to the payload") }

        if let events = option("--events") {
            print("\nEvents from \(events)")
            let install = InstallModel()
            install.start(["/bin/cat", events])
            while install.running { RunLoop.main.run(until: Date(timeIntervalSinceNow: 0.05)) }
            for step in install.steps {
                let progress = step.total > 0 ? " \(step.done)/\(step.total)" : ""
                let status = step.needsYou ? "needs you" : step.status
                print("  \(status.padding(toLength: 9, withPad: " ", startingAt: 0)) \(step.id): \(step.title)\(progress)\(step.detail.isEmpty ? "" : " — \(step.detail)")")
            }
            for note in install.notes { print("  note: \(note)") }
            print("  outcome: \(install.outcome.map { "\($0.ok ? "ok" : "failed"): \($0.message)" } ?? "none")")
        }

        print(problems.isEmpty ? "\nall clear" : "\n" + problems.map { "FAIL  \($0)" }.joined(separator: "\n"))
        return problems.isEmpty ? 0 : 1
    }
}

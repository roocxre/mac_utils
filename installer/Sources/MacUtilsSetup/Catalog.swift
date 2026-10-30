import Foundation

/// Where things live. HOME comes from the environment first, as in Python's expanduser, so a sandbox
/// home (HOME=/some/folder) is honoured the same way the engine honours it.
enum Paths {
    static let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
    static let support = home + "/Library/Application Support/MacUtils"
    static let state = support + "/state"
    static let installDir = support + "/app"
    static let record = state + "/components.json"
    static let python = installDir + "/.venv/bin/python"
    static let manage = installDir + "/manage.py"
    static let extensionDir = installDir + "/dataflow/extension"
    static let resources = Bundle.main.resourceURL ?? URL(fileURLWithPath: ".")
    static let payload = resources.appendingPathComponent("payload")
    static let uv = resources.appendingPathComponent("uv")
}

struct ComponentGroup: Decodable, Identifiable {
    let id: String
    let title: String
}

/// One checkbox in the installer, as described in components.json.
struct Component: Decodable, Identifiable {
    let id: String
    let group: String
    let title: String
    let summary: String
    let agents: [String]
    let models: [String]
    let requires: [String]
    let permissions: [String]
    let setup: [String]
    let apps: [String]

    enum CodingKeys: String, CodingKey {
        case id, group, title, summary, agents, models, requires, permissions, setup, apps
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        func list(_ key: CodingKeys) throws -> [String] { try c.decodeIfPresent([String].self, forKey: key) ?? [] }
        id = try c.decode(String.self, forKey: .id)
        group = try c.decode(String.self, forKey: .group)
        title = try c.decode(String.self, forKey: .title)
        summary = try c.decodeIfPresent(String.self, forKey: .summary) ?? ""
        agents = try list(.agents)
        models = try list(.models)
        requires = try list(.requires)
        permissions = try list(.permissions)
        setup = try list(.setup)
        apps = try list(.apps)
    }
}

/// components.json and models.lock.json from the payload.
struct Catalog {
    let groups: [ComponentGroup]
    let components: [Component]
    let modelSizes: [String: Int64]   // model name -> bytes of all its files

    static func load(payload: URL) throws -> Catalog {
        struct ComponentsFile: Decodable { let groups: [ComponentGroup]; let components: [Component] }
        struct LockFile: Decodable { let models: [String: Model] }
        struct Model: Decodable { let files: [File] }
        struct File: Decodable { let size: Int64? }
        let decoder = JSONDecoder()
        let file = try decoder.decode(ComponentsFile.self,
                                      from: Data(contentsOf: payload.appendingPathComponent("components.json")))
        let lock = try decoder.decode(LockFile.self,
                                      from: Data(contentsOf: payload.appendingPathComponent("models.lock.json")))
        let sizes = lock.models.mapValues { $0.files.reduce(Int64(0)) { $0 + ($1.size ?? 0) } }
        return Catalog(groups: file.groups, components: file.components, modelSizes: sizes)
    }

    func component(_ id: String) -> Component? { components.first { $0.id == id } }

    func components(in group: String) -> [Component] { components.filter { $0.group == group } }

    func title(_ id: String) -> String { component(id)?.title ?? id }

    /// Ids in the order components.json lists them.
    func ordered(_ ids: Set<String>) -> [String] { components.map(\.id).filter(ids.contains) }

    /// Everything a component requires, directly or through another component.
    func requirements(of id: String) -> [String] {
        var found = Set<String>()
        var queue = component(id)?.requires ?? []
        while let next = queue.popLast() {
            if next != id, found.insert(next).inserted {
                queue += component(next)?.requires ?? []
            }
        }
        return ordered(found)
    }

    /// Models the selected components need that the installed ones haven't already brought, and their size.
    func download(selected: Set<String>, installed: Set<String>) -> (models: [String], bytes: Int64) {
        let have = Set(installed.flatMap { component($0)?.models ?? [] })
        let need = Set(selected.flatMap { component($0)?.models ?? [] }).subtracting(have)
        return (need.sorted(), need.reduce(Int64(0)) { $0 + (modelSizes[$1] ?? 0) })
    }

    /// Ids, groups, requirements and models that don't resolve, for the self-test.
    func problems() -> [String] {
        let groupIDs = Set(groups.map(\.id))
        var found: [String] = []
        for c in components {
            if !groupIDs.contains(c.group) { found.append("\(c.id): unknown group \(c.group)") }
            for r in c.requires where component(r) == nil { found.append("\(c.id): requires unknown component \(r)") }
            for m in c.models where modelSizes[m] == nil { found.append("\(c.id): model \(m) is not in models.lock.json") }
            for p in c.permissions where Permission.kind(of: p) == nil { found.append("\(c.id): unknown permission \(p)") }
        }
        let ids = components.map(\.id)
        if Set(ids).count != ids.count { found.append("duplicate component ids") }
        return found
    }
}

/// The install record manage.py writes: {"version": …, "installed": {"<id>": {"version": …}}}.
enum Record {
    /// Installed component id -> version, or nil when there is no record.
    static func load(path: String = Paths.record) -> [String: String]? {
        guard let data = FileManager.default.contents(atPath: path),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return nil }
        let entries = json["installed"] as? [String: Any] ?? [:]
        return entries.mapValues { ($0 as? [String: Any])?["version"] as? String ?? "" }
    }
}

/// The components ticked for this run. Ticking one also ticks what it requires (unless that is
/// installed already); unticking one unticks what can't be installed without it.
struct Selection {
    var ids: Set<String> = []

    mutating func set(_ id: String, _ on: Bool, catalog: Catalog, installed: Set<String>) {
        if on {
            ids.insert(id)
            ids.formUnion(catalog.requirements(of: id).filter { !installed.contains($0) })
        } else {
            ids.remove(id)
            if !installed.contains(id) {
                ids = ids.filter { !catalog.requirements(of: $0).contains(id) }
            }
        }
    }
}

/// The macOS privacy permissions components ask for, and the System Settings pane for each.
enum Permission: String, CaseIterable, Identifiable {
    case accessibility, screenRecording, automation, files

    var id: String { rawValue }

    /// components.json permission id -> kind. The three folder permissions share one pane.
    static func kind(of id: String) -> Permission? {
        switch id {
        case "accessibility": return .accessibility
        case "screen-recording": return .screenRecording
        case "automation": return .automation
        case "desktop-folder", "documents-folder", "downloads-folder": return .files
        default: return nil
        }
    }

    /// The short tag shown on a component.
    static func tag(_ id: String) -> String {
        switch id {
        case "desktop-folder": return "Desktop folder"
        case "documents-folder": return "Documents folder"
        case "downloads-folder": return "Downloads folder"
        default: return kind(of: id)?.title ?? id
        }
    }

    var title: String {
        switch self {
        case .accessibility: return "Accessibility"
        case .screenRecording: return "Screen Recording"
        case .automation: return "Automation"
        case .files: return "Files and Folders"
        }
    }

    var why: String {
        switch self {
        case .accessibility: return "Lets agents listen for their keyboard shortcuts and read the address bar of the browser in front."
        case .screenRecording: return "Lets the screen-text tool see the region you select so it can read the text in it."
        case .automation: return "Lets agents ask other apps to act, such as pausing Spotify or Music, or reporting the browser's current page."
        case .files: return "Lets agents look in Desktop, Documents and Downloads to tidy them and pick up new screenshots and downloads."
        }
    }

    var settingsURL: URL {
        let pane = ["accessibility": "Privacy_Accessibility", "screenRecording": "Privacy_ScreenCapture",
                    "automation": "Privacy_Automation", "files": "Privacy_FilesAndFolders"][rawValue]!
        return URL(string: "x-apple.systempreferences:com.apple.preference.security?\(pane)")!
    }

    var symbol: String {
        switch self {
        case .accessibility: return "accessibility"
        case .screenRecording: return "rectangle.dashed.badge.record"
        case .automation: return "gearshape.2"
        case .files: return "folder"
        }
    }
}

/// After `manage.py uninstall-all`: deletes what it leaves, the folder it runs from (the code, its
/// virtualenv and models) and the Python, uv and cache the installer put beside it. Keeps the state
/// folder if asked. Returns a line per folder.
func deleteInstall(keepState: Bool) -> [String] {
    let files = FileManager.default
    var lines: [String] = []
    for name in (try? files.contentsOfDirectory(atPath: Paths.support)) ?? [] where !(keepState && name == "state") {
        let path = Paths.support + "/" + name
        do {
            try files.removeItem(atPath: path)
            lines.append("Deleted \(path)")
        } catch {
            lines.append("Couldn't delete \(path): \(error.localizedDescription)")
        }
    }
    if !keepState { try? files.removeItem(atPath: Paths.support) }
    return lines
}

/// The command that installs the given components from the payload.
func installCommand(payload: URL, uv: URL, ids: [String]) -> [String] {
    ["/bin/bash", payload.appendingPathComponent("install.sh").path,
     "--payload", payload.path, "--uv", uv.path, "--json", "--yes", "--components", ids.joined(separator: ",")]
}

/// A command as one line you could paste into Terminal.
func shellLine(_ command: [String]) -> String {
    command.map { arg in
        arg.allSatisfy({ $0.isLetter || $0.isNumber || "-_./,=:@".contains($0) }) && !arg.isEmpty
            ? arg : "'" + arg.replacingOccurrences(of: "'", with: "'\\''") + "'"
    }.joined(separator: " ")
}

func formatBytes(_ bytes: Int64) -> String {
    ByteCountFormatter.string(fromByteCount: bytes, countStyle: .file)
}

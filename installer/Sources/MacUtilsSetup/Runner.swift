import Combine
import Foundation

/// Runs a command and hands each line of its output to the main thread, then its exit status.
final class Runner {
    private var process: Process?
    private(set) var cancelled = false

    func start(_ command: [String], cwd: String? = nil,
               line: @escaping @MainActor (String, Bool) -> Void,   // (text, came from stderr)
               exit: @escaping @MainActor (Int32) -> Void) throws {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: command[0])
        p.arguments = Array(command.dropFirst())
        if let cwd { p.currentDirectoryURL = URL(fileURLWithPath: cwd) }
        // Python buffers a pipe; unbuffered, each JSON event arrives as soon as it's printed.
        p.environment = ProcessInfo.processInfo.environment.merging(["PYTHONUNBUFFERED": "1"]) { $1 }
        p.standardInput = FileHandle.nullDevice
        let out = Pipe(), err = Pipe()
        p.standardOutput = out
        p.standardError = err
        try p.run()
        process = p
        cancelled = false

        let reading = DispatchGroup()
        for (pipe, isErr) in [(out, false), (err, true)] {
            reading.enter()
            DispatchQueue.global().async {
                Runner.readLines(pipe.fileHandleForReading) { text in
                    DispatchQueue.main.async { MainActor.assumeIsolated { line(text, isErr) } }
                }
                reading.leave()
            }
        }
        DispatchQueue.global().async {
            p.waitUntilExit()
            // Something the command started may keep the pipe open; don't wait for it for long.
            _ = reading.wait(timeout: .now() + 2)
            let status = p.terminationStatus
            DispatchQueue.main.async { MainActor.assumeIsolated { exit(status) } }
        }
    }

    /// Stops the command and everything it started (install.sh runs uv and Python under it).
    func cancel() {
        guard let p = process, p.isRunning else { return }
        cancelled = true
        let children = Runner.descendants(of: p.processIdentifier)
        p.terminate()
        for pid in children { kill(pid, SIGTERM) }
    }

    private static func readLines(_ handle: FileHandle, _ each: (String) -> Void) {
        var buffer = Data()
        while true {
            let chunk = handle.availableData   // empty at end of file
            if chunk.isEmpty { break }
            buffer.append(chunk)
            while let newline = buffer.firstIndex(of: 10) {
                each(String(decoding: buffer[buffer.startIndex..<newline], as: UTF8.self))
                buffer.removeSubrange(buffer.startIndex...newline)
            }
        }
        if !buffer.isEmpty { each(String(decoding: buffer, as: UTF8.self)) }
    }

    /// Every process below pid, found with pgrep -P one level at a time.
    static func descendants(of pid: Int32) -> [Int32] {
        var found: [Int32] = []
        var queue = [pid]
        while let parent = queue.popLast() {
            let pgrep = Process()
            pgrep.executableURL = URL(fileURLWithPath: "/usr/bin/pgrep")
            pgrep.arguments = ["-P", String(parent)]
            let pipe = Pipe()
            pgrep.standardOutput = pipe
            guard (try? pgrep.run()) != nil else { continue }
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            pgrep.waitUntilExit()
            let kids = String(decoding: data, as: UTF8.self).split(separator: "\n").compactMap { Int32($0) }
            found += kids
            queue += kids
        }
        return found
    }
}

/// A step of the install, built from install.sh --json events.
struct Step: Identifiable {
    let id: String
    var title: String
    var status: String          // start, done, skipped, failed, stopped
    var detail: String = ""
    var needsYou = false        // a skipped step that is the user's to do (the Setup screen guides it)
    var done = 0
    var total = 0
}

/// One line of install.sh --json output.
struct InstallEvent: Decodable {
    let event: String
    let id: String?
    let title: String?
    let status: String?
    let detail: String?
    let done: Int?
    let total: Int?
    let ok: Bool?
    let message: String?
    let text: String?           // a note
    let needsYou: Bool?

    enum CodingKeys: String, CodingKey {
        case event, id, title, status, detail, done, total, ok, message, text
        case needsYou = "needs_you"
    }
}

/// Runs install.sh and keeps its steps, its raw log and how it ended.
@MainActor
final class InstallModel: ObservableObject {
    @Published var steps: [Step] = []
    @Published var log: [String] = []
    @Published var notes: [String] = []
    @Published var running = false
    @Published var outcome: (ok: Bool, message: String)?
    var onFinish: ((Bool) -> Void)?
    private let runner = Runner()
    private var finishedMessage: (ok: Bool, message: String)?

    func start(_ command: [String]) {
        steps = []
        notes = []
        log = ["$ " + shellLine(command)]
        outcome = nil
        finishedMessage = nil
        do {
            try runner.start(command,
                             line: { [weak self] text, isErr in self?.receive(text, fromStderr: isErr) },
                             exit: { [weak self] status in self?.ended(status) })
            running = true
        } catch {
            outcome = (false, "Couldn't start the installer: \(error.localizedDescription)")
        }
    }

    func cancel() { runner.cancel() }

    func receive(_ text: String, fromStderr: Bool) {
        log.append(text)
        if log.count > 5000 { log.removeFirst(log.count - 5000) }
        guard !fromStderr, text.hasPrefix("{"),
              let event = try? JSONDecoder().decode(InstallEvent.self, from: Data(text.utf8)) else { return }
        switch event.event {
        case "step", "progress":
            guard let id = event.id else { return }
            let i = steps.firstIndex { $0.id == id } ?? {
                steps.append(Step(id: id, title: event.title ?? id, status: "start"))
                return steps.count - 1
            }()
            if let title = event.title { steps[i].title = title }
            if let status = event.status { steps[i].status = status }
            if let detail = event.detail { steps[i].detail = detail }
            if let done = event.done { steps[i].done = done }
            if let total = event.total { steps[i].total = total }
            if let needsYou = event.needsYou { steps[i].needsYou = needsYou }
        case "note":
            if let text = event.text { notes.append(text) }
        case "finished":
            finishedMessage = (event.ok ?? false, event.message ?? "")
        default:
            break
        }
    }

    /// Success needs exit status 0 and no finished event saying otherwise; the event supplies the words.
    private func ended(_ status: Int32) {
        running = false
        for i in steps.indices where steps[i].status == "start" { steps[i].status = "stopped" }
        let words = finishedMessage?.message ?? ""
        if runner.cancelled {
            outcome = (false, "Cancelled. Run the install again to finish it.")
        } else if status == 0 && finishedMessage?.ok != false {
            outcome = (true, words.isEmpty ? "Installed." : words)
        } else {
            outcome = (false, (words.isEmpty ? "The installer stopped" : words) + " (exit code \(status)).")
        }
        onFinish?(outcome?.ok ?? false)
    }
}

/// Runs one manage.py or helper command at a time and keeps its output.
@MainActor
final class CommandModel: ObservableObject {
    @Published var output: [String] = []
    @Published var running = false
    @Published var status: Int32?
    private(set) var stdout: [String] = []
    private let runner = Runner()

    /// Runs the installed Python with these arguments in the install folder.
    func python(_ args: [String], then done: ((Int32) -> Void)? = nil) {
        run([Paths.python] + args, cwd: Paths.installDir, then: done)
    }

    func run(_ command: [String], cwd: String? = nil, then done: ((Int32) -> Void)? = nil) {
        guard !running else { return }
        output = ["$ " + shellLine(command)]
        stdout = []
        status = nil
        do {
            try runner.start(command, cwd: cwd,
                             line: { [weak self] text, isErr in
                                 self?.output.append(text)
                                 if !isErr { self?.stdout.append(text) }
                             },
                             exit: { [weak self] code in
                                 self?.running = false
                                 self?.status = code
                                 done?(code)
                             })
            running = true
        } catch {
            output.append("Couldn't run it: \(error.localizedDescription)")
            status = -1
        }
    }

    func cancel() { runner.cancel() }
}

/// One line of `manage.py doctor --json`.
struct Check: Identifiable {
    let id = UUID()
    let level: String   // ok, warn, fail
    let text: String

    /// Accepts {"checks": [...]} as manage.py prints it, a bare list, or one JSON object per line.
    /// Each check has a level (ok, warn, fail, info) and a text.
    static func parse(_ text: String) -> [Check] {
        var items: [[String: Any]] = []
        if let json = try? JSONSerialization.jsonObject(with: Data(text.utf8)) {
            if let list = json as? [[String: Any]] {
                items = list
            } else if let object = json as? [String: Any] {
                items = object["checks"] as? [[String: Any]] ?? [object]
            }
        } else {
            items = text.split(separator: "\n").compactMap {
                try? JSONSerialization.jsonObject(with: Data($0.utf8)) as? [String: Any]
            }
        }
        return items.map { item in
            let level = (item["level"] ?? item["status"]) as? String ?? "info"
            let words = (item["text"] ?? item["message"]) as? String ?? "\(item)"
            return Check(level: level.lowercased(), text: words)
        }
    }
}

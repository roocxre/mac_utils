import SwiftUI

struct InstallView: View {
    @EnvironmentObject var model: AppModel
    @ObservedObject var install: InstallModel
    @State private var showLog = false

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            if install.steps.isEmpty && !install.running && install.outcome == nil {
                ContentUnavailableView("Nothing is installing", systemImage: "arrow.down.circle",
                                       description: Text("Tick components on the Components screen and click Install."))
            } else {
                List {
                    ForEach(install.steps) { step in StepRow(step: step) }
                    ForEach(install.notes.indices, id: \.self) { i in
                        Label(install.notes[i], systemImage: "info.circle").font(.callout).foregroundStyle(.secondary)
                            .textSelection(.enabled)
                    }
                }
                .listStyle(.inset)
                if let outcome = install.outcome {
                    Label(outcome.message, systemImage: outcome.ok ? "checkmark.seal.fill" : "exclamationmark.triangle.fill")
                        .foregroundStyle(outcome.ok ? .green : .orange)
                        .textSelection(.enabled)
                }
                DisclosureGroup("Log", isExpanded: $showLog) {
                    LogView(lines: install.log).frame(minHeight: 160, maxHeight: 260)
                }
                HStack {
                    Spacer()
                    if install.running {
                        Button("Cancel", role: .cancel) { install.cancel() }
                    } else if install.outcome?.ok == true {
                        Button("Continue to Setup") { model.screen = .setup }.keyboardShortcut(.defaultAction)
                    } else if install.outcome != nil {
                        Button("Back to Components") { model.screen = .components }
                    }
                }
            }
        }
        .padding()
        .navigationTitle("Install")
    }
}

struct StepRow: View {
    let step: Step

    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            icon.frame(width: 18)
            VStack(alignment: .leading, spacing: 3) {
                Text(step.title + (step.needsYou ? " (yours to do on the Setup screen)" : ""))
                if step.status == "start" && step.total > 0 {
                    ProgressView(value: Double(min(step.done, step.total)), total: Double(step.total))
                }
                if !step.detail.isEmpty {
                    Text(step.detail).font(.caption).foregroundStyle(.secondary).textSelection(.enabled)
                }
            }
        }
        .padding(.vertical, 2)
    }

    @ViewBuilder private var icon: some View {
        switch step.needsYou ? "needs-you" : step.status {
        case "needs-you": Image(systemName: "hand.raised.fill").foregroundStyle(.orange)
        case "start": ProgressView().controlSize(.small)
        case "done": Image(systemName: "checkmark.circle.fill").foregroundStyle(.green)
        case "skipped": Image(systemName: "minus.circle").foregroundStyle(.secondary)
        case "failed": Image(systemName: "xmark.octagon.fill").foregroundStyle(.red)
        default: Image(systemName: "pause.circle").foregroundStyle(.secondary)
        }
    }
}

import AppKit
import SwiftUI
import QueueModel

@main
struct MergeQueueMenuApp: App {
    @NSApplicationDelegateAdaptor(MenuAppDelegate.self) private var delegate
    var body: some Scene {
        MenuBarExtra("Merge Queue", systemImage: "point.topleft.down.to.point.bottomright.curvepath") {
            LiveQueuePanel()
        }
        .menuBarExtraStyle(.window)
    }
}

struct LiveQueuePanel: View {
    @StateObject private var store: QueueStore
    @AppStorage("theme") private var themeRaw = QueueThemeID.orbital.rawValue

    init() {
        let arguments = ProcessInfo.processInfo.arguments
        let scenario = arguments.firstIndex(of: "--preview-scenario").flatMap { index in
            index + 1 < arguments.count ? arguments[index + 1] : nil
        }
        _store = StateObject(wrappedValue: QueueStore(preview: scenario.map(PreviewData.snapshot)))
    }
    var body: some View {
        QueuePanel(snapshot: store.snapshot, theme: QueueThemeID(rawValue: themeRaw) ?? .orbital,
                   onSelectRepository: store.select, onSelectTheme: { themeRaw = $0.rawValue },
                   onRefresh: store.refresh, onQuit: { NSApplication.shared.terminate(nil) })
            .onAppear { store.start() }
            .onDisappear { store.stop() }
    }
}

/// Preview windows exist only behind an explicit development flag; normal launch is menu-bar only.
final class MenuAppDelegate: NSObject, NSApplicationDelegate {
    private var previewWindow: NSWindow?
    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApplication.shared.setActivationPolicy(.accessory)
        let args = ProcessInfo.processInfo.arguments
        if let index = args.firstIndex(of: "--render-previews"), index + 1 < args.count {
            renderPreviews(to: URL(fileURLWithPath: args[index + 1]))
            return
        }
        if args.contains("--inspect-window") {
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 390, height: 500),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Merge Queue — Inspector"
            window.contentView = NSHostingView(rootView: LiveQueuePanel())
            window.center(); window.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate(ignoringOtherApps: true)
            previewWindow = window
            return
        }
        if let index = args.firstIndex(of: "--preview-scenario"), index + 1 < args.count {
            let scenario = args[index + 1]
            let theme: QueueThemeID = args.contains("--minimal") ? .minimal : .orbital
            let root = QueuePanel(snapshot: PreviewData.snapshot(scenario), theme: theme,
                onSelectRepository: { _ in }, onSelectTheme: { _ in }, onRefresh: {},
                onQuit: { NSApplication.shared.terminate(nil) })
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 390, height: 540),
                                  styleMask: [.titled, .closable], backing: .buffered, defer: false)
            window.title = "Merge Queue Preview — \(scenario)"
            window.contentView = NSHostingView(rootView: root)
            window.center(); window.makeKeyAndOrderFront(nil)
            NSApplication.shared.activate(ignoringOtherApps: true)
            previewWindow = window
        }
    }

    private func renderPreviews(to directory: URL) {
        do { try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true) }
        catch { fputs("Cannot create preview directory: \(error)\n", stderr); exit(1) }
        var windows: [(String, NSWindow, NSView)] = []
        for scenario in ["active", "idle", "queued", "failed", "multiple", "long", "empty", "error", "minimal"] {
            let view = NSHostingView(rootView: QueuePanel(
                snapshot: PreviewData.snapshot(scenario == "minimal" ? "active" : scenario),
                theme: scenario == "minimal" ? .minimal : .orbital, onSelectRepository: { _ in },
                onSelectTheme: { _ in }, onRefresh: {}, onQuit: {}))
            let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 390, height: 500),
                                  styleMask: [.borderless], backing: .buffered, defer: false)
            window.contentView = view
            view.frame = NSRect(x: 0, y: 0, width: 390, height: 500)
            view.layoutSubtreeIfNeeded()
            windows.append((scenario, window, view))
        }
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) {
            for (name, _, view) in windows {
                guard let bitmap = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { exit(1) }
                view.cacheDisplay(in: view.bounds, to: bitmap)
                guard let png = bitmap.representation(using: .png, properties: [:]) else { exit(1) }
                do { try png.write(to: directory.appendingPathComponent(name + ".png")) }
                catch { fputs("Cannot write preview: \(error)\n", stderr); exit(1) }
            }
            NSApplication.shared.terminate(nil)
        }
    }
}

enum PreviewData {
    static func snapshot(_ scenario: String) -> DashboardSnapshot {
        let repo = RepositoryRecord(id: "012345abcdef", commonDirectory: "/tmp/observatory/.git",
            checkout: "/tmp/observatory", stateDirectory: "/tmp/merge-queue/012345abcdef", target: "refs/heads/main")
        let second = RepositoryRecord(id: "abcdef012345", commonDirectory: "/tmp/moonbase/.git",
            checkout: "/tmp/moonbase", stateDirectory: "/tmp/merge-queue/abcdef012345", target: "refs/heads/main")
        let now = Date()
        var requests: [RequestRecord] = []
        if ["active", "long", "multiple"].contains(scenario) {
            requests.append(RequestRecord(id: "mq-synthetic-active", state: "running", phase: "validating",
                sourceBranch: scenario == "long" ? "codex/a-very-long-request-name-that-should-truncate-without-moving-the-repository-picker" : "codex/native-menu-bar",
                sequence: 1, submittedAt: now.addingTimeInterval(-155), startedAt: now.addingTimeInterval(-92)))
            requests.append(RequestRecord(id: "mq-synthetic-next", state: "queued", sourceBranch: "codex/smoother-orbits", sequence: 2, submittedAt: now))
        }
        if scenario == "multiple" {
            requests.append(RequestRecord(id: "mq-synthetic-inline", state: "running", phase: "publishing",
                sourceBranch: "codex/quick-copy-fix", sequence: 3, startedAt: now.addingTimeInterval(-15), lane: "inline"))
        }
        if scenario == "queued" {
            requests.append(RequestRecord(id: "mq-synthetic-waiting", state: "queued",
                sourceBranch: "codex/ready-for-launch", sequence: 1, submittedAt: now.addingTimeInterval(-63)))
        }
        if scenario == "failed" {
            requests.append(RequestRecord(id: "mq-synthetic-failed", state: "validation_failed", sourceBranch: "codex/orbit-controls",
                sequence: 1, submittedAt: now.addingTimeInterval(-200), finishedAt: now.addingTimeInterval(-45),
                failureMessage: "Validation needs attention."))
        }
        return DashboardSnapshot(repositories: scenario == "empty" ? [] : [repo, second],
            selectedRepositoryID: scenario == "empty" ? nil : repo.id, requests: requests,
            warning: scenario == "error" ? "Queue data unavailable; showing the last readable snapshot." : nil)
    }
}

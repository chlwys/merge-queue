import Foundation
import Combine
import CoreServices
import QueueModel

/// Observes existing durable state. Never starts a runner, invokes Git, or writes queue files.
@MainActor
final class QueueStore: ObservableObject {
    @Published private(set) var snapshot = DashboardSnapshot(isLoading: true)
    private let reader: SnapshotReader
    private let work = DispatchQueue(label: "merge-queue.snapshot", qos: .utility)
    private var watcher: QueueWatcher?
    private var pending: DispatchWorkItem?
    private var generation = 0
    private var visible = false
    private var selection: String?
    private var watchKey = ""
    private var recentFailures: [String: [String: RequestRecord]] = [:]
    private var failureFirstSeen: [String: [String: Date]] = [:]
    private let defaults: UserDefaults
    private let preview: DashboardSnapshot?

    init(root: URL = SnapshotReader.defaultRoot(), defaults: UserDefaults = .standard, preview: DashboardSnapshot? = nil) {
        reader = SnapshotReader(root: root)
        self.defaults = defaults
        self.preview = preview
        selection = defaults.string(forKey: "selectedRepositoryID")
        if let preview { snapshot = preview; selection = preview.selectedRepositoryID }
    }
    func start() {
        visible = true
        guard preview == nil else { return }
        updateWatcher()
        refresh()
    }
    func stop() {
        visible = false
        pending?.cancel(); pending = nil
        watcher?.stop(); watcher = nil; watchKey = ""
    }
    func select(_ id: String) {
        selection = id
        defaults.set(id, forKey: "selectedRepositoryID")
        snapshot.selectedRepositoryID = id
        snapshot.requests = []
        refresh()
    }
    private func updateWatcher() {
        guard visible else { return }
        var targets = [reader.root.appendingPathComponent("repos")]
        if let repo = snapshot.selectedRepository {
            let directory = URL(fileURLWithPath: repo.stateDirectory).resolvingSymlinksInPath()
            targets += ["requests", "notices"].map { directory.appendingPathComponent($0) }
        }
        let key = targets.map(\.path).joined(separator: "|")
        guard key != watchKey else { return }
        watcher?.stop()
        watcher = QueueWatcher(targets: targets) { [weak self] in
            Task { @MainActor in self?.scheduleRefresh() }
        }
        if watcher?.start() == true {
            watchKey = key
            scheduleRefresh() // Close the snapshot-to-watch registration race.
        } else {
            watcher = nil
            watchKey = ""
            snapshot.warning = "Live updates unavailable. Use Refresh to read current queue data and retry observation."
        }
    }
    private func scheduleRefresh() {
        guard visible else { return }
        pending?.cancel()
        let item = DispatchWorkItem { [weak self] in self?.refresh() }
        pending = item
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: item)
    }
    private func withFailureDebris(_ input: DashboardSnapshot, at now: Date) -> DashboardSnapshot {
        var result = input
        recentFailures = recentFailures.mapValues { records in
            records.filter { _, request in
                guard let origin = request.debrisOrigin else { return false }
                return now.timeIntervalSince(origin) < DashboardSnapshot.debrisLifetime
            }
        }.filter { !$0.value.isEmpty }
        guard let repository = result.selectedRepositoryID else { return result }
        var records = recentFailures[repository] ?? [:]
        var seen = failureFirstSeen[repository] ?? [:]
        let present = Set(result.requests.filter(\.isFailure).map(\.id))
        for index in result.requests.indices where result.requests[index].isFailure {
            let id = result.requests[index].id
            let first = seen[id] ?? records[id]?.firstObservedAt ?? result.requests[index].firstObservedAt ?? now
            seen[id] = first
            result.requests[index].firstObservedAt = first
            let request = result.requests[index]
            if let origin = request.debrisOrigin, now.timeIntervalSince(origin) < DashboardSnapshot.debrisLifetime {
                records[id] = request
            }
        }
        failureFirstSeen[repository] = seen.filter { present.contains($0.key) || records[$0.key] != nil }
        recentFailures[repository] = records
        result.transientFailures = Array(records.values)
        return result
    }

    func refresh() {
        guard preview == nil else { return }
        generation += 1
        let expected = generation, selected = selection, previous = snapshot, reader = reader
        snapshot.isLoading = true
        work.async { [weak self] in
            let result = reader.read(selectedID: selected, previous: previous)
            DispatchQueue.main.async {
                guard let self, self.generation == expected else { return }
                self.snapshot = self.withFailureDebris(result, at: Date())
                self.selection = result.selectedRepositoryID
                self.updateWatcher()
            }
        }
    }
}

/// Native, coalesced filesystem notifications. A missing state root is observed from its nearest
/// existing ancestor; events are filtered to the configured state tree.
private final class QueueWatcher {
    let targets: [URL]
    let onChange: () -> Void
    private var stream: FSEventStreamRef?
    init(targets: [URL], onChange: @escaping () -> Void) { self.targets = targets; self.onChange = onChange }
    func start() -> Bool {
        stop()
        let directories = Set(targets.map { target -> String in
            var directory = target
            while !FileManager.default.fileExists(atPath: directory.path), directory.path != "/" {
                directory.deleteLastPathComponent()
            }
            return directory.path
        })
        var context = FSEventStreamContext(version: 0, info: Unmanaged.passUnretained(self).toOpaque(), retain: nil, release: nil, copyDescription: nil)
        let callback: FSEventStreamCallback = { _, info, count, paths, _, _ in
            guard let info else { return }
            let owner = Unmanaged<QueueWatcher>.fromOpaque(info).takeUnretainedValue()
            let changed = unsafeBitCast(paths, to: NSArray.self) as? [String] ?? []
            if changed.prefix(count).contains(where: { path in
                owner.targets.contains { target in
                    let root = target.path
                    let relevant = path == root || path.hasPrefix(root + "/") || root.hasPrefix(path + "/")
                    let ext = URL(fileURLWithPath: path).pathExtension
                    return relevant && (ext == "json" || ext.isEmpty)
                }
            }) { owner.onChange() }
        }
        stream = FSEventStreamCreate(nil, callback, &context, Array(directories) as CFArray,
                                     FSEventStreamEventId(kFSEventStreamEventIdSinceNow), 0.15,
                                     FSEventStreamCreateFlags(kFSEventStreamCreateFlagUseCFTypes | kFSEventStreamCreateFlagFileEvents | kFSEventStreamCreateFlagWatchRoot))
        guard let stream else { return false }
        FSEventStreamSetDispatchQueue(stream, DispatchQueue.main)
        guard FSEventStreamStart(stream) else { stop(); return false }
        return true
    }
    func stop() {
        guard let stream else { return }
        FSEventStreamStop(stream); FSEventStreamInvalidate(stream); FSEventStreamRelease(stream)
        self.stream = nil
    }
    deinit { stop() }
}

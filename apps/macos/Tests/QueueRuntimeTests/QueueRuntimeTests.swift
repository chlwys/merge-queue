import Foundation
import Testing
import QueueModel
@testable import MergeQueueMenu

@Suite(.serialized)
struct QueueRuntimeTests {
    @Test @MainActor func nativeChangesSelectionPersistenceAndVisibility() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("mq-runtime-\(UUID())")
            .resolvingSymlinksInPath()
        let suite = "local.merge-queue.tests.\(UUID())"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer {
            try? FileManager.default.removeItem(at: root)
            defaults.removePersistentDomain(forName: suite)
        }
        let a = SnapshotReader.repositoryID(commonDirectory: root.appendingPathComponent("alpha/.git").path)
        let b = SnapshotReader.repositoryID(commonDirectory: root.appendingPathComponent("beta/.git").path)
        func write(_ value: [String: Any], to path: String) throws {
            let url = root.appendingPathComponent(path)
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: value).write(to: url, options: .atomic)
        }
        for (id, name) in [(a, "alpha"), (b, "beta")] {
            try write(["repo_id": id, "common_dir": root.appendingPathComponent(name + "/.git").path,
                       "checkout": root.appendingPathComponent(name).path,
                       "state_dir": root.appendingPathComponent(id).path, "target": "refs/heads/main"],
                      to: "repos/\(id).json")
            try FileManager.default.createDirectory(at: root.appendingPathComponent(id + "/requests"), withIntermediateDirectories: true)
        }
        try write(["request_id": "one", "repository": root.appendingPathComponent("alpha/.git").path, "state": "running", "phase": "validating", "seq": 1],
                  to: a + "/requests/one.json")
        let store = QueueStore(root: root, defaults: defaults)
        store.start()
        defer { store.stop() }
        try await until { !store.snapshot.isLoading && store.snapshot.focusedRequest?.phase == "validating" }

        // No explicit refresh: an atomic JSON replacement must reach the store through native file events.
        try write(["request_id": "one", "repository": root.appendingPathComponent("alpha/.git").path, "state": "running", "phase": "publishing", "seq": 1],
                  to: a + "/requests/one.json")
        try await until { store.snapshot.focusedRequest?.phase == "publishing" }

        store.select(b)
        try await until { !store.snapshot.isLoading && store.snapshot.selectedRepositoryID == b }
        #expect(store.snapshot.requests.isEmpty)
        store.stop()
        try write(["request_id": "two", "repository": root.appendingPathComponent("beta/.git").path, "state": "queued", "seq": 2], to: b + "/requests/two.json")
        try await Task.sleep(for: .milliseconds(350))
        #expect(store.snapshot.requests.isEmpty)
        store.start()
        try await until { store.snapshot.waitingRequests.first?.id == "two" }

        let restored = QueueStore(root: root, defaults: defaults)
        restored.start()
        defer { restored.stop() }
        try await until { !restored.snapshot.isLoading && restored.snapshot.selectedRepositoryID == b }
        #expect(restored.snapshot.waitingRequests.first?.id == "two")
    }

    @MainActor private func until(_ predicate: () -> Bool) async throws {
        let deadline = Date().addingTimeInterval(5)
        while !predicate() {
            if Date() >= deadline { throw WaitFailure.timeout }
            try await Task.sleep(for: .milliseconds(20))
        }
    }
    private enum WaitFailure: Error { case timeout }
}

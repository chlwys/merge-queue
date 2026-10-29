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

    @Test @MainActor func acknowledgedFailureRemainsAsLocalDebrisForFiveMinutes() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("mq-failure-runtime-\(UUID())")
            .resolvingSymlinksInPath()
        let suite = "local.merge-queue.tests.\(UUID())"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer {
            try? FileManager.default.removeItem(at: root)
            defaults.removePersistentDomain(forName: suite)
        }

        let common = root.appendingPathComponent("synthetic-repo/.git").path
        let repoID = SnapshotReader.repositoryID(commonDirectory: common)
        let state = root.appendingPathComponent("state")
        func write(_ value: [String: Any], to url: URL) throws {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: value).write(to: url, options: .atomic)
        }
        try write(["repo_id": repoID, "common_dir": common,
                   "checkout": root.appendingPathComponent("synthetic-repo").path,
                   "state_dir": state.path, "target": "refs/heads/main"],
                  to: root.appendingPathComponent("repos/\(repoID).json"))
        let finished = ISO8601DateFormatter().string(from: Date())
        let receipt = state.appendingPathComponent("requests/synthetic-failure.json")
        try write(["request_id": "synthetic-failure", "repository": common,
                   "state": "validation_failed", "seq": 1,
                   "submitted_at": finished,
                   "timings": ["started_at": finished],
                   "result": ["finished_at": finished, "message": "synthetic failure"]], to: receipt)

        let store = QueueStore(root: root, defaults: defaults)
        store.start()
        defer { store.stop() }
        try await until { store.snapshot.requests.first?.isFailure == true }
        let failure = try #require(store.snapshot.requests.first)
        #expect(store.snapshot.debrisRequests(at: Date()).contains { $0.id == failure.id })

        // Simulate an external acknowledgement by removing only this synthetic fixture receipt.
        try FileManager.default.removeItem(at: receipt)
        try await until { store.snapshot.requests.isEmpty }
        #expect(store.snapshot.debrisRequests(at: Date()).contains { $0.id == failure.id })
        #expect(store.snapshot.debrisRequests(at: Date().addingTimeInterval(301)).isEmpty)
        #expect(FileManager.default.fileExists(atPath: root.appendingPathComponent("repos/\(repoID).json").path))
    }

    @Test @MainActor func missingFinishFailureKeepsFirstObservedTimeAcrossRepositorySwitch() async throws {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("mq-first-seen-runtime-\(UUID())")
            .resolvingSymlinksInPath()
        let suite = "local.merge-queue.tests.\(UUID())"
        let defaults = try #require(UserDefaults(suiteName: suite))
        defer {
            try? FileManager.default.removeItem(at: root)
            defaults.removePersistentDomain(forName: suite)
        }

        let commonA = root.appendingPathComponent("synthetic-alpha/.git").path
        let commonB = root.appendingPathComponent("synthetic-beta/.git").path
        let idA = SnapshotReader.repositoryID(commonDirectory: commonA)
        let idB = SnapshotReader.repositoryID(commonDirectory: commonB)
        func write(_ value: [String: Any], to url: URL) throws {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: value).write(to: url, options: .atomic)
        }
        for (id, common, name) in [(idA, commonA, "synthetic-alpha"), (idB, commonB, "synthetic-beta")] {
            let state = root.appendingPathComponent("state-\(id)")
            try write(["repo_id": id, "common_dir": common,
                       "checkout": root.appendingPathComponent(name).path,
                       "state_dir": state.path, "target": "refs/heads/main"],
                      to: root.appendingPathComponent("repos/\(id).json"))
            try FileManager.default.createDirectory(at: state.appendingPathComponent("requests"),
                                                    withIntermediateDirectories: true)
        }
        let submitted = ISO8601DateFormatter().string(from: Date())
        try write(["request_id": "missing-finish", "repository": commonA,
                   "state": "infra_failed", "seq": 1, "submitted_at": submitted,
                   "timings": ["started_at": submitted],
                   "result": ["message": "synthetic failure"]],
                  to: root.appendingPathComponent("state-\(idA)/requests/missing-finish.json"))

        let store = QueueStore(root: root, defaults: defaults)
        store.start()
        defer { store.stop() }
        try await until { !store.snapshot.isLoading && store.snapshot.selectedRepositoryID == idA
            && store.snapshot.debrisRequests(at: Date()).contains { $0.id == "missing-finish" } }
        let firstSeen = try #require(store.snapshot.requests.first { $0.id == "missing-finish" }?.firstObservedAt)

        store.select(idB)
        try await until { !store.snapshot.isLoading && store.snapshot.selectedRepositoryID == idB }
        #expect(store.snapshot.requests.isEmpty)
        store.select(idA)
        try await until { !store.snapshot.isLoading && store.snapshot.selectedRepositoryID == idA
            && store.snapshot.requests.contains { $0.id == "missing-finish" } }

        let restored = try #require(store.snapshot.requests.first { $0.id == "missing-finish" })
        #expect(restored.firstObservedAt == firstSeen)
        #expect(restored.debrisOrigin == firstSeen)
        #expect(!store.snapshot.debrisRequests(at: firstSeen.addingTimeInterval(300))
            .contains { $0.id == "missing-finish" })
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

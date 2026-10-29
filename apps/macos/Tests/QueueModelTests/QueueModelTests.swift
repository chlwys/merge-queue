import Foundation
import Testing
@testable import QueueModel

@Suite
struct QueueModelTests {
    private struct Fixture {
        let root: URL
        let repository: RepositoryRecord

        init(name: String = "repo") throws {
            root = FileManager.default.temporaryDirectory
                .appendingPathComponent("merge-queue-tests-\(UUID().uuidString)", isDirectory: true)
            let common = root.appendingPathComponent(name).appendingPathComponent(".git").path
            repository = RepositoryRecord(
                id: SnapshotReader.repositoryID(commonDirectory: common), commonDirectory: common,
                checkout: root.appendingPathComponent(name).path,
                stateDirectory: root.appendingPathComponent("state").path,
                target: "refs/heads/main")
        }

        func createRepository(stateDirectory: URL? = nil) throws {
            try FileManager.default.createDirectory(at: root.appendingPathComponent("repos"),
                                                   withIntermediateDirectories: true)
            let state = stateDirectory?.path ?? repository.stateDirectory
            try write([
                "repo_id": repository.id,
                "common_dir": repository.commonDirectory,
                "checkout": repository.checkout,
                "state_dir": state,
                "target": repository.target
            ], to: root.appendingPathComponent("repos/\(repository.id).json"))
            try FileManager.default.createDirectory(at: URL(fileURLWithPath: state)
                .appendingPathComponent("requests"), withIntermediateDirectories: true)
        }

        func write(_ object: [String: Any], to url: URL) throws {
            try FileManager.default.createDirectory(at: url.deletingLastPathComponent(),
                                                   withIntermediateDirectories: true)
            try JSONSerialization.data(withJSONObject: object, options: [.sortedKeys])
                .write(to: url, options: .atomic)
        }

        func request(_ id: String, state: String, seq: Int, phase: String? = nil,
                     submitted: String = "2026-01-01T00:00:00Z", started: String? = nil,
                     finished: String? = nil, lane: String? = nil) -> [String: Any] {
            var record: [String: Any] = [
                "request_id": id, "repository": repository.commonDirectory,
                "state": state, "seq": seq, "submitted_at": submitted
            ]
            if let phase { record["phase"] = phase }
            if let lane { record["lane"] = lane }
            var timings: [String: Any] = [:]
            if let started { timings["started_at"] = started }
            if !timings.isEmpty { record["timings"] = timings }
            if let finished { record["result"] = ["finished_at": finished] }
            return record
        }

        var stateURL: URL { URL(fileURLWithPath: repository.stateDirectory) }

        func remove() {
            try? FileManager.default.removeItem(at: root)
        }
    }

    @Test func testSnapshotSortsActiveWorkAndFocusesConcurrentPublisher() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.createRepository()
        try fixture.write(fixture.request("inline", state: "running", seq: 3, phase: "validating", lane: "inline"),
                          to: fixture.stateURL.appendingPathComponent("requests/inline.json"))
        try fixture.write(fixture.request("publisher", state: "running", seq: 8, phase: "publishing"),
                          to: fixture.stateURL.appendingPathComponent("requests/publisher.json"))
        try fixture.write(fixture.request("normal", state: "running", seq: 1, phase: "preparing"),
                          to: fixture.stateURL.appendingPathComponent("requests/normal.json"))
        try fixture.write(fixture.request("queued", state: "queued", seq: 2),
                          to: fixture.stateURL.appendingPathComponent("requests/queued.json"))

        let snapshot = SnapshotReader(root: fixture.root).read(selectedID: fixture.repository.id)

        #expect(snapshot.activeRequests.map(\.id) == ["normal", "inline", "publisher"])
        #expect(snapshot.waitingRequests.map(\.id) == ["queued"])
        #expect(snapshot.focusedRequest?.id == "publisher")
        #expect(snapshot.requests.first { $0.id == "inline" }?.lane == "inline")
    }

    @Test func testElapsedUsesStartThenSubmissionAndFreezesAtTerminalTime() throws {
        let json = try JSONSerialization.data(withJSONObject: [
            "request_id": "r", "state": "running", "created_at": "2025-12-31T00:00:00Z",
            "submitted_at": "2026-01-01T00:00:00Z",
            "timings": ["started_at": "2026-01-01T00:01:00Z"]
        ])
        let running = try JSONDecoder().decode(RequestRecord.self, from: json)
        #expect(running.elapsed(at: try date("2026-01-01T00:02:05Z")) == "1m 5s")

        let fallbackData = try JSONSerialization.data(withJSONObject: [
            "request_id": "fallback", "state": "queued",
            "created_at": "2025-12-31T00:00:00Z", "submitted_at": "2026-01-01T00:00:00Z"
        ])
        let fallback = try JSONDecoder().decode(RequestRecord.self, from: fallbackData)
        #expect(fallback.elapsed(at: try date("2026-01-01T00:00:45Z")) == "45s")

        let terminalData = try JSONSerialization.data(withJSONObject: [
            "request_id": "done", "state": "landed", "submitted_at": "2026-01-01T00:00:00Z",
            "timings": ["started_at": "2026-01-01T00:01:00Z"],
            "result": ["finished_at": "2026-01-01T00:01:20Z"]
        ])
        let terminal = try JSONDecoder().decode(RequestRecord.self, from: terminalData)
        #expect(terminal.elapsed(at: try date("2026-01-01T02:00:00Z")) == "20s")
    }

    @Test func testMissingRootIsReadOnly() throws {
        let root = FileManager.default.temporaryDirectory
            .appendingPathComponent("merge-queue-missing-\(UUID().uuidString)")
        #expect(!FileManager.default.fileExists(atPath: root.path))

        let snapshot = SnapshotReader(root: root).read(selectedID: nil)

        #expect(snapshot.repositories.isEmpty)
        #expect(!FileManager.default.fileExists(atPath: root.path))
    }

    @Test func testMalformedRequestKeepsPreviousSnapshotAndWarns() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.createRepository()
        let previous = DashboardSnapshot(repositories: [fixture.repository],
                                          selectedRepositoryID: fixture.repository.id,
                                          requests: [RequestRecord(id: "last-good", state: "running", sequence: 4)])
        try Data("{bad json".utf8).write(to: fixture.stateURL.appendingPathComponent("requests/broken.json"))

        let snapshot = SnapshotReader(root: fixture.root).read(selectedID: fixture.repository.id, previous: previous)

        #expect(snapshot.requests.map(\.id) == ["last-good"])
        #expect(snapshot.warning?.contains("last readable snapshot") == true)
    }

    @Test func testMismatchedRegistryRecordIsIgnoredWithWarning() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.createRepository()
        try fixture.write([
            "repo_id": "bbbbbbbbbbbb", "common_dir": fixture.repository.commonDirectory,
            "checkout": fixture.repository.checkout, "state_dir": fixture.repository.stateDirectory,
            "target": fixture.repository.target
        ], to: fixture.root.appendingPathComponent("repos/\(fixture.repository.id).json"))

        let snapshot = SnapshotReader(root: fixture.root).read(selectedID: fixture.repository.id)

        #expect(snapshot.repositories.isEmpty)
        #expect(snapshot.warning?.contains("could not be read") == true)
    }

    @Test func testSelectionIsolatedAndFallsBackWhenSelectedRepositoryIsRemoved() throws {
        let fixture = try Fixture(name: "alpha")
        defer { fixture.remove() }
        let betaCommon = fixture.root.appendingPathComponent("beta/.git").path
        let betaID = SnapshotReader.repositoryID(commonDirectory: betaCommon)
        let betaState = fixture.root.appendingPathComponent("beta-state")
        try fixture.createRepository()
        try fixture.write([
            "repo_id": betaID, "common_dir": betaCommon,
            "checkout": fixture.root.appendingPathComponent("beta").path,
            "state_dir": betaState.path, "target": "refs/heads/main"
        ], to: fixture.root.appendingPathComponent("repos/\(betaID).json"))
        try fixture.write(fixture.request("alpha-request", state: "running", seq: 1),
                          to: fixture.stateURL.appendingPathComponent("requests/alpha-request.json"))
        let betaRequest = fixture.request("beta-request", state: "running", seq: 2)
        var isolated = betaRequest
        isolated["repository"] = betaCommon
        try fixture.write(isolated, to: betaState.appendingPathComponent("requests/beta-request.json"))

        let reader = SnapshotReader(root: fixture.root)
        let beta = reader.read(selectedID: betaID)
        #expect(beta.selectedRepositoryID == betaID)
        #expect(beta.requests.map(\.id) == ["beta-request"])

        try FileManager.default.removeItem(at: fixture.root.appendingPathComponent("repos/\(betaID).json"))
        let fallback = reader.read(selectedID: betaID)
        #expect(fallback.selectedRepositoryID == fixture.repository.id)
        #expect(fallback.requests.map(\.id) == ["alpha-request"])
    }

    @Test func testUnknownStateRemainsActiveInsteadOfAppearingIdle() {
        let request = RequestRecord(id: "future", state: "paused_by_newer_runner", sequence: 2)
        let snapshot = DashboardSnapshot(requests: [request])

        #expect(request.isActive)
        #expect(snapshot.activeRequests.map(\.id) == ["future"])
        #expect(request.stageLabel == "Paused By Newer Runner")
    }

    @Test func testAcknowledgementRemovesActiveRequestWithoutRecreatingIt() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.createRepository()
        let requestURL = fixture.stateURL.appendingPathComponent("requests/done.json")
        try fixture.write(fixture.request("done", state: "queued", seq: 1), to: requestURL)
        let reader = SnapshotReader(root: fixture.root)
        #expect(reader.read(selectedID: fixture.repository.id).requests.first?.state == "queued")

        try fixture.write([
            "request_id": "done", "owner_id": "synthetic", "source_commit_sha": "abc",
            "target_branch": "refs/heads/main", "state": "landed", "seq": 1,
            "landed_sha": "def", "acknowledged_at": "2026-01-01T00:02:00Z"
        ], to: fixture.stateURL.appendingPathComponent("tombstones/done.json"))
        try FileManager.default.removeItem(at: requestURL)

        let snapshot = reader.read(selectedID: fixture.repository.id)

        #expect(snapshot.requests.isEmpty)
        #expect(!FileManager.default.fileExists(atPath: requestURL.path))
    }

    @Test func testRegisteredRepositoryWithMissingStateDirectoryWarns() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try FileManager.default.createDirectory(at: fixture.root.appendingPathComponent("repos"),
                                                withIntermediateDirectories: true)
        try fixture.write([
            "repo_id": fixture.repository.id, "common_dir": fixture.repository.commonDirectory,
            "checkout": fixture.repository.checkout, "state_dir": fixture.stateURL.path,
            "target": fixture.repository.target
        ], to: fixture.root.appendingPathComponent("repos/\(fixture.repository.id).json"))

        let snapshot = SnapshotReader(root: fixture.root).read(selectedID: fixture.repository.id)

        #expect(snapshot.selectedRepositoryID == fixture.repository.id)
        #expect(snapshot.warning?.contains("queue directory is unavailable") == true)
    }


    @Test func testRegistryHashMustMatchCommonDirectory() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.createRepository()
        try FileManager.default.removeItem(at: fixture.root.appendingPathComponent("repos/\(fixture.repository.id).json"))
        let wrongID = "000000000000"
        try fixture.write([
            "repo_id": wrongID, "common_dir": fixture.repository.commonDirectory,
            "checkout": fixture.repository.checkout, "state_dir": fixture.repository.stateDirectory,
            "target": fixture.repository.target
        ], to: fixture.root.appendingPathComponent("repos/\(wrongID).json"))
        let snapshot = SnapshotReader(root: fixture.root).read(selectedID: wrongID)
        #expect(snapshot.repositories.isEmpty)
        #expect(snapshot.warning != nil)
    }

    @Test func testForeignRequestsCannotAppearUnderSelectedRepository() throws {
        let fixture = try Fixture()
        defer { fixture.remove() }
        try fixture.createRepository()
        var foreign = fixture.request("foreign", state: "running", seq: 1)
        foreign["repository"] = fixture.root.appendingPathComponent("another/.git").path
        try fixture.write(foreign, to: fixture.stateURL.appendingPathComponent("requests/foreign.json"))
        let snapshot = SnapshotReader(root: fixture.root).read(selectedID: fixture.repository.id)
        #expect(snapshot.requests.isEmpty)
        #expect(snapshot.warning != nil)
    }

    private func date(_ value: String) throws -> Date {
        try #require(QueueDate.parse(value))
    }
}

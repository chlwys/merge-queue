import Foundation
import Testing
@testable import QueueModel

@Suite
struct MissionStateTests {
    private func date(_ value: String) throws -> Date { try #require(QueueDate.parse(value)) }

    private func request(_ id: String, state: String = "landed", sequence: Int = 0,
                         start: String? = nil, finish: String? = nil, lane: String? = nil,
                         phase: String? = nil, firstObserved: Date? = nil) throws -> RequestRecord {
        RequestRecord(id: id, state: state, phase: phase, sequence: sequence,
            submittedAt: try date("2026-01-01T00:00:00Z"),
            startedAt: try start.map(date), finishedAt: try finish.map(date), lane: lane,
            firstObservedAt: firstObserved)
    }

    @Test func estimateUsesMedianOfThreeRecentSuccessfulNormalMerges() throws {
        let now = try date("2026-01-01T01:00:00Z")
        let history = [
            try request("n1", sequence: 1, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T00:02:00Z"),
            try request("n2", sequence: 2, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T00:04:00Z"),
            try request("n3", sequence: 3, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T00:06:00Z"),
            try request("inline", sequence: 4, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T02:00:00Z", lane: "inline"),
            try request("failed", state: "validation_failed", sequence: 5, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T03:00:00Z"),
            try request("invalid", sequence: 6, start: "2026-01-01T01:00:00Z", finish: "2026-01-01T00:00:00Z"),
            try request("missing", sequence: 7, start: "2026-01-01T00:00:00Z")
        ]
        let active = try request("active", state: "running", sequence: 8, start: "2026-01-01T00:59:00Z", phase: "validating")
        let snapshot = DashboardSnapshot(requests: history + [active,
            try request("q1", state: "queued", sequence: 9),
            try request("q2", state: "dispatched", sequence: 10)])

        let estimate = snapshot.queueEstimate(at: now)

        // Median is 240s; one active has 180s left and two requests wait: 660s.
        #expect(estimate.seconds == 660)
        #expect(estimate.label == "Est. to clear: ~11m")
    }

    @Test func estimateLearningIdleWarningAndConcurrentCasesAreExplicit() throws {
        let now = try date("2026-01-01T01:00:00Z")
        let tooLittleHistory = DashboardSnapshot(requests: [
            try request("one", sequence: 1, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T00:02:00Z"),
            try request("two", sequence: 2, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T00:04:00Z"),
            try request("q", state: "queued", sequence: 3)
        ])
        #expect(tooLittleHistory.queueEstimate(at: now).label == "Est. to clear: learning")
        #expect(DashboardSnapshot().queueEstimate(at: now).seconds == 0)

        var warning = tooLittleHistory
        warning.warning = "Queue data unavailable"
        #expect(warning.queueEstimate(at: now).label == "Est. to clear: unavailable")

        let sampleStart = try date("2026-01-01T00:00:00Z")
        let sampleFinish = try date("2026-01-01T00:02:00Z")
        let samples = (1...3).map { i in
            RequestRecord(id: "s\(i)", state: "landed", sequence: i,
                startedAt: sampleStart, finishedAt: sampleFinish)
        }
        let inlineActive = RequestRecord(id: "inline", state: "running", phase: "validating", lane: "inline")
        #expect(DashboardSnapshot(requests: samples + [inlineActive]).queueEstimate(at: now).seconds == nil)
        let normalActive = RequestRecord(id: "a", state: "running", phase: "validating", startedAt: now)
        let secondActive = RequestRecord(id: "b", state: "running", phase: "preparing", startedAt: now)
        #expect(DashboardSnapshot(requests: samples + [normalActive, secondActive]).queueEstimate(at: now).seconds == nil)
        #expect(DashboardSnapshot(requests: samples + [RequestRecord(id: "unknown", state: "running")])
            .queueEstimate(at: now).seconds == nil)
        #expect(DashboardSnapshot(requests: samples + [RequestRecord(id: "old", state: "running", phase: "validating",
                startedAt: try date("2026-01-01T00:00:00Z"))]).queueEstimate(at: now).seconds == nil)
    }

    @Test func completedStationContainsOnlyLandedAndOrdersNewestFirst() throws {
        let snapshot = DashboardSnapshot(requests: [
            try request("older", sequence: 1, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T00:02:00Z"),
            try request("newer", sequence: 2, start: "2026-01-01T00:00:00Z", finish: "2026-01-01T00:03:00Z"),
            try request("failed", state: "blocked", sequence: 3, finish: "2026-01-01T00:04:00Z"),
            try request("superseded", state: "superseded", sequence: 4, finish: "2026-01-01T00:05:00Z")
        ])
        #expect(snapshot.completedRequests.map(\.id) == ["newer", "older"])
    }

    @Test func failureDebrisUsesHalfOpenFiveMinuteWindowAndObservationFallback() throws {
        let now = try date("2026-01-01T00:05:00Z")
        let finish = try date("2026-01-01T00:00:00Z")
        let finishedFailure = try request("finished", state: "infra_failed", finish: "2026-01-01T00:00:00Z")
        let observedFailure = try request("observed", state: "blocked", firstObserved: now.addingTimeInterval(-299))
        let expiredFailure = try request("expired", state: "conflict", firstObserved: now.addingTimeInterval(-300))
        let snapshot = DashboardSnapshot(requests: [finishedFailure, observedFailure, expiredFailure,
            try request("landed", finish: "2026-01-01T00:00:00Z")])

        let debris = snapshot.debrisRequests(at: now)
        #expect(Set(debris.map(\.id)) == Set(["observed"]))
        #expect(snapshot.debrisRequests(at: finish).contains { $0.id == "finished" })
        #expect(!snapshot.debrisRequests(at: now.addingTimeInterval(1)).contains { $0.id == "observed" })
    }

    @Test func failureFinishedAtWinsAndActiveTimingFinishDoesNotFreezeElapsed() throws {
        let failureJSON = try JSONSerialization.data(withJSONObject: [
            "request_id": "failure", "state": "validation_failed", "submitted_at": "2026-01-01T00:00:00Z",
            "timings": ["started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:04:00Z"],
            "result": ["finished_at": "2026-01-01T00:03:00Z", "message": "synthetic"]
        ])
        let failure = try JSONDecoder().decode(RequestRecord.self, from: failureJSON)
        #expect(failure.finishedAt == (try date("2026-01-01T00:03:00Z")))
        #expect(failure.elapsed(at: try date("2026-01-01T00:10:00Z")) == "3m 0s")

        let activeJSON = try JSONSerialization.data(withJSONObject: [
            "request_id": "active", "state": "running", "submitted_at": "2026-01-01T00:00:00Z",
            "timings": ["started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:01:00Z"]
        ])
        let active = try JSONDecoder().decode(RequestRecord.self, from: activeJSON)
        #expect(active.finishedAt == nil)
        #expect(active.elapsed(at: try date("2026-01-01T00:05:00Z")) == "5m 0s")
    }

    @Test func transientAndDurableFailureAreDeduplicatedByRequestID() throws {
        let observed = try request("same", state: "blocked", firstObserved: date("2026-01-01T00:00:00Z"))
        let durable = try request("same", state: "blocked", finish: "2026-01-01T00:00:30Z")
        let snapshot = DashboardSnapshot(requests: [durable], transientFailures: [observed])
        #expect(snapshot.debrisRequests(at: try date("2026-01-01T00:01:00Z")).count == 1)
    }
}

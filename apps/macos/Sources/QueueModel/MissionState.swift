import Foundation

public struct QueueTimeEstimate: Equatable, Sendable {
    public let seconds: TimeInterval?
    public let label: String
    public let detail: String
    public init(seconds: TimeInterval?, label: String, detail: String) {
        self.seconds = seconds; self.label = label; self.detail = detail
    }
}

extension DashboardSnapshot {
    public static let debrisLifetime: TimeInterval = 5 * 60

    /// Available landed receipts, not an all-time commit count. A request can contain multiple commits.
    public var completedRequests: [RequestRecord] {
        requests.filter { $0.state == "landed" }.sorted {
            if $0.finishedAt == $1.finishedAt { return $0.sequence > $1.sequence }
            return ($0.finishedAt ?? .distantPast) > ($1.finishedAt ?? .distantPast)
        }
    }

    public func debrisRequests(at now: Date) -> [RequestRecord] {
        var byID = Dictionary(transientFailures.map { ($0.id, $0) }, uniquingKeysWith: { _, last in last })
        for request in requests where request.isFailure { byID[request.id] = request }
        return byID.values.filter { request in
            guard request.isFailure, let origin = request.debrisOrigin else { return false }
            let age = now.timeIntervalSince(origin)
            return age >= 0 && age < Self.debrisLifetime
        }.sorted { ($0.debrisOrigin ?? .distantPast) > ($1.debrisOrigin ?? .distantPast) }
    }

    /// A deliberately coarse estimate of time to clear the normal FIFO, never a promised ETA.
    public func queueEstimate(at now: Date) -> QueueTimeEstimate {
        func unavailable(_ reason: String) -> QueueTimeEstimate {
            QueueTimeEstimate(seconds: nil, label: "Est. to clear: unavailable", detail: reason)
        }
        if warning != nil {
            return unavailable("Queue data or checkout readiness needs attention; remaining time cannot be estimated reliably.")
        }
        let work = activeRequests + waitingRequests
        if work.isEmpty {
            return QueueTimeEstimate(seconds: 0, label: "No active or waiting requests", detail: "No active or waiting merge requests in this snapshot.")
        }
        if work.contains(where: { $0.lane == "inline" }) || activeRequests.count > 1 {
            return unavailable("Concurrent or inline work does not follow one serial queue. No serial completion estimate is shown.")
        }
        if activeRequests.contains(where: { $0.state != "running" || !["preparing", "validating", "publishing"].contains($0.phase ?? "") }) {
            return unavailable("The active request's execution stage is unknown.")
        }
        let samples = Array(completedRequests.filter { $0.lane != "inline" }.compactMap { request -> TimeInterval? in
            guard let start = request.startedAt, let end = request.finishedAt else { return nil }
            let duration = end.timeIntervalSince(start)
            return duration.isFinite && duration > 0 ? duration : nil
        }.prefix(20))
        guard samples.count >= 3 else {
            return QueueTimeEstimate(seconds: nil, label: "Est. to clear: learning", detail: "Needs 3 successful normal merges with start and finish times; \(samples.count) available. No duration is invented.")
        }
        let ordered = samples.sorted(), middle = ordered.count / 2
        let typical = ordered.count.isMultiple(of: 2) ? (ordered[middle - 1] + ordered[middle]) / 2 : ordered[middle]
        var remaining = typical * Double(waitingRequests.count)
        if let active = focusedRequest {
            guard let start = active.startedAt else { return unavailable("The active merge has no recorded start time.") }
            let elapsed = max(0, now.timeIntervalSince(start))
            guard elapsed.isFinite, elapsed < typical else {
                return unavailable("The active merge has exceeded the recent median duration; its remaining time is unknown.")
            }
            remaining += typical - elapsed
        }
        let minutes = max(1, Int(ceil(remaining / 60)))
        let duration = minutes >= 60 ? "\(minutes / 60)h \(minutes % 60)m" : "\(minutes)m"
        return QueueTimeEstimate(seconds: remaining, label: "Est. to clear: ~\(duration)",
            detail: "Rough time for all current active and waiting normal requests, using the median of \(samples.count) recent successful normal merges. Validation durations vary; this is not a guaranteed ETA.")
    }
}

extension RequestRecord {
    public var debrisOrigin: Date? {
        switch (finishedAt, firstObservedAt) {
        case let (finish?, seen?): return min(finish, seen)
        case let (finish?, nil): return finish
        case let (nil, seen?): return seen
        case (nil, nil): return nil
        }
    }
}

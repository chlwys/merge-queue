import Foundation

public struct RepositoryRecord: Decodable, Identifiable, Equatable, Sendable {
    public let id: String
    public let commonDirectory: String
    public let checkout: String
    public let stateDirectory: String
    public let target: String
    public var name: String {
        let url = URL(fileURLWithPath: commonDirectory)
        return (url.lastPathComponent == ".git" ? url.deletingLastPathComponent() : url).lastPathComponent
    }
    enum CodingKeys: String, CodingKey {
        case id = "repo_id", commonDirectory = "common_dir", checkout, stateDirectory = "state_dir", target
    }
    public init(id: String, commonDirectory: String, checkout: String, stateDirectory: String, target: String) {
        self.id = id; self.commonDirectory = commonDirectory; self.checkout = checkout
        self.stateDirectory = stateDirectory; self.target = target
    }
}

public struct RequestRecord: Decodable, Identifiable, Equatable, Sendable {
    public let id: String
    public let state: String
    public let phase: String?
    public let sourceBranch: String?
    public let sequence: Int
    public let submittedAt: Date?
    public let startedAt: Date?
    public let finishedAt: Date?
    public let failureMessage: String?
    public let repository: String?
    public let lane: String?
    public var displayName: String { sourceBranch?.replacingOccurrences(of: "refs/heads/", with: "") ?? id }
    public static let terminalStates: Set<String> = ["landed", "conflict", "validation_failed", "blocked", "infra_failed", "superseded"]
    public var isActive: Bool { !Self.terminalStates.contains(state) && !isWaiting }
    public var isWaiting: Bool { state == "queued" || state == "dispatched" }
    public var isFailure: Bool { ["conflict", "validation_failed", "blocked", "infra_failed"].contains(state) }
    public var stageLabel: String {
        switch phase ?? state {
        case "preparing": return "Preparing"
        case "validating": return "Validating"
        case "publishing": return "Landing"
        case "waiting", "dispatched", "queued": return "Waiting"
        case "landed": return "Landed"
        case "validation_failed": return "Check failed"
        case "infra_failed": return "Needs attention"
        case "conflict": return "Conflict"
        case "blocked": return "Blocked"
        default: return (phase ?? state).replacingOccurrences(of: "_", with: " ").capitalized
        }
    }
    // Position represents lifecycle stage, never a percentage or an estimated completion time.
    public var routePosition: Double {
        switch phase {
        case "preparing": return 0.24
        case "validating": return 0.53
        case "publishing": return 0.82
        default: return state == "landed" ? 1 : 0.12
        }
    }
    public func elapsed(at now: Date) -> String {
        guard let start = startedAt ?? submittedAt else { return "—" }
        let seconds = max(0, Int((finishedAt ?? now).timeIntervalSince(start)))
        if seconds >= 3600 { return "\(seconds / 3600)h \((seconds % 3600) / 60)m" }
        if seconds >= 60 { return "\(seconds / 60)m \(seconds % 60)s" }
        return "\(seconds)s"
    }
    public init(id: String, state: String, phase: String? = nil, sourceBranch: String? = nil,
                sequence: Int = 0, submittedAt: Date? = nil, startedAt: Date? = nil,
                finishedAt: Date? = nil, failureMessage: String? = nil, repository: String? = nil,
                lane: String? = nil) {
        self.id = id; self.state = state; self.phase = phase; self.sourceBranch = sourceBranch
        self.sequence = sequence; self.submittedAt = submittedAt; self.startedAt = startedAt
        self.finishedAt = finishedAt; self.failureMessage = failureMessage; self.repository = repository; self.lane = lane
    }
    enum CodingKeys: String, CodingKey {
        case id = "request_id", state, phase, sourceBranch = "source_branch", sequence = "seq"
        case submittedAt = "submitted_at", timings, result, repository, lane, acknowledgedAt = "acknowledged_at"
    }
    struct Timings: Decodable { let started_at: String?; let finished_at: String? }
    struct Result: Decodable { let finished_at: String?; let message: String?; let failure_type: String? }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        state = try c.decode(String.self, forKey: .state)
        phase = try c.decodeIfPresent(String.self, forKey: .phase)
        sourceBranch = try c.decodeIfPresent(String.self, forKey: .sourceBranch)
        sequence = try c.decodeIfPresent(Int.self, forKey: .sequence) ?? 0
        submittedAt = QueueDate.parse(try c.decodeIfPresent(String.self, forKey: .submittedAt))
        let timing = try c.decodeIfPresent(Timings.self, forKey: .timings)
        let result = try c.decodeIfPresent(Result.self, forKey: .result)
        startedAt = QueueDate.parse(timing?.started_at)
        let acknowledged = try c.decodeIfPresent(String.self, forKey: .acknowledgedAt)
        finishedAt = QueueDate.parse(result?.finished_at ?? timing?.finished_at) ?? QueueDate.parse(acknowledged)
        failureMessage = result?.message ?? result?.failure_type
        repository = try c.decodeIfPresent(String.self, forKey: .repository)
        lane = try c.decodeIfPresent(String.self, forKey: .lane)
    }
}

public enum QueueDate {
    public static func parse(_ value: String?) -> Date? {
        guard let value else { return nil }
        let formatter = ISO8601DateFormatter()
        formatter.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let date = formatter.date(from: value) { return date }
        formatter.formatOptions = [.withInternetDateTime]
        return formatter.date(from: value)
    }
}

public struct DashboardSnapshot: Sendable {
    public var repositories: [RepositoryRecord]
    public var selectedRepositoryID: String?
    public var requests: [RequestRecord]
    public var warning: String?
    public var isLoading: Bool
    public var selectedRepository: RepositoryRecord? { repositories.first { $0.id == selectedRepositoryID } }
    public var activeRequests: [RequestRecord] { requests.filter(\.isActive).sorted { $0.sequence < $1.sequence } }
    public var focusedRequest: RequestRecord? {
        activeRequests.first { $0.phase == "publishing" } ?? activeRequests.first
    }
    public var waitingRequests: [RequestRecord] { requests.filter(\.isWaiting).sorted { $0.sequence < $1.sequence } }
    public var recentRequests: [RequestRecord] {
        Array(requests.filter { RequestRecord.terminalStates.contains($0.state) && $0.state != "superseded" }
            .sorted { ($0.finishedAt ?? .distantPast) > ($1.finishedAt ?? .distantPast) }.prefix(3))
    }
    public init(repositories: [RepositoryRecord] = [], selectedRepositoryID: String? = nil,
                requests: [RequestRecord] = [], warning: String? = nil, isLoading: Bool = false) {
        self.repositories = repositories; self.selectedRepositoryID = selectedRepositoryID
        self.requests = requests; self.warning = warning; self.isLoading = isLoading
    }
}

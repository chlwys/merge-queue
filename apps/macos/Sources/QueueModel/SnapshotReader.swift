import Foundation
import CryptoKit

public struct SnapshotReader: Sendable {
    public let root: URL
    public init(root: URL) { self.root = root.resolvingSymlinksInPath() }
    public static func defaultRoot(environment: [String: String] = ProcessInfo.processInfo.environment) -> URL {
        if let path = environment["MERGE_QUEUE_STATE_HOME"], !path.isEmpty { return URL(fileURLWithPath: path) }
        if let path = environment["XDG_STATE_HOME"], !path.isEmpty {
            return URL(fileURLWithPath: path).appendingPathComponent("merge-queue", isDirectory: true)
        }
        return FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent(".local/state/merge-queue", isDirectory: true)
    }
    public static func repositoryID(commonDirectory: String) -> String {
        SHA256.hash(data: Data(commonDirectory.utf8)).prefix(6).map { String(format: "%02x", $0) }.joined()
    }
    public func read(selectedID: String?, previous: DashboardSnapshot? = nil) -> DashboardSnapshot {
        var snapshot = DashboardSnapshot()
        do {
            var issues = 0
            snapshot.repositories = try jsonFiles(root.appendingPathComponent("repos"), allowMissing: true).compactMap { file in
                do {
                    let record = try decode(RepositoryRecord.self, at: file)
                    guard record.id == file.deletingPathExtension().lastPathComponent,
                          record.id.range(of: "^[a-f0-9]{12}$", options: .regularExpression) != nil,
                          [record.checkout, record.stateDirectory, record.commonDirectory].allSatisfy({ $0.hasPrefix("/") }),
                          record.target.hasPrefix("refs/heads/"),
                          record.id == Self.repositoryID(commonDirectory: record.commonDirectory) else { issues += 1; return nil }
                    return record
                } catch { issues += 1; return nil }
            }.sorted { ($0.name, $0.id) < ($1.name, $1.id) }
            snapshot.selectedRepositoryID = snapshot.repositories.contains { $0.id == selectedID }
                ? selectedID : snapshot.repositories.first?.id
            if issues > 0 { snapshot.warning = "\(issues) repository record(s) could not be read. Refresh after repairing the registry." }
            if let repo = snapshot.selectedRepository {
                do {
                    let directory = URL(fileURLWithPath: repo.stateDirectory, isDirectory: true)
                    guard FileManager.default.fileExists(atPath: directory.path) else {
                        throw ReadFailure.message("The selected repository’s queue directory is unavailable.")
                    }
                    var byID: [String: RequestRecord] = [:]
                    for file in try jsonFiles(directory.appendingPathComponent("requests"), allowMissing: false) {
                        do {
                            let request = try decode(RequestRecord.self, at: file)
                            guard request.id == file.deletingPathExtension().lastPathComponent,
                                  request.repository == repo.commonDirectory else {
                                throw ReadFailure.message("A request does not match its repository.")
                            }
                            byID[request.id] = request
                        } catch CocoaError.fileReadNoSuchFile {
                            // Atomic acknowledgement can remove a request during this scan.
                            continue
                        }
                    }
                    snapshot.requests = Array(byID.values)
                    let notices = directory.appendingPathComponent("notices")
                    if FileManager.default.fileExists(atPath: notices.appendingPathComponent("stale-tq.json").path) {
                        snapshot.warning = "Dispatch is blocked. Inspect merge-queue status for this repository."
                    } else if FileManager.default.fileExists(atPath: notices.appendingPathComponent("checkout-wait.json").path) {
                        snapshot.warning = "Waiting for the integration checkout to become ready."
                    }
                } catch {
                    if previous?.selectedRepositoryID == repo.id { snapshot.requests = previous?.requests ?? [] }
                    snapshot.warning = "Queue data unavailable; showing the last readable snapshot. \(error.localizedDescription)"
                }
            }
        } catch {
            snapshot = previous ?? snapshot
            snapshot.isLoading = false
            snapshot.warning = "Repository registry unavailable. \(error.localizedDescription)"
        }
        return snapshot
    }
    private func jsonFiles(_ directory: URL, allowMissing: Bool) throws -> [URL] {
        do {
            return try FileManager.default.contentsOfDirectory(at: directory, includingPropertiesForKeys: [.contentModificationDateKey], options: [.skipsHiddenFiles])
                .filter { $0.pathExtension == "json" }
        } catch CocoaError.fileReadNoSuchFile where allowMissing { return [] }
    }
    private func decode<T: Decodable>(_ type: T.Type, at url: URL) throws -> T {
        let data = try Data(contentsOf: url, options: [.mappedIfSafe])
        guard data.count < 4 * 1024 * 1024 else { throw ReadFailure.message("A queue record exceeds the size limit.") }
        return try JSONDecoder().decode(type, from: data)
    }
    private func modificationDate(_ url: URL) -> Date {
        (try? url.resourceValues(forKeys: [.contentModificationDateKey]).contentModificationDate) ?? .distantPast
    }
    private enum ReadFailure: LocalizedError {
        case message(String)
        var errorDescription: String? { if case let .message(text) = self { return text }; return nil }
    }
}

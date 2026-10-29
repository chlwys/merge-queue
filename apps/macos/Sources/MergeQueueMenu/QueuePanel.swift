import SwiftUI
import QueueModel

struct QueuePanel: View {
    let snapshot: DashboardSnapshot
    let theme: QueueThemeID
    let onSelectRepository: (String) -> Void
    let onSelectTheme: (QueueThemeID) -> Void
    let onRefresh: () -> Void
    let onQuit: () -> Void

    private var palette: QueuePalette { .forTheme(theme) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            if snapshot.isLoading && snapshot.repositories.isEmpty {
                message("Loading queues…", icon: "arrow.triangle.2.circlepath")
            } else if snapshot.repositories.isEmpty {
                message(snapshot.warning ?? "No repositories installed. Run merge-queue install in a repository to get started.", icon: "square.stack.3d.up")
            } else {
                content
            }
            Spacer(minLength: 0)
            repositorySelector
        }
        .frame(width: 390, height: 500)
        .background(palette.background)
        .foregroundStyle(palette.text)
        .preferredColorScheme(.dark)
    }

    private var header: some View {
        HStack {
            Text("MERGE QUEUE").font(.system(size: 11, weight: .semibold, design: .rounded)).tracking(1.4)
            Spacer()
            Menu {
                ForEach(QueueThemeID.allCases) { choice in
                    Button { onSelectTheme(choice) } label: {
                        if choice == theme { Label(choice.title, systemImage: "checkmark") } else { Text(choice.title) }
                    }
                }
                Divider()
                Button("Refresh", action: onRefresh)
                Button("Quit", action: onQuit)
            } label: {
                Image(systemName: "ellipsis").font(.system(size: 15, weight: .semibold)).frame(width: 28, height: 24).contentShape(Rectangle())
            }
            .menuStyle(.borderlessButton).fixedSize()
            .accessibilityLabel("Theme and actions")
        }
        .padding(.horizontal, 20).padding(.top, 16)
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 0) {
            if let warning = snapshot.warning {
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(.system(size: 11)).foregroundStyle(palette.danger)
                    .lineLimit(2).help(warning).padding(.horizontal, 20).padding(.top, 10)
            }
            if let request = snapshot.focusedRequest {
                TimelineView(.periodic(from: .now, by: 1)) { context in
                    route(request: request, elapsed: request.elapsed(at: context.date))
                }
            } else {
                route(request: nil, elapsed: nil)
            }
            requestList
        }
    }

    private func route(request: RequestRecord?, elapsed: String?) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            SpaceArtwork(theme: theme, palette: palette, hasActiveRequest: request != nil, routePosition: request?.routePosition)
                .frame(height: request == nil ? 164 : 138)
                .accessibilityLabel(request.map { "\($0.displayName), \($0.stageLabel), \(elapsed ?? "")" } ?? "No active request")
                .overlay(alignment: .bottomLeading) {
                    if request == nil {
                        Text(idleLabel)
                            .font(.system(size: 13, weight: .medium)).foregroundStyle(palette.muted)
                            .padding(.leading, 20).padding(.bottom, 5)
                    } else if snapshot.activeRequests.count > 1 {
                        Text("+\(snapshot.activeRequests.count - 1) active")
                            .font(.system(size: 10)).foregroundStyle(palette.muted)
                            .padding(.leading, 20).padding(.bottom, 5)
                    }
                }
            if let request {
                VStack(alignment: .leading, spacing: 4) {
                    Text(request.displayName)
                        .font(.system(size: 12, weight: .semibold))
                        .lineLimit(4).truncationMode(.tail)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled).help(request.displayName)
                    Text([request.stageLabel, elapsed].compactMap { $0 }.joined(separator: " · "))
                        .font(.system(size: 11)).foregroundStyle(palette.muted)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 20).padding(.top, 2).padding(.bottom, 8)
            }
        }
    }

    private var idleLabel: String {
        if snapshot.warning != nil { return "Status unavailable" }
        if snapshot.recentRequests.contains(where: \.isFailure) { return "Needs attention" }
        return snapshot.waitingRequests.isEmpty ? "All clear" : "Waiting to launch"
    }

    private var requestList: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Text("QUEUE").font(.system(size: 10, weight: .semibold)).tracking(1.2).foregroundStyle(palette.muted)
                Spacer()
                if !snapshot.waitingRequests.isEmpty {
                    Text("\(snapshot.waitingRequests.count) waiting").font(.system(size: 10)).foregroundStyle(palette.muted)
                }
            }.padding(.bottom, 2)
            let otherActive = snapshot.activeRequests.filter { $0.id != snapshot.focusedRequest?.id }
            let rows = Array((otherActive + snapshot.waitingRequests + snapshot.recentRequests.filter { !$0.isWaiting && !$0.isActive }).prefix(4))
            if rows.isEmpty {
                Text(snapshot.warning != nil ? "No readable requests" : (snapshot.focusedRequest == nil ? "No requests yet" : "No other requests"))
                    .font(.system(size: 12)).foregroundStyle(palette.muted).padding(.vertical, 7)
            } else {
                ForEach(rows) { request in
                    HStack(spacing: 9) {
                        Circle().fill(request.isFailure ? palette.danger : palette.line).frame(width: 6, height: 6)
                        Text(request.displayName).font(.system(size: 12)).lineLimit(1).truncationMode(.middle).help(request.displayName)
                        Spacer(minLength: 6)
                        Text(request.isFailure ? "Failed" : request.isActive ? "Active · \(request.stageLabel)" : request.isWaiting ? "Waiting" : request.stageLabel).font(.system(size: 10)).foregroundStyle(request.isFailure ? palette.danger : palette.muted).lineLimit(1)
                    }
                    .padding(.horizontal, 10).frame(height: 28)
                    .background(palette.surface.opacity(0.65), in: RoundedRectangle(cornerRadius: 6))
                    .help(request.failureMessage ?? request.displayName)
                    .accessibilityElement(children: .combine)
                }
            }
        }.padding(.horizontal, 20).padding(.top, 8)
        .accessibilityElement(children: .contain)
        .accessibilityLabel("Requests. Active: " + snapshot.activeRequests.map(\.displayName).joined(separator: ", "))
    }

    private func message(_ text: String, icon: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: icon).font(.system(size: 24)).foregroundStyle(palette.accent)
            Text(text).font(.system(size: 13)).foregroundStyle(palette.muted).fixedSize(horizontal: false, vertical: true)
        }.padding(20).padding(.top, 35)
    }

    private var repositorySelector: some View {
        HStack {
            Text("REPOSITORY").font(.system(size: 10, weight: .semibold)).tracking(1).foregroundStyle(palette.muted)
            Spacer(minLength: 8)
            if snapshot.repositories.isEmpty {
                Text("None installed").font(.system(size: 11)).foregroundStyle(palette.muted)
            } else {
                Menu {
                    ForEach(snapshot.repositories) { repository in
                        Button { onSelectRepository(repository.id) } label: {
                            if repository.id == snapshot.selectedRepositoryID { Label(repository.name, systemImage: "checkmark") } else { Text(repository.name) }
                        }
                    }
                } label: {
                    HStack(spacing: 5) {
                        Text(snapshot.selectedRepository?.name ?? "Select repository").lineLimit(1).truncationMode(.middle)
                        Image(systemName: "chevron.up.chevron.down").font(.system(size: 9))
                    }.font(.system(size: 11, weight: .medium)).frame(maxWidth: 210, alignment: .trailing)
                }.menuStyle(.borderlessButton).fixedSize().help(snapshot.selectedRepository?.name ?? "Select repository")
                .accessibilityLabel("Repository")
            }
        }
        .padding(.horizontal, 20).frame(height: 45)
        .overlay(alignment: .top) { Rectangle().fill(palette.line.opacity(0.5)).frame(height: 1) }
    }
}

import SwiftUI
import QueueModel

struct QueuePanel: View {
    static let panelSize = CGSize(width: 320, height: 276)
    let snapshot: DashboardSnapshot
    let theme: QueueThemeID
    let onSelectRepository: (String) -> Void
    let onSelectTheme: (QueueThemeID) -> Void
    let onRefresh: () -> Void
    let onQuit: () -> Void

    @State private var isVisible = false
    @State private var debrisClockExpired = false

    private var palette: QueuePalette { .forTheme(theme) }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
            ScrollView(.vertical) {
                if snapshot.isLoading && snapshot.repositories.isEmpty {
                    message("Loading queues…", icon: "arrow.triangle.2.circlepath")
                } else if snapshot.repositories.isEmpty {
                    message(snapshot.warning ?? "No repositories installed. Run merge-queue install in a repository to get started.", icon: "square.stack.3d.up")
                } else {
                    content
                }
            }
        }
        .frame(width: Self.panelSize.width, height: Self.panelSize.height)
        .background(palette.background)
        .foregroundStyle(palette.text)
        .preferredColorScheme(.dark)
        .onAppear { isVisible = true; debrisClockExpired = false }
        .onDisappear { isVisible = false }
        .onChange(of: snapshot.requests) { _ in debrisClockExpired = false }
        .onChange(of: snapshot.transientFailures) { _ in debrisClockExpired = false }
    }

    private var header: some View {
        HStack {
            Text("MERGE QUEUE").font(.system(size: 11, weight: .semibold, design: .rounded)).tracking(1.4)
            if let repository = snapshot.selectedRepository {
                Text(repository.name).font(.system(size: 10)).foregroundStyle(palette.muted)
                    .lineLimit(1).truncationMode(.middle).help(repository.name)
            }
            Spacer(minLength: 4)
            Menu {
                Section("Repository") {
                    if snapshot.repositories.isEmpty {
                        Text("None installed")
                    } else {
                        ForEach(snapshot.repositories) { repository in
                            Button { onSelectRepository(repository.id) } label: {
                                if repository.id == snapshot.selectedRepositoryID { Label(repository.name, systemImage: "checkmark") } else { Text(repository.name) }
                            }
                        }
                    }
                }
                Section("Theme") {
                ForEach(QueueThemeID.allCases) { choice in
                    Button { onSelectTheme(choice) } label: {
                        if choice == theme { Label(choice.title, systemImage: "checkmark") } else { Text(choice.title) }
                    }
                }
                }
                Divider()
                Button("Refresh", action: onRefresh)
                Button("Quit", action: onQuit)
            } label: {
                Image(systemName: "line.3.horizontal").font(.system(size: 15, weight: .semibold)).frame(width: 28, height: 24).contentShape(Rectangle())
            }
            .menuStyle(.borderlessButton).menuIndicator(.hidden).fixedSize()
            .accessibilityLabel("Menu")
        }
        .padding(.horizontal, 16).padding(.top, 10)
    }

    private var content: some View {
        Group {
            if isVisible && (!snapshot.activeRequests.isEmpty || (!debrisClockExpired && !snapshot.debrisRequests(at: Date()).isEmpty)) {
                TimelineView(.periodic(from: .now, by: snapshot.activeRequests.isEmpty ? 5 : 1)) { context in
                    content(at: context.date)
                        .onChange(of: context.date) { date in
                            if snapshot.activeRequests.isEmpty && snapshot.debrisRequests(at: date).isEmpty {
                                debrisClockExpired = true
                            }
                        }
                }
            } else {
                content(at: Date())
            }
        }
    }

    private func content(at date: Date) -> some View {
            VStack(alignment: .leading, spacing: 0) {
                if let warning = snapshot.warning {
                    Label(warning, systemImage: "exclamationmark.triangle")
                        .font(.system(size: 11)).foregroundStyle(palette.danger)
                        .lineLimit(2).help(warning).padding(.horizontal, 16).padding(.top, 8)
                }
                SpaceArtwork(theme: theme, palette: palette, snapshot: snapshot, date: date)
                    .frame(height: 148)
                    .padding(.top, 16)
                VStack(alignment: .leading, spacing: 5) {
                    if let request = snapshot.focusedRequest {
                        Text(request.displayName)
                            .font(.system(size: 13, weight: .semibold))
                            .lineLimit(4).truncationMode(.tail)
                            .fixedSize(horizontal: false, vertical: true)
                            .textSelection(.enabled).help(request.displayName)
                        Text([request.stageLabel, request.elapsed(at: date)].joined(separator: " · "))
                            .font(.system(size: 11)).foregroundStyle(palette.muted)
                        if snapshot.activeRequests.count > 1 {
                            Text("+\(snapshot.activeRequests.count - 1) active")
                                .font(.system(size: 10)).foregroundStyle(palette.muted)
                        }
                    } else {
                        Text(idleLabel(at: date)).font(.system(size: 13, weight: .medium)).foregroundStyle(palette.muted)
                    }
                    let estimate = snapshot.queueEstimate(at: date)
                    Text(estimate.label).font(.system(size: 11)).foregroundStyle(palette.muted)
                        .lineLimit(2).help(estimate.detail)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .padding(.horizontal, 16).padding(.top, 5)
            }
    }

    private func idleLabel(at date: Date) -> String {
        if snapshot.warning != nil { return "Status unavailable" }
        if !snapshot.debrisRequests(at: date).isEmpty { return "Needs attention" }
        return snapshot.waitingRequests.isEmpty ? "All clear" : "Waiting to launch"
    }

    private func message(_ text: String, icon: String) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Image(systemName: icon).font(.system(size: 24)).foregroundStyle(palette.accent)
            Text(text).font(.system(size: 13)).foregroundStyle(palette.muted).fixedSize(horizontal: false, vertical: true)
        }.padding(20).padding(.top, 35)
    }

}

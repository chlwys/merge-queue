import SwiftUI
import QueueModel

struct InspectionButton<Artwork: View>: View {
    let title: String
    let subtitle: String
    let empty: String
    let requests: [RequestRecord]
    let palette: QueuePalette
    let history: Bool
    @ViewBuilder let artwork: () -> Artwork
    @State private var isOpen = false
    @State private var closeWork: DispatchWorkItem?

    var body: some View {
        Button { cancelClose(); isOpen = true } label: { artwork().contentShape(Rectangle()) }
            .buttonStyle(.plain)
            .accessibilityLabel("\(title), \(requests.count) requests")
            .accessibilityHint("Show request details")
            .onHover(perform: hover)
            .popover(isPresented: $isOpen, arrowEdge: .bottom) {
                VStack(alignment: .leading, spacing: 8) {
                    Text(subtitle).font(.system(size: 12, weight: .semibold))
                    if history { Text("Shows the current retained history, not all-time completions.").font(.system(size: 10)).foregroundStyle(palette.muted) }
                    if requests.isEmpty {
                        Text(empty).font(.system(size: 11)).foregroundStyle(palette.muted)
                    } else {
                        ScrollView {
                            LazyVStack(alignment: .leading, spacing: 8) {
                                ForEach(requests) { request in
                                    VStack(alignment: .leading, spacing: 2) {
                                        Text(request.displayName).font(.system(size: 11, weight: .medium)).fixedSize(horizontal: false, vertical: true).textSelection(.enabled)
                                        Text(request.stageLabel).font(.system(size: 10)).foregroundStyle(palette.muted)
                                        if let failure = request.failureMessage, !failure.isEmpty {
                                            Text(failure).font(.system(size: 10)).foregroundStyle(palette.danger).fixedSize(horizontal: false, vertical: true)
                                        }
                                    }.frame(maxWidth: .infinity, alignment: .leading)
                                }
                            }
                        }.frame(maxHeight: 180)
                    }
                }.padding(14).frame(width: 260, alignment: .leading).foregroundStyle(palette.text).background(palette.background)
                    .onHover(perform: hover)
            }
            .onDisappear { cancelClose() }
    }
    private func cancelClose() { closeWork?.cancel(); closeWork = nil }
    private func hover(_ inside: Bool) {
        cancelClose()
        if inside { isOpen = true } else {
            let work = DispatchWorkItem { isOpen = false }
            closeWork = work
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.4, execute: work)
        }
    }
}

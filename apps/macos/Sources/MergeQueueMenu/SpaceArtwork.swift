import SwiftUI
import QueueModel

// All artwork is drawn in the panel; lifecycle progress follows the model, not a timer.
struct SpaceArtwork: View {
    let theme: QueueThemeID
    let palette: QueuePalette
    let snapshot: DashboardSnapshot
    let date: Date

    var body: some View {
        GeometryReader { geometry in
            let w = geometry.size.width, h = geometry.size.height
            let start = CGPoint(x: w * 0.78, y: h * 0.69)
            let end = CGPoint(x: w * 0.25, y: h * 0.27)
            let c1 = CGPoint(x: w * 0.66, y: h * 0.18)
            let c2 = CGPoint(x: w * 0.43, y: h * 0.77)
            let t = CGFloat(snapshot.focusedRequest?.routePosition ?? 0.24)
            let point = cubic(start, c1, c2, end, t)
            let tangent = derivative(start, c1, c2, end, t)
            let angle = atan2(tangent.y, tangent.x) * 180 / .pi + 90
            ZStack {
                if theme == .orbital {
                    Canvas { context, size in
                        let stars: [(CGFloat, CGFloat)] = [(0.08,0.65),(0.18,0.12),(0.38,0.17),(0.53,0.09),(0.71,0.22),(0.92,0.12),(0.46,0.83),(0.12,0.91),(0.92,0.44)]
                        for (x,y) in stars { context.fill(Path(ellipseIn: CGRect(x: size.width*x, y: size.height*y, width: 2, height: 2)), with: .color(palette.text.opacity(0.5))) }
                    }.allowsHitTesting(false)
                }
                Path { p in p.move(to: start); p.addCurve(to: end, control1: c1, control2: c2) }
                    .stroke(palette.line, style: StrokeStyle(lineWidth: 1, dash: [3, 5])).allowsHitTesting(false)
                if snapshot.focusedRequest != nil {
                    InspectionButton(title: "Active flights", subtitle: "Active requests", empty: "No active requests.", requests: snapshot.activeRequests, palette: palette, history: false) {
                        ZStack {
                            Circle().fill(palette.accent.opacity(0.18)).frame(width: 32, height: 32)
                            Circle().fill(palette.accent.opacity(0.65)).frame(width: 8, height: 8)
                            Rocket(palette: palette).frame(width: 18, height: 24).rotationEffect(.degrees(Double(angle)))
                        }.frame(width: 40, height: 40)
                    }.position(point)
                }
                InspectionButton(title: "Space station", subtitle: "Recently landed", empty: "No landed requests in retained history.", requests: snapshot.completedRequests, palette: palette, history: true) {
                    Station(palette: palette, modules: min(snapshot.completedRequests.count, 4)).frame(width: 108, height: 62)
                }.position(x: w * 0.22, y: h * 0.23)
                InspectionButton(title: "Earth", subtitle: "Waiting to launch", empty: "No requests waiting.", requests: snapshot.waitingRequests, palette: palette, history: false) {
                    ZStack(alignment: .topTrailing) {
                        Earth(palette: palette).frame(width: 72, height: 72)
                        if !snapshot.waitingRequests.isEmpty {
                            Text("\(snapshot.waitingRequests.count)").font(.system(size: 11, weight: .bold)).foregroundStyle(palette.background)
                                .padding(.horizontal, 6).padding(.vertical, 3).background(palette.accent, in: Capsule()).offset(x: 8, y: -3)
                        }
                    }
                }.position(x: w * 0.80, y: h * 0.72)
                let debris = snapshot.debrisRequests(at: date)
                if !debris.isEmpty {
                    InspectionButton(title: "Recent failures", subtitle: "Recent failures", empty: "No recent failures.", requests: debris, palette: palette, history: false) {
                        Debris(palette: palette).frame(width: 48, height: 35)
                    }.position(x: w * 0.16, y: h * 0.70)
                }
            }
        }
    }
    private func cubic(_ a: CGPoint, _ b: CGPoint, _ c: CGPoint, _ d: CGPoint, _ t: CGFloat) -> CGPoint {
        let u = 1-t
        return CGPoint(x: u*u*u*a.x+3*u*u*t*b.x+3*u*t*t*c.x+t*t*t*d.x, y: u*u*u*a.y+3*u*u*t*b.y+3*u*t*t*c.y+t*t*t*d.y)
    }
    private func derivative(_ a: CGPoint, _ b: CGPoint, _ c: CGPoint, _ d: CGPoint, _ t: CGFloat) -> CGPoint {
        let u = 1-t
        return CGPoint(x: 3*u*u*(b.x-a.x)+6*u*t*(c.x-b.x)+3*t*t*(d.x-c.x), y: 3*u*u*(b.y-a.y)+6*u*t*(c.y-b.y)+3*t*t*(d.y-c.y))
    }
}

private struct Earth: View {
    let palette: QueuePalette
    var body: some View {
        ZStack {
            Circle().fill(palette.surface).overlay(Circle().stroke(palette.accent.opacity(0.7), lineWidth: 1))
            Path { p in
                p.move(to: CGPoint(x: 9,y: 21)); p.addLines([CGPoint(x: 17,y: 13),CGPoint(x: 27,y: 15),CGPoint(x: 31,y: 22),CGPoint(x: 23,y: 29),CGPoint(x: 16,y: 26)]); p.closeSubpath()
                p.move(to: CGPoint(x: 25,y: 34)); p.addLines([CGPoint(x: 33,y: 35),CGPoint(x: 38,y: 42),CGPoint(x: 29,y: 57),CGPoint(x: 24,y: 48),CGPoint(x: 21,y: 38)]); p.closeSubpath()
                p.move(to: CGPoint(x: 44,y: 16)); p.addLines([CGPoint(x: 56,y: 19),CGPoint(x: 62,y: 26),CGPoint(x: 53,y: 30),CGPoint(x: 46,y: 26),CGPoint(x: 40,y: 30),CGPoint(x: 39,y: 22)]); p.closeSubpath()
            }.fill(palette.accent.opacity(0.5)).clipShape(Circle())
        }
    }
}
private struct Station: View {
    let palette: QueuePalette
    let modules: Int
    var body: some View {
        ZStack {
            Rectangle().fill(palette.line).frame(width: 100, height: 2)
            ForEach(0..<4) { index in
                RoundedRectangle(cornerRadius: 2).fill((index == 0 || index == 3 ? modules >= 4 : modules >= 2) ? palette.accent.opacity(0.75) : palette.surface)
                    .overlay(RoundedRectangle(cornerRadius: 2).stroke(palette.accent.opacity(0.65), lineWidth: 1))
                    .frame(width: 15, height: 25).offset(x: [-44.0, -26.0, 26.0, 44.0][index])
            }
            RoundedRectangle(cornerRadius: 5).fill(palette.surface).overlay(RoundedRectangle(cornerRadius: 5).stroke(palette.text, lineWidth: 1)).frame(width: 31, height: 24)
            Rectangle().fill(palette.text.opacity(0.8)).frame(width: 3, height: 41)
            Circle().fill(palette.accent).frame(width: 7, height: 7)
        }
    }
}
private struct Debris: View {
    let palette: QueuePalette
    var body: some View {
        ZStack {
            ForEach(0..<6) { i in
                Rectangle().fill(palette.danger.opacity(0.85)).frame(width: CGFloat(3 + i % 3), height: CGFloat(3 + (i + 1) % 3))
                    .rotationEffect(.degrees(Double(i * 31))).offset(x: CGFloat((i * 17) % 39 - 19), y: CGFloat((i * 11) % 25 - 12))
            }
        }
    }
}
private struct Rocket: View {
    let palette: QueuePalette
    var body: some View {
        ZStack {
            Path { p in p.move(to: CGPoint(x: 9,y: 0)); p.addCurve(to: CGPoint(x: 15,y: 16), control1: CGPoint(x: 15,y: 5), control2: CGPoint(x: 16,y: 11)); p.addLine(to: CGPoint(x: 3,y: 16)); p.addCurve(to: CGPoint(x: 9,y: 0), control1: CGPoint(x: 2,y: 11), control2: CGPoint(x: 3,y: 5)); p.closeSubpath() }.fill(palette.text)
            Path { p in p.move(to: CGPoint(x: 3,y: 11)); p.addLines([CGPoint(x: 0,y: 19),CGPoint(x: 5,y: 17)]); p.move(to: CGPoint(x: 15,y: 11)); p.addLines([CGPoint(x: 18,y: 19),CGPoint(x: 13,y: 17)]) }.fill(palette.accent)
            Circle().fill(palette.background).frame(width: 4, height: 4).position(x: 9, y: 9)
            Path { p in p.move(to: CGPoint(x: 6,y: 18)); p.addLines([CGPoint(x: 9,y: 24),CGPoint(x: 12,y: 18)]); p.closeSubpath() }.fill(palette.accent)
        }
    }
}

import SwiftUI

struct SpaceArtwork: View {
    let theme: QueueThemeID
    let palette: QueuePalette
    let hasActiveRequest: Bool

    let routePosition: Double?
    // Fixed lifecycle stops come from QueueModel, independent of theme or display wording.
    private var stop: CGFloat { CGFloat(routePosition ?? 0.27) }

    var body: some View {
        GeometryReader { geometry in
            let w = geometry.size.width
            let h = geometry.size.height
            let start = CGPoint(x: w * 0.13, y: h * 0.28)
            let end = CGPoint(x: w * 0.87, y: h * 0.75)
            let c1 = CGPoint(x: w * 0.31, y: h * 0.86)
            let c2 = CGPoint(x: w * 0.67, y: h * 0.05)
            let point = cubic(start, c1, c2, end, stop)
            ZStack {
                if theme == .orbital {
                    Canvas { context, size in
                        let stars: [(CGFloat, CGFloat, CGFloat)] = [(0.08, 0.72, 1), (0.25, 0.10, 1.2), (0.39, 0.30, 0.8), (0.57, 0.13, 1), (0.74, 0.34, 0.8), (0.93, 0.13, 1.1), (0.48, 0.83, 0.8), (0.14, 0.94, 0.8), (0.91, 0.48, 0.7)]
                        for (x, y, r) in stars {
                            context.fill(Path(ellipseIn: CGRect(x: size.width * x, y: size.height * y, width: r * 2, height: r * 2)), with: .color(palette.text.opacity(0.48)))
                        }
                    }
                    Earth(palette: palette).frame(width: 67, height: 67).position(x: end.x + 8, y: end.y + 9)
                    Satellite(palette: palette).frame(width: 32, height: 32).position(start)
                } else {
                    Circle().stroke(palette.line, lineWidth: 1).frame(width: 15, height: 15).position(start)
                    Circle().stroke(palette.line, lineWidth: 1).frame(width: 25, height: 25).position(end)
                }
                Path { path in
                    path.move(to: start)
                    path.addCurve(to: end, control1: c1, control2: c2)
                }.stroke(palette.line, style: StrokeStyle(lineWidth: 1, dash: [3, 5]))
                if hasActiveRequest {
                    Circle().fill(palette.accent.opacity(0.16)).frame(width: 22, height: 22).position(point)
                    Circle().fill(palette.accent).frame(width: 7, height: 7).position(point)
                    if theme == .orbital {
                        Rocket(palette: palette).frame(width: 18, height: 24).rotationEffect(.degrees(45)).position(x: point.x + 18, y: point.y - 15)
                    }
                }
            }
        }
        .accessibilityElement(children: .ignore)
    }

    private func cubic(_ a: CGPoint, _ b: CGPoint, _ c: CGPoint, _ d: CGPoint, _ t: CGFloat) -> CGPoint {
        let u = 1 - t
        return CGPoint(x: u*u*u*a.x + 3*u*u*t*b.x + 3*u*t*t*c.x + t*t*t*d.x,
                       y: u*u*u*a.y + 3*u*u*t*b.y + 3*u*t*t*c.y + t*t*t*d.y)
    }
}

private struct Earth: View {
    let palette: QueuePalette
    var body: some View {
        ZStack {
            Circle().fill(palette.surface).overlay(Circle().stroke(palette.accent.opacity(0.65), lineWidth: 1))
            Path { path in
                path.move(to: CGPoint(x: 8, y: 19)); path.addLines([CGPoint(x: 16, y: 12), CGPoint(x: 25, y: 14), CGPoint(x: 29, y: 20), CGPoint(x: 24, y: 23), CGPoint(x: 22, y: 29), CGPoint(x: 17, y: 27), CGPoint(x: 14, y: 23)]); path.closeSubpath()
                path.move(to: CGPoint(x: 24, y: 31)); path.addLines([CGPoint(x: 31, y: 32), CGPoint(x: 35, y: 38), CGPoint(x: 32, y: 45), CGPoint(x: 27, y: 53), CGPoint(x: 23, y: 47), CGPoint(x: 24, y: 40), CGPoint(x: 20, y: 35)]); path.closeSubpath()
                path.move(to: CGPoint(x: 43, y: 15)); path.addLines([CGPoint(x: 53, y: 17), CGPoint(x: 58, y: 23), CGPoint(x: 53, y: 27), CGPoint(x: 47, y: 25), CGPoint(x: 43, y: 30), CGPoint(x: 38, y: 25), CGPoint(x: 39, y: 20)]); path.closeSubpath()
                path.move(to: CGPoint(x: 47, y: 39)); path.addLines([CGPoint(x: 54, y: 37), CGPoint(x: 59, y: 41), CGPoint(x: 55, y: 47), CGPoint(x: 48, y: 46)]); path.closeSubpath()
            }.fill(palette.accent.opacity(0.48)).clipShape(Circle())
            Path { path in
                path.move(to: CGPoint(x: 8, y: 24)); path.addCurve(to: CGPoint(x: 55, y: 18), control1: CGPoint(x: 23, y: 12), control2: CGPoint(x: 39, y: 32))
                path.move(to: CGPoint(x: 4, y: 39)); path.addCurve(to: CGPoint(x: 61, y: 38), control1: CGPoint(x: 22, y: 30), control2: CGPoint(x: 42, y: 48))
                path.move(to: CGPoint(x: 20, y: 5)); path.addCurve(to: CGPoint(x: 34, y: 64), control1: CGPoint(x: 10, y: 23), control2: CGPoint(x: 25, y: 52))
            }.stroke(palette.accent.opacity(0.35), lineWidth: 1).clipShape(Circle())
        }
    }
}

private struct Satellite: View {
    let palette: QueuePalette
    var body: some View {
        ZStack {
            RoundedRectangle(cornerRadius: 2).fill(palette.text).frame(width: 10, height: 10)
            Rectangle().stroke(palette.accent, lineWidth: 1).frame(width: 9, height: 12).offset(x: -12)
            Rectangle().stroke(palette.accent, lineWidth: 1).frame(width: 9, height: 12).offset(x: 12)
            Path { p in p.move(to: CGPoint(x: 16, y: 11)); p.addLine(to: CGPoint(x: 20, y: 4)) }.stroke(palette.text, lineWidth: 1)
        }.rotationEffect(.degrees(-25))
    }
}

private struct Rocket: View {
    let palette: QueuePalette
    var body: some View {
        ZStack {
            Path { p in
                p.move(to: CGPoint(x: 9, y: 0))
                p.addCurve(to: CGPoint(x: 15, y: 16), control1: CGPoint(x: 15, y: 5), control2: CGPoint(x: 16, y: 11))
                p.addLine(to: CGPoint(x: 3, y: 16))
                p.addCurve(to: CGPoint(x: 9, y: 0), control1: CGPoint(x: 2, y: 11), control2: CGPoint(x: 3, y: 5))
                p.closeSubpath()
            }.fill(palette.text)
            Path { p in
                p.move(to: CGPoint(x: 3, y: 11)); p.addLines([CGPoint(x: 0, y: 16), CGPoint(x: 0, y: 20), CGPoint(x: 5, y: 17)])
                p.move(to: CGPoint(x: 15, y: 11)); p.addLines([CGPoint(x: 18, y: 16), CGPoint(x: 18, y: 20), CGPoint(x: 13, y: 17)])
            }.fill(palette.accent)
            Circle().fill(palette.background).frame(width: 4, height: 4).position(x: 9, y: 9)
            Path { p in p.move(to: CGPoint(x: 6, y: 18)); p.addLines([CGPoint(x: 9, y: 24), CGPoint(x: 12, y: 18)]); p.closeSubpath() }.fill(palette.accent.opacity(0.8))
        }
    }
}

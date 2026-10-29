import SwiftUI

enum QueueThemeID: String, CaseIterable, Identifiable {
    case orbital, minimal
    var id: String { rawValue }
    var title: String { self == .orbital ? "Orbital" : "Minimal" }
}

struct QueuePalette {
    let background: Color
    let surface: Color
    let text: Color
    let muted: Color
    let accent: Color
    let line: Color
    let danger: Color

    static func forTheme(_ theme: QueueThemeID) -> QueuePalette {
        switch theme {
        case .orbital:
            return QueuePalette(background: Color(red: 0.035, green: 0.065, blue: 0.13), surface: Color(red: 0.075, green: 0.12, blue: 0.20), text: Color(red: 0.96, green: 0.94, blue: 0.85), muted: Color(red: 0.59, green: 0.68, blue: 0.73), accent: Color(red: 0.45, green: 0.84, blue: 0.87), line: Color(red: 0.24, green: 0.40, blue: 0.48), danger: Color(red: 0.96, green: 0.57, blue: 0.48))
        case .minimal:
            return QueuePalette(background: Color(red: 0.10, green: 0.12, blue: 0.15), surface: Color(red: 0.16, green: 0.18, blue: 0.21), text: Color(red: 0.95, green: 0.95, blue: 0.92), muted: Color(red: 0.65, green: 0.68, blue: 0.70), accent: Color(red: 0.62, green: 0.82, blue: 0.80), line: Color(red: 0.32, green: 0.38, blue: 0.40), danger: Color(red: 0.94, green: 0.60, blue: 0.53))
        }
    }
}

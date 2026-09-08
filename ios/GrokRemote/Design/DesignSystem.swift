//
//  DesignSystem.swift
//  GrokRemote
//
//  Ported verbatim from Pianotuner v2.0 (Core/DesignSystem.swift) so both apps
//  render from one set of tokens, whose values come from the shared website
//  tokens.css. Two additions are marked GROK REMOTE below; everything else is
//  unchanged on purpose -- divergence here is how two apps stop looking related.
//
//  App 的唯一视觉真相源。数值逐字来自官网设计系统
//  `pianotuner-site/app/src/styles/tokens.css`（与 melspectrum-website 逐字节相同）：
//
//      ONE accent · zero gradients · zero shadows · square corners ·
//      1px dashed hairlines · 300 细体大标题 · tabular-nums 数字 · 无 emoji
//
//  纪律（AGENTS.md 会引用这里）：
//    · 视图**禁止**直接写 `.gray / .white / .black / Color(red:…)`，一律走 `DS.Color.*`
//    · monospace **只给数字**（`.dsMetric()`），标签永远不用 monospace、不大写、不加字距
//    · Liquid Glass 只存在于 `DSPrimaryButton` 与 `DSBackButton` 两个组件里，别处不许用
//    · 全项目零 `repeatForever`、零 `.shadow`、零 emoji（SF Symbols 用 `.monochrome`）
//

import SwiftUI
import UIKit

enum DS {

    // MARK: - 颜色（随系统深浅色自适应；`onAccent` 与 `textTertiary` 两套相同）

    enum Color {
        /// 页面底：platinum / black
        static let bg            = dynamic(light: 0xECECEC, dark: 0x141414)
        /// 卡片面：porcelain / 官网无深色卡片值，取 bg 上抬一阶
        static let surface       = dynamic(light: 0xF5F4F2, dark: 0x1C1C1C)
        static let text          = dynamic(light: 0x141414, dark: 0xFAFAFA)
        static let textSecondary = dynamic(light: 0x5A5A5A, dark: 0xB3B3B3)
        static let textTertiary  = dynamic(light: 0x8D8D8D, dark: 0x8D8D8D)
        /// 1px 虚线细线：alabaster / gunmetal
        static let hairline      = dynamic(light: 0xD9D9D9, dark: 0x414141)
        /// 唯一强调色。浅底用 accent-deep 保证对比度
        static let accent        = dynamic(light: 0x14B8A6, dark: 0x2DD4BF)
        /// 压在 accent 上的文字。白字压 #14B8A6 只有 2.9:1，所以恒用近黑
        static let onAccent      = dynamic(light: 0x141414, dark: 0x141414)
        /// 唯一非调色板色：断弦级危险
        static let danger        = SwiftUI.Color(uiColor: .systemRed)

        static func dynamic(light: UInt32, dark: UInt32) -> SwiftUI.Color {
            SwiftUI.Color(uiColor: UIColor { trait in
                trait.userInterfaceStyle == .dark ? UIColor(hex: dark) : UIColor(hex: light)
            })
        }
    }

    // MARK: - 字体（只用语义字号，Dynamic Type 免费得到）

    enum Font {
        /// 大标题：细体 + 负字距（官网 t-h2 的 iOS 映射）
        static let display  = SwiftUI.Font.system(.largeTitle, design: .default, weight: .light)
        static let title    = SwiftUI.Font.system(.title2, design: .default, weight: .regular)
        static let headline = SwiftUI.Font.system(.headline, design: .default, weight: .medium)
        static let body     = SwiftUI.Font.system(.body, design: .default, weight: .regular)
        static let callout  = SwiftUI.Font.system(.callout, design: .default, weight: .regular)
        static let footnote = SwiftUI.Font.system(.footnote, design: .default, weight: .regular)
        /// 标签：**不大写、不加字距、不 monospace**
        static let label    = SwiftUI.Font.system(.caption, design: .default, weight: .medium)
        /// 英雄数字（音分 / 音名）。调用方用 `@ScaledMetric` 给 size，再叠 `.dsMetric()`
        static func hero(_ size: CGFloat) -> SwiftUI.Font {
            SwiftUI.Font.system(size: size, weight: .thin, design: .default)
        }
        /// 度量数字（token 计数 / 耗时 / 上下文占用）
        static let metric = SwiftUI.Font.system(.title3, design: .default, weight: .light)

        // MARK: GROK REMOTE 追加
        /// 代码块与命令名。**这是 monospace 的唯一合法用途**——
        /// 本设计系统禁止 monospace 出现在标签上（数字用 `.dsMetric()`），
        /// 但代码不是标签，等宽是它的语义。别把这条当成"可以随便用等宽"的授权。
        static let code = SwiftUI.Font.system(.footnote, design: .monospaced)
        /// 行内代码 / 命令名，比 code 再小一档
        static let codeInline = SwiftUI.Font.system(.caption, design: .monospaced)
    }

    // MARK: - 间距 / 形状

    enum Space {
        static let xs: CGFloat = 4
        static let s:  CGFloat = 8
        static let m:  CGFloat = 16
        static let l:  CGFloat = 24
        static let xl: CGFloat = 32
        static let xxl: CGFloat = 48
    }

    enum Radius {
        /// 卡片直角（官网 border-radius: 0）
        static let card: CGFloat = 0
        /// 按钮 4pt 微圆角（用户拍板：避免 iOS 上生硬）
        static let button: CGFloat = 4
    }

    /// 主按钮高度；触控目标下限 44pt
    static let controlHeight: CGFloat = 52
    static let minTapTarget: CGFloat = 44

    // MARK: - 动效（临界阻尼为默认；只有手势带动量时才允许回弹）

    enum Motion {
        /// 进场 / 布局变化
        static let enter  = Animation.spring(response: 0.45, dampingFraction: 1.0)
        /// 数值 / 指示器落位
        static let settle = Animation.spring(response: 0.35, dampingFraction: 1.0)
        /// 按压反馈（Apple 值）
        static let press  = Animation.spring(response: 0.26, dampingFraction: 0.82)
        /// 屏幕切换
        static let screen = Animation.spring(response: 0.5, dampingFraction: 0.9)
        /// 交错进场每项间隔
        static let staggerStep: Double = 0.08

        #if DEBUG
        /// 模拟器验证 reduce-motion 用（`accessibilityReduceMotion` 环境值是只读的）
        static var forceReducedMotion = false
        #endif
    }

    enum Transition {
        /// 屏幕切换：同路进出，对称
        static let screen: AnyTransition = .opacity.combined(with: .offset(y: 12))
        /// 覆盖层（横幅 / 卡片）
        static let overlay: AnyTransition = .opacity.combined(with: .offset(y: 8))
    }

    // MARK: - 图标白名单（SF Symbols，单色，细线）

    enum Icon {
        static let back      = "chevron.left"
        static let forward   = "arrow.right"
        static let help      = "questionmark.circle"
        static let check     = "checkmark"
        static let warning   = "exclamationmark.triangle"
        static let mic       = "mic"
        static let device    = "antenna.radiowaves.left.and.right"
        static let close     = "xmark"
        static let history   = "clock"
        static let document  = "doc.text"
        static let settings  = "slider.horizontal.3"

        // MARK: GROK REMOTE 追加（同样 .monochrome + .light，禁 .fill 变体）
        static let folder    = "folder"
        static let chevron   = "chevron.right"
        static let terminal  = "terminal"
        static let tool      = "hammer"
        static let copy      = "doc.on.doc"
        static let stop      = "stop.circle"
        static let send      = "arrow.up"
        static let command   = "slash.circle"
        static let gear      = "gearshape"
        static let qr        = "qrcode.viewfinder"
        static let repository = "shippingbox"
        static let branch    = "arrow.triangle.branch"
        static let offline   = "wifi.slash"
        static let plus      = "plus"
        static let think     = "sparkle"
        static let image     = "photo"
    }

    // MARK: - 状态语义（只有 inTune 得青色）

    enum Status: Equatable {
        case idle, active, inTune, warning, danger

        var tint: SwiftUI.Color {
            switch self {
            case .idle:    return DS.Color.textTertiary
            case .active:  return DS.Color.text
            case .inTune:  return DS.Color.accent
            case .warning: return DS.Color.text
            case .danger:  return DS.Color.danger
            }
        }
        /// 强调态用实线框，其余虚线
        var isEmphasized: Bool {
            switch self {
            case .idle, .active: return false
            case .inTune, .warning, .danger: return true
            }
        }
        var borderColor: SwiftUI.Color {
            switch self {
            case .idle, .active: return DS.Color.hairline
            case .inTune:  return DS.Color.accent
            case .warning: return DS.Color.text
            case .danger:  return DS.Color.danger
            }
        }
    }
}

// MARK: - UIColor hex

extension UIColor {
    convenience init(hex: UInt32) {
        self.init(red:   CGFloat((hex >> 16) & 0xFF) / 255.0,
                  green: CGFloat((hex >> 8) & 0xFF) / 255.0,
                  blue:  CGFloat(hex & 0xFF) / 255.0,
                  alpha: 1.0)
    }
}

// MARK: - 修饰符

/// 1px 虚线细线框（官网 `border: 1px dashed`）。实线 = 强调态。
struct DSHairline: ViewModifier {
    var color: Color = DS.Color.hairline
    var solid: Bool = false
    func body(content: Content) -> some View {
        content.overlay(
            RoundedRectangle(cornerRadius: DS.Radius.card)
                .strokeBorder(color, style: StrokeStyle(lineWidth: 1, dash: solid ? [] : [3, 3]))
        )
    }
}

/// 卡片：surface 底 + 虚线框 + 16pt 内边距。零阴影。
struct DSCard: ViewModifier {
    var status: DS.Status = .idle
    var padding: CGFloat = DS.Space.m
    func body(content: Content) -> some View {
        content
            .padding(padding)
            .background(DS.Color.surface)
            .modifier(DSHairline(color: status.borderColor, solid: status.isEmphasized))
    }
}

/// 交错进场：opacity 0→1 + y 8→0，每项延迟 index × 80ms。尊重 reduce-motion。
struct DSReveal: ViewModifier {
    let index: Int
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @State private var shown = false

    private var reduced: Bool {
        #if DEBUG
        return reduceMotion || DS.Motion.forceReducedMotion
        #else
        return reduceMotion
        #endif
    }

    func body(content: Content) -> some View {
        content
            .opacity(shown ? 1 : 0)
            .offset(y: shown || reduced ? 0 : 8)
            .onAppear {
                let delay = Double(index) * DS.Motion.staggerStep
                if reduced {
                    withAnimation(.easeOut(duration: 0.2).delay(delay)) { shown = true }
                } else {
                    withAnimation(DS.Motion.enter.delay(delay)) { shown = true }
                }
            }
    }
}

extension View {
    func dsHairline(_ color: Color = DS.Color.hairline, solid: Bool = false) -> some View {
        modifier(DSHairline(color: color, solid: solid))
    }
    func dsCard(_ status: DS.Status = .idle, padding: CGFloat = DS.Space.m) -> some View {
        modifier(DSCard(status: status, padding: padding))
    }
    func dsReveal(_ index: Int = 0) -> some View {
        modifier(DSReveal(index: index))
    }
    /// 度量数字：等宽数字 + 数值变化过渡。**只用于数字。**
    func dsMetric() -> some View {
        self.monospacedDigit().contentTransition(.numericText())
    }
    /// 大标题的负字距（display 专用）
    func dsDisplayTracking() -> some View {
        self.tracking(-1)
    }
}

/// 水平线 Shape（strokeBorder 在 1pt 高的矩形上会把上下两条边叠成实线，所以单独画一条路径）
struct DSHLine: Shape {
    func path(in rect: CGRect) -> Path {
        var p = Path()
        p.move(to: CGPoint(x: rect.minX, y: rect.midY))
        p.addLine(to: CGPoint(x: rect.maxX, y: rect.midY))
        return p
    }
}

/// 1pt 虚线分隔线
struct DSDivider: View {
    var body: some View {
        DSHLine()
            .stroke(DS.Color.hairline, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
            .frame(height: 1)
    }
}

// MARK: - 按钮

struct DSButtonStyle: ButtonStyle {
    enum Kind { case primary, secondary, quiet, danger }
    var kind: Kind = .primary
    @Environment(\.isEnabled) private var isEnabled

    func makeBody(configuration: Configuration) -> some View {
        configuration.label
            .font(DS.Font.headline)
            .foregroundStyle(foreground)
            .frame(maxWidth: kind == .quiet ? nil : .infinity)
            .frame(minHeight: kind == .quiet ? DS.minTapTarget : DS.controlHeight)
            .padding(.horizontal, kind == .quiet ? DS.Space.s : DS.Space.m)
            .background(background)
            .overlay(border)
            .contentShape(Rectangle())
            .scaleEffect(configuration.isPressed ? 0.97 : 1.0)
            .opacity(isEnabled ? 1.0 : 0.4)
            .animation(DS.Motion.press, value: configuration.isPressed)
    }

    private var foreground: Color {
        switch kind {
        case .primary:   return DS.Color.onAccent
        case .secondary: return DS.Color.text
        case .quiet:     return DS.Color.textSecondary
        case .danger:    return .white
        }
    }
    @ViewBuilder private var background: some View {
        switch kind {
        case .primary:   RoundedRectangle(cornerRadius: DS.Radius.button).fill(DS.Color.accent)
        case .danger:    RoundedRectangle(cornerRadius: DS.Radius.button).fill(DS.Color.danger)
        case .secondary, .quiet: Color.clear
        }
    }
    @ViewBuilder private var border: some View {
        if kind == .secondary {
            RoundedRectangle(cornerRadius: DS.Radius.button)
                .strokeBorder(DS.Color.hairline, style: StrokeStyle(lineWidth: 1, dash: [3, 3]))
        }
    }
}

/// 主按钮 —— Liquid Glass 的两个合法落点之一。
/// iOS 26+ 用 `.glassProminent`，否则 accent 实底 + spring。
struct DSPrimaryButton: View {
    let title: String
    var systemImage: String? = nil
    var isEnabled: Bool = true
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            HStack(spacing: DS.Space.s) {
                Text(title)
                if let systemImage {
                    Image(systemName: systemImage).symbolRenderingMode(.monochrome)
                }
            }
            .font(DS.Font.headline)
            .frame(maxWidth: .infinity)
            .frame(minHeight: DS.controlHeight)
        }
        .modifier(DSPrimaryGlass())
        .disabled(!isEnabled)
    }
}

private struct DSPrimaryGlass: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            content
                .buttonStyle(.glassProminent)
                .buttonBorderShape(.roundedRectangle(radius: DS.Radius.button))
                .tint(DS.Color.accent)
                .foregroundStyle(DS.Color.onAccent)
        } else {
            content.buttonStyle(DSButtonStyle(kind: .primary))
        }
    }
}

/// 返回键 —— Liquid Glass 的另一个合法落点。
struct DSBackButton: View {
    var accessibilityLabel: String = "Back"
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: DS.Icon.back)
                .symbolRenderingMode(.monochrome)
                .font(.system(size: 17, weight: .light))
                .foregroundStyle(DS.Color.text)
                .frame(width: DS.minTapTarget, height: DS.minTapTarget)
                .contentShape(Rectangle())
        }
        .modifier(DSBackGlass())
        .accessibilityLabel(accessibilityLabel)
    }
}

private struct DSBackGlass: ViewModifier {
    func body(content: Content) -> some View {
        if #available(iOS 26.0, *) {
            content
                .buttonStyle(.plain)
                .glassEffect(.regular.interactive(), in: .rect(cornerRadius: DS.Radius.button))
        } else {
            content
                .buttonStyle(.plain)
                .background(DS.Color.surface)
                .dsHairline()
        }
    }
}

import SwiftUI

/// Shared pieces, built only from DS tokens.
///
/// Anything reaching for a literal colour or a magic number belongs here first,
/// so the rule "views never hardcode style" stays enforceable.

// MARK: - Section label

struct DSSectionLabel: View {
    let text: String
    var body: some View {
        Text(text)
            .font(DS.Font.label)
            .foregroundStyle(DS.Color.textSecondary)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: - Chip

struct DSChip: View {
    let text: String
    var systemImage: String?
    var status: DS.Status = .idle

    var body: some View {
        HStack(spacing: DS.Space.xs) {
            if let systemImage {
                Image(systemName: systemImage)
                    .symbolRenderingMode(.monochrome)
                    .font(.system(size: 10, weight: .light))
            }
            Text(text).font(DS.Font.label)
        }
        .foregroundStyle(status == .idle ? DS.Color.textSecondary : status.tint)
        .padding(.horizontal, DS.Space.s)
        .padding(.vertical, DS.Space.xs)
        .overlay(
            RoundedRectangle(cornerRadius: DS.Radius.button)
                .strokeBorder(status.borderColor,
                              style: StrokeStyle(lineWidth: 1,
                                                 dash: status.isEmphasized ? [] : [3, 3]))
        )
    }
}

// MARK: - Status line

struct DSStatusLine: View {
    let text: String
    var status: DS.Status = .idle

    var body: some View {
        HStack(spacing: DS.Space.s) {
            Circle().fill(status.tint).frame(width: 6, height: 6)
            Text(text)
                .font(DS.Font.footnote)
                .foregroundStyle(DS.Color.textSecondary)
            Spacer(minLength: 0)
        }
    }
}

// MARK: - Empty state

struct DSEmptyState: View {
    let title: String
    let systemImage: String
    var detail: String?
    var action: (title: String, run: () -> Void)?

    var body: some View {
        VStack(spacing: DS.Space.m) {
            Image(systemName: systemImage)
                .symbolRenderingMode(.monochrome)
                .font(.system(size: 32, weight: .thin))
                .foregroundStyle(DS.Color.textTertiary)
            Text(title).font(DS.Font.title).foregroundStyle(DS.Color.text)
            if let detail {
                Text(detail)
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textSecondary)
                    .multilineTextAlignment(.center)
            }
            if let action {
                Button(action.title, action: action.run)
                    .buttonStyle(DSButtonStyle(kind: .secondary))
                    .frame(maxWidth: 220)
                    .padding(.top, DS.Space.s)
            }
        }
        .padding(DS.Space.xl)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

// MARK: - Screen scaffold

struct DSScreen<Content: View>: View {
    let title: String
    var subtitle: String?
    var leading: AnyView?
    var trailing: AnyView?
    @ViewBuilder var content: () -> Content

    var body: some View {
        VStack(spacing: 0) {
            HStack(alignment: .firstTextBaseline, spacing: DS.Space.s) {
                if let leading { leading }
                VStack(alignment: .leading, spacing: 2) {
                    Text(title)
                        .font(DS.Font.headline)
                        .foregroundStyle(DS.Color.text)
                        .lineLimit(1)
                    if let subtitle {
                        Text(subtitle)
                            .font(DS.Font.footnote)
                            .foregroundStyle(DS.Color.textTertiary)
                            .lineLimit(1)
                    }
                }
                Spacer(minLength: 0)
                if let trailing { trailing }
            }
            .padding(.horizontal, DS.Space.l)
            .padding(.vertical, DS.Space.m)

            DSDivider().padding(.horizontal, DS.Space.l)
            content()
        }
        .background(DS.Color.bg)
    }
}

// MARK: - Icon button

struct DSIconButton: View {
    let systemName: String
    let accessibilityLabel: String
    var status: DS.Status = .active
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: systemName)
                .symbolRenderingMode(.monochrome)
                .font(.system(size: 17, weight: .light))
                .foregroundStyle(status.tint)
                .frame(width: DS.minTapTarget, height: DS.minTapTarget)
                .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(accessibilityLabel)
    }
}

// MARK: - Text field

struct DSTextField: View {
    let placeholder: String
    @Binding var text: String
    var systemImage: String?
    var autocapitalization: TextInputAutocapitalization = .never

    var body: some View {
        HStack(spacing: DS.Space.s) {
            if let systemImage {
                Image(systemName: systemImage)
                    .symbolRenderingMode(.monochrome)
                    .font(.system(size: 14, weight: .light))
                    .foregroundStyle(DS.Color.textTertiary)
            }
            TextField(placeholder, text: $text)
                .font(DS.Font.body)
                .foregroundStyle(DS.Color.text)
                .textInputAutocapitalization(autocapitalization)
                .autocorrectionDisabled()
        }
        .padding(.horizontal, DS.Space.m)
        .frame(minHeight: DS.controlHeight)
        .background(DS.Color.surface)
        .dsHairline()
    }
}

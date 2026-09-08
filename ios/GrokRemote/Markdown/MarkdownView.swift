import SwiftUI

/// Renders parsed Markdown blocks with DS tokens.
struct MarkdownView: View {
    let source: String
    /// While a turn streams, re-parsing the whole document on every token is
    /// what makes long answers stutter. The caller throttles instead; this view
    /// simply renders whatever it is handed.
    var body: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            ForEach(MarkdownParser.parse(source)) { block in
                blockView(block)
            }
        }
    }

    @ViewBuilder
    private func blockView(_ block: MarkdownBlock) -> some View {
        switch block {
        case .heading(let level, let text):
            Text(AttributedString.inlineMarkdown(text))
                .font(level <= 2 ? DS.Font.title : DS.Font.headline)
                .foregroundStyle(DS.Color.text)
                .padding(.top, DS.Space.xs)

        case .paragraph(let text):
            Text(AttributedString.inlineMarkdown(text))
                .font(DS.Font.body)
                .foregroundStyle(DS.Color.text)
                .textSelection(.enabled)

        case .code(let language, let text):
            CodeBlock(language: language, code: text)

        case .bullet(let items, let ordered):
            VStack(alignment: .leading, spacing: DS.Space.xs) {
                ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                    HStack(alignment: .firstTextBaseline, spacing: DS.Space.s) {
                        Text(ordered ? "\(index + 1)." : "—")
                            .font(DS.Font.footnote)
                            .foregroundStyle(DS.Color.textTertiary)
                            .frame(minWidth: 16, alignment: .trailing)
                        Text(AttributedString.inlineMarkdown(item))
                            .font(DS.Font.body)
                            .foregroundStyle(DS.Color.text)
                    }
                }
            }

        case .quote(let text):
            HStack(alignment: .top, spacing: DS.Space.s) {
                Rectangle().fill(DS.Color.hairline).frame(width: 2)
                Text(AttributedString.inlineMarkdown(text))
                    .font(DS.Font.body)
                    .foregroundStyle(DS.Color.textSecondary)
            }

        case .table(let header, let rows):
            // Wide content scrolls inside its own container rather than forcing
            // the whole message to scroll sideways.
            ScrollView(.horizontal, showsIndicators: false) {
                VStack(alignment: .leading, spacing: 0) {
                    tableRow(header, isHeader: true)
                    ForEach(Array(rows.enumerated()), id: \.offset) { _, row in
                        DSDivider()
                        tableRow(row, isHeader: false)
                    }
                }
                .dsHairline()
            }

        case .rule:
            DSDivider().padding(.vertical, DS.Space.xs)
        }
    }

    private func tableRow(_ cells: [String], isHeader: Bool) -> some View {
        HStack(alignment: .top, spacing: DS.Space.m) {
            ForEach(Array(cells.enumerated()), id: \.offset) { _, cell in
                Text(AttributedString.inlineMarkdown(cell))
                    .font(isHeader ? DS.Font.label : DS.Font.footnote)
                    .foregroundStyle(isHeader ? DS.Color.text : DS.Color.textSecondary)
                    .frame(minWidth: 64, alignment: .leading)
            }
        }
        .padding(.horizontal, DS.Space.s)
        .padding(.vertical, DS.Space.s)
    }
}

struct CodeBlock: View {
    let language: String?
    let code: String
    @State private var copied = false

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack {
                Text(language?.uppercased() ?? "CODE")
                    .font(DS.Font.label)
                    .foregroundStyle(DS.Color.textTertiary)
                Spacer()
                Button {
                    UIPasteboard.general.string = code
                    copied = true
                    Task {
                        try? await Task.sleep(for: .seconds(1.5))
                        copied = false
                    }
                } label: {
                    HStack(spacing: DS.Space.xs) {
                        Image(systemName: copied ? DS.Icon.check : DS.Icon.copy)
                            .symbolRenderingMode(.monochrome)
                        Text(copied ? "Copied" : "Copy")
                    }
                    .font(DS.Font.label)
                    .foregroundStyle(copied ? DS.Color.accent : DS.Color.textSecondary)
                }
                .buttonStyle(.plain)
                .accessibilityLabel("Copy code")
            }
            .padding(.horizontal, DS.Space.s)
            .padding(.vertical, DS.Space.xs)

            DSDivider()

            ScrollView(.horizontal, showsIndicators: false) {
                Text(code)
                    .font(DS.Font.code)
                    .foregroundStyle(DS.Color.text)
                    .textSelection(.enabled)
                    .padding(DS.Space.s)
            }
        }
        .background(DS.Color.surface)
        .dsHairline()
    }
}

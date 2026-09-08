import SwiftUI

struct MessageView: View {
    let message: ChatMessage
    @State private var thoughtExpanded = false

    var body: some View {
        switch message.role {
        case "user":    userBubble
        case "system":  systemNote
        default:        assistantBody
        }
    }

    // The old version put `.frame(maxWidth: .infinity, alignment:)` on a view
    // that already had a background, so both roles rendered as full-width blocks
    // and the speaker was indistinguishable. Alignment belongs on the row.
    private var userBubble: some View {
        HStack {
            Spacer(minLength: DS.Space.xl)
            Text(message.text)
                .font(DS.Font.body)
                .foregroundStyle(DS.Color.text)
                .textSelection(.enabled)
                .padding(DS.Space.m)
                .background(DS.Color.surface)
                .overlay(
                    RoundedRectangle(cornerRadius: DS.Radius.card)
                        .strokeBorder(DS.Color.accent, lineWidth: 1))
        }
    }

    private var systemNote: some View {
        Text(message.text)
            .font(DS.Font.footnote)
            .foregroundStyle(DS.Color.textSecondary)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(DS.Space.s)
            .dsHairline()
    }

    private var assistantBody: some View {
        VStack(alignment: .leading, spacing: DS.Space.s) {
            if !message.thought.isEmpty {
                thoughtSection
            }
            ForEach(message.tools) { tool in
                ToolCard(tool: tool)
            }
            if !message.text.isEmpty {
                MarkdownView(source: message.text)
            }
            if message.isStreaming && message.text.isEmpty && message.tools.isEmpty {
                ProgressView().controlSize(.small).tint(DS.Color.accent)
            }
            if let error = message.errorMessage {
                Text(error)
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.danger)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .dsCard(.danger)
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var thoughtSection: some View {
        VStack(alignment: .leading, spacing: DS.Space.xs) {
            Button {
                withAnimation(DS.Motion.settle) { thoughtExpanded.toggle() }
            } label: {
                HStack(spacing: DS.Space.xs) {
                    Image(systemName: DS.Icon.think)
                        .symbolRenderingMode(.monochrome)
                        .font(.system(size: 11, weight: .light))
                    Text(message.isStreaming ? "Thinking…" : "Thought")
                        .font(DS.Font.label)
                    Image(systemName: thoughtExpanded ? "chevron.down" : DS.Icon.chevron)
                        .symbolRenderingMode(.monochrome)
                        .font(.system(size: 9, weight: .light))
                }
                .foregroundStyle(DS.Color.textTertiary)
            }
            .buttonStyle(.plain)

            if thoughtExpanded || message.isStreaming {
                HStack(alignment: .top, spacing: DS.Space.s) {
                    Rectangle().fill(DS.Color.hairline).frame(width: 1)
                    Text(message.thought)
                        .font(DS.Font.footnote)
                        .foregroundStyle(DS.Color.textTertiary)
                }
                .frame(maxHeight: message.isStreaming ? 90 : nil, alignment: .top)
                .clipped()
            }
        }
    }
}

struct ToolCard: View {
    let tool: ToolRun
    @State private var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: DS.Space.xs) {
            Button {
                withAnimation(DS.Motion.settle) { expanded.toggle() }
            } label: {
                HStack(spacing: DS.Space.s) {
                    Image(systemName: icon)
                        .symbolRenderingMode(.monochrome)
                        .font(.system(size: 12, weight: .light))
                        .foregroundStyle(status.tint)
                    Text(tool.label ?? tool.name)
                        .font(DS.Font.codeInline)
                        .foregroundStyle(DS.Color.text)
                    Spacer(minLength: 0)
                    if let code = tool.exitCode, code != 0 {
                        DSChip(text: "exit \(code)", status: .warning)
                    }
                    if tool.status == "running" {
                        ProgressView().controlSize(.mini).tint(DS.Color.accent)
                    }
                }
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .accessibilityLabel("\(tool.label ?? tool.name), \(tool.status)")

            if let title = tool.title, title != tool.name {
                Text(title)
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textSecondary)
                    .lineLimit(expanded ? nil : 1)
            }
            if expanded, !tool.paths.isEmpty {
                ForEach(tool.paths, id: \.self) { path in
                    Text(path)
                        .font(DS.Font.codeInline)
                        .foregroundStyle(DS.Color.textTertiary)
                        .lineLimit(1)
                        .truncationMode(.head)
                }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .dsCard(status, padding: DS.Space.s)
    }

    private var status: DS.Status {
        switch tool.status {
        case "completed": return .inTune
        case "failed", "error": return .warning
        case "running": return .active
        default: return .idle
        }
    }

    private var icon: String {
        switch tool.kind {
        case "read", "search": return DS.Icon.document
        case "edit": return DS.Icon.tool
        case "execute": return DS.Icon.terminal
        case "fetch": return DS.Icon.device
        case "think": return DS.Icon.think
        default: return DS.Icon.tool
        }
    }
}

import SwiftUI

/// The "/" palette.
///
/// Commands come from two places and the distinction is real, not cosmetic:
/// grok exposes 43 over ACP (mostly this machine's skills and plugins), while
/// /model, /effort, /status and friends exist only because the Bridge
/// implements them -- they are not on the protocol at all. Each row says which,
/// so the list is honest about what it is offering.
struct CommandPaletteView: View {
    let commands: [CommandSpec]
    let acpAvailable: Bool
    let onRun: (CommandSpec, String?) -> Void
    @Binding var query: String

    @State private var pendingArgument: CommandSpec?

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            if !acpAvailable {
                Text("Agent commands appear once the session is running.")
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textTertiary)
                    .padding(.horizontal, DS.Space.l)
                    .padding(.vertical, DS.Space.s)
                DSDivider().padding(.horizontal, DS.Space.l)
            }

            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(groups, id: \.0) { group, items in
                        DSSectionLabel(text: group)
                            .padding(.horizontal, DS.Space.l)
                            .padding(.top, DS.Space.s)
                        ForEach(items) { command in
                            row(command)
                        }
                    }
                }
                .padding(.bottom, DS.Space.m)
            }
            .frame(maxHeight: 340)
        }
        .background(DS.Color.surface)
        .dsHairline()
        .sheet(item: $pendingArgument) { command in
            CommandArgumentSheet(command: command) { value in
                pendingArgument = nil
                onRun(command, value)
            }
        }
    }

    private func row(_ command: CommandSpec) -> some View {
        Button {
            if command.takesArgument {
                pendingArgument = command
            } else {
                onRun(command, nil)
            }
        } label: {
            HStack(alignment: .firstTextBaseline, spacing: DS.Space.s) {
                Text("/\(command.name)")
                    .font(DS.Font.codeInline)
                    .foregroundStyle(DS.Color.text)
                if command.source == "acp" {
                    DSChip(text: "agent")
                }
                Text(command.description)
                    .font(DS.Font.footnote)
                    .foregroundStyle(DS.Color.textSecondary)
                    .lineLimit(1)
                Spacer(minLength: 0)
                if command.takesArgument {
                    Image(systemName: DS.Icon.chevron)
                        .symbolRenderingMode(.monochrome)
                        .font(.system(size: 11, weight: .light))
                        .foregroundStyle(DS.Color.textTertiary)
                }
            }
            .padding(.horizontal, DS.Space.l)
            .frame(minHeight: DS.minTapTarget)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .disabled(command.disabled == true)
        .opacity(command.disabled == true ? 0.4 : 1)
    }

    private var filtered: [CommandSpec] {
        let needle = query.trimmingCharacters(in: .whitespaces)
            .trimmingCharacters(in: CharacterSet(charactersIn: "/"))
            .lowercased()
        guard !needle.isEmpty else { return commands }
        return commands.filter {
            $0.name.lowercased().contains(needle)
                || $0.aliases.contains { $0.lowercased().contains(needle) }
                || $0.description.lowercased().contains(needle)
        }
    }

    private var groups: [(String, [CommandSpec])] {
        Dictionary(grouping: filtered, by: \.group)
            .map { ($0.key, $0.value.sorted { $0.name < $1.name }) }
            .sorted { $0.0 < $1.0 }
    }
}

/// Arguments are picked, not typed, wherever the Bridge told us the options --
/// nobody should be typing a model id on a phone.
struct CommandArgumentSheet: View {
    let command: CommandSpec
    let onSubmit: (String?) -> Void

    @Environment(\.dismiss) private var dismiss
    @State private var text = ""

    var body: some View {
        DSScreen(title: "/\(command.name)",
                 subtitle: command.description,
                 leading: AnyView(DSBackButton(accessibilityLabel: "Cancel") { dismiss() })) {
            ScrollView {
                VStack(alignment: .leading, spacing: DS.Space.m) {
                    if command.isEnum, let options = command.options, !options.isEmpty {
                        ForEach(options, id: \.value) { option in
                            Button { onSubmit(option.value) } label: {
                                VStack(alignment: .leading, spacing: DS.Space.xs) {
                                    Text(option.label ?? option.value)
                                        .font(DS.Font.headline)
                                        .foregroundStyle(DS.Color.text)
                                    if let detail = option.detail {
                                        Text(detail)
                                            .font(DS.Font.footnote)
                                            .foregroundStyle(DS.Color.textSecondary)
                                            .lineLimit(2)
                                    }
                                    if let window = option.contextWindow {
                                        Text("\(window / 1000)k context")
                                            .font(DS.Font.label)
                                            .foregroundStyle(DS.Color.textTertiary)
                                            .dsMetric()
                                    }
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .dsCard()
                            }
                            .buttonStyle(.plain)
                        }
                    } else if command.isEnum {
                        Text("This command has no options for the current model.")
                            .font(DS.Font.footnote)
                            .foregroundStyle(DS.Color.textSecondary)
                    } else {
                        DSTextField(placeholder: command.argHint ?? "argument", text: $text)
                        DSPrimaryButton(title: "Run", isEnabled: !text.isEmpty) {
                            onSubmit(text)
                        }
                    }
                }
                .padding(DS.Space.l)
            }
        }
        .background(DS.Color.bg)
    }
}

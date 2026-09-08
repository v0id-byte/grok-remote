import Foundation

/// A small block-level Markdown parser.
///
/// Foundation can parse full Markdown, but `Text(AttributedString)` only renders
/// a subset -- essentially the inline intents. Headings, lists, block quotes,
/// tables and fenced code all come back as attributes SwiftUI ignores, so a
/// model's answer renders as one undifferentiated paragraph with stray `#` and
/// backticks in it.
///
/// So: block structure is parsed here and rendered as real views, while inline
/// formatting (bold, italic, links, inline code) is still handed to
/// AttributedString, which does handle those. No third-party dependency, which
/// keeps this project at zero.
enum MarkdownBlock: Identifiable, Equatable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case code(language: String?, text: String)
    case bullet(items: [String], ordered: Bool)
    case quote(String)
    case table(header: [String], rows: [[String]])
    case rule

    var id: String {
        switch self {
        case .heading(let l, let t): return "h\(l):\(t.hashValue)"
        case .paragraph(let t):      return "p:\(t.hashValue)"
        case .code(let l, let t):    return "c:\(l ?? ""):\(t.hashValue)"
        case .bullet(let i, let o):  return "l\(o):\(i.hashValue)"
        case .quote(let t):          return "q:\(t.hashValue)"
        case .table(let h, let r):   return "t:\(h.hashValue):\(r.count)"
        case .rule:                  return "hr"
        }
    }
}

enum MarkdownParser {
    static func parse(_ source: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var listItems: [String] = []
        var listOrdered = false

        func flushParagraph() {
            let text = paragraph.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty { blocks.append(.paragraph(text)) }
            paragraph.removeAll()
        }
        func flushList() {
            if !listItems.isEmpty {
                blocks.append(.bullet(items: listItems, ordered: listOrdered))
                listItems.removeAll()
            }
        }
        func flushAll() { flushParagraph(); flushList() }

        let lines = source.components(separatedBy: .newlines)
        var index = 0

        while index < lines.count {
            let line = lines[index]
            let trimmed = line.trimmingCharacters(in: .whitespaces)

            // Fenced code. An unterminated fence still renders as code -- during
            // streaming the closing fence simply has not arrived yet.
            if trimmed.hasPrefix("```") {
                flushAll()
                let language = String(trimmed.dropFirst(3)).trimmingCharacters(in: .whitespaces)
                var body: [String] = []
                index += 1
                while index < lines.count,
                      !lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("```") {
                    body.append(lines[index])
                    index += 1
                }
                index += 1
                blocks.append(.code(language: language.isEmpty ? nil : language,
                                    text: body.joined(separator: "\n")))
                continue
            }

            if trimmed.isEmpty { flushAll(); index += 1; continue }

            if trimmed.hasPrefix("#") {
                let hashes = trimmed.prefix(while: { $0 == "#" }).count
                if hashes <= 6, trimmed.dropFirst(hashes).hasPrefix(" ") {
                    flushAll()
                    blocks.append(.heading(
                        level: hashes,
                        text: String(trimmed.dropFirst(hashes + 1))))
                    index += 1
                    continue
                }
            }

            if trimmed == "---" || trimmed == "***" || trimmed == "___" {
                flushAll(); blocks.append(.rule); index += 1; continue
            }

            if trimmed.hasPrefix(">") {
                flushAll()
                var quoted: [String] = []
                while index < lines.count,
                      lines[index].trimmingCharacters(in: .whitespaces).hasPrefix(">") {
                    let body = lines[index].trimmingCharacters(in: .whitespaces).dropFirst()
                    quoted.append(body.trimmingCharacters(in: .whitespaces))
                    index += 1
                }
                blocks.append(.quote(quoted.joined(separator: "\n")))
                continue
            }

            // Table: a header row followed by a |---|---| separator.
            if trimmed.hasPrefix("|"), index + 1 < lines.count,
               isSeparatorRow(lines[index + 1]) {
                flushAll()
                let header = cells(trimmed)
                var rows: [[String]] = []
                index += 2
                while index < lines.count,
                      lines[index].trimmingCharacters(in: .whitespaces).hasPrefix("|") {
                    // Clamp to the header width: a model that omits the newline
                    // before trailing prose (seen from grok) yields a row with an
                    // extra cell, which would otherwise render as a phantom column.
                    var row = cells(lines[index].trimmingCharacters(in: .whitespaces))
                    if row.count > header.count { row = Array(row.prefix(header.count)) }
                    while row.count < header.count { row.append("") }
                    rows.append(row)
                    index += 1
                }
                blocks.append(.table(header: header, rows: rows))
                continue
            }

            if let item = bulletItem(trimmed) {
                flushParagraph()
                if !listItems.isEmpty && listOrdered != item.ordered { flushList() }
                listOrdered = item.ordered
                listItems.append(item.text)
                index += 1
                continue
            }

            flushList()
            paragraph.append(line)
            index += 1
        }

        flushAll()
        return blocks
    }

    private static func bulletItem(_ line: String) -> (text: String, ordered: Bool)? {
        for marker in ["- ", "* ", "+ "] where line.hasPrefix(marker) {
            return (String(line.dropFirst(2)), false)
        }
        // "1. text"
        let parts = line.split(separator: ".", maxSplits: 1, omittingEmptySubsequences: false)
        if parts.count == 2, let first = parts.first, Int(first) != nil,
           parts[1].hasPrefix(" ") {
            return (String(parts[1].dropFirst()), true)
        }
        return nil
    }

    private static func isSeparatorRow(_ line: String) -> Bool {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard trimmed.hasPrefix("|") else { return false }
        let body = trimmed.trimmingCharacters(in: CharacterSet(charactersIn: "|"))
        guard !body.isEmpty else { return false }
        return body.allSatisfy { $0 == "-" || $0 == ":" || $0 == "|" || $0 == " " }
    }

    private static func cells(_ row: String) -> [String] {
        row.trimmingCharacters(in: CharacterSet(charactersIn: "|"))
            .components(separatedBy: "|")
            .map { $0.trimmingCharacters(in: .whitespaces) }
    }
}

extension AttributedString {
    /// Inline formatting only. Block syntax is handled by MarkdownParser, and
    /// asking Foundation for full-document parsing here would silently drop the
    /// structure SwiftUI cannot render anyway.
    static func inlineMarkdown(_ source: String) -> AttributedString {
        (try? AttributedString(
            markdown: source,
            options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(source)
    }
}

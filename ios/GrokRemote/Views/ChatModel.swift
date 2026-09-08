import Foundation
import Observation
import SwiftUI

/// One rendered turn.
struct ChatMessage: Identifiable, Equatable {
    let id: String
    var role: String
    var text: String = ""
    var thought: String = ""
    var tools: [ToolRun] = []
    var isStreaming: Bool = false
    var errorMessage: String?
    var stopReason: String?
}

struct ToolRun: Identifiable, Equatable {
    let id: String
    var name: String
    var label: String?
    var kind: String?
    var status: String
    var title: String?
    var readOnly: Bool?
    var paths: [String] = []
    var output: String = ""
    var exitCode: Int?
}

/// Drives one session's socket, transcript and commands.
///
/// The socket is session-scoped and resumable: a dropped connection is not a
/// cancellation, and reconnecting replays whatever was missed using the last
/// sequence number seen. iOS suspends backgrounded apps, so that path is
/// ordinary rather than exceptional.
/// NOTE: deliberately not `@MainActor`-isolated at the type level. With
/// `@Observable`, marking the whole class `@MainActor` stopped SwiftUI tracking
/// its properties here -- the model fetched history, commands and models
/// (confirmed in the Bridge's access log) and the view rendered none of it.
/// Every mutation still happens on the main actor: `start()` is called from
/// `.task`, and the Tasks it creates inherit that context.
@Observable
final class ChatModel {
    private(set) var messages: [ChatMessage] = []
    private(set) var commands: [CommandSpec] = []
    private(set) var acpAvailable = false
    private(set) var usage: Usage?
    private(set) var currentModel: String?
    private(set) var isStreaming = false
    private(set) var isConnected = false
    private(set) var hasMoreHistory = false
    var banner: String?
    var errorBanner: String?

    private var client: BridgeClient?
    private var session: ChatSession?
    private var socket: SessionSocket?
    private var pump: Task<Void, Never>?
    private var lastSeq = 0
    private var nextHistoryBefore: Int?
    private var contextWindow: Int?

    /// Streaming deltas arrive per token. Re-rendering Markdown on every one of
    /// them visibly stutters on long answers, so text accumulates here and is
    /// published on a timer instead.
    private var pendingText: [String: String] = [:]
    private var flushTask: Task<Void, Never>?
    private let flushInterval = Duration.milliseconds(50)

    func start(client: BridgeClient, session: ChatSession) async {
        self.client = client
        self.session = session
        await loadHistory()
        await loadCommands()
        connect()
    }

    func stop() {
        pump?.cancel()
        flushTask?.cancel()
        socket?.close()
        socket = nil
        isConnected = false
    }

    // MARK: History

    private func loadHistory() async {
        guard let client, let session else { return }
        let page: HistoryPage
        do {
            page = try await client.history(session.id, limit: 40)
        } catch {
            banner = "History failed: \(error.localizedDescription)"
            return
        }
        messages = page.messages.map { message in
            ChatMessage(id: UUID().uuidString, role: message.role, text: message.text,
                        tools: (message.toolCalls ?? []).map {
                            ToolRun(id: $0.id ?? UUID().uuidString,
                                    name: $0.name ?? "tool", status: "completed")
                        })
        }
        hasMoreHistory = page.hasMore
        nextHistoryBefore = page.nextBefore
    }

    func loadMoreHistory() async {
        guard let client, let session, let before = nextHistoryBefore else { return }
        guard let page = try? await client.history(session.id, limit: 40, before: before)
        else { return }
        let older = page.messages.map { message in
            ChatMessage(id: UUID().uuidString, role: message.role, text: message.text)
        }
        messages.insert(contentsOf: older, at: 0)
        hasMoreHistory = page.hasMore
        nextHistoryBefore = page.nextBefore
    }

    private func loadCommands() async {
        guard let client, let session else { return }
        guard let response = try? await client.commands(session.id) else { return }
        commands = response.commands
        acpAvailable = response.acpAvailable
        if let models = try? await client.models() {
            currentModel = currentModel ?? session.model ?? models.defaultModel
            contextWindow = models.models.first { $0.id == currentModel }?.contextWindow
        }
    }

    // MARK: Socket

    private func connect() {
        guard let client, let session else { return }
        pump?.cancel()
        pump = Task { [weak self] in
            guard let self else { return }
            // Reconnect for as long as this screen is on-screen. Each attempt
            // resumes from the last sequence number, so nothing is missed and
            // nothing arrives twice.
            var backoff = Duration.seconds(1)
            var firstConnect = true
            while !Task.isCancelled {
                do {
                    // Fresh entry tails (nil): the transcript is already loaded,
                    // so replaying the journal would double every past turn.
                    // A reconnect resumes exactly after the last seq seen.
                    let resumeFrom: Int? = firstConnect ? nil : self.lastSeq
                    firstConnect = false
                    let socket = try client.connect(sessionId: session.id,
                                                    afterSeq: resumeFrom)
                    self.socket = socket
                    for try await event in socket.events() {
                        if Task.isCancelled { return }
                        self.apply(event)
                    }
                } catch {
                    if Task.isCancelled { return }
                    self.isConnected = false
                    self.banner = "Reconnecting…"
                }
                if Task.isCancelled { return }
                try? await Task.sleep(for: backoff)
                backoff = min(backoff * 2, .seconds(15))
            }
        }
    }

    // MARK: Events

    private func apply(_ event: BridgeEvent) {
        if let seq = event.seq { lastSeq = max(lastSeq, seq) }

        switch event.type {
        case "hello.ack":
            isConnected = true
            banner = nil
            if let seq = event.currentSeq { lastSeq = max(lastSeq, seq) }
            isStreaming = (event.sessionState == "running")
            if isStreaming { ensureAssistant() }

        case "message.delta":
            let target = ensureAssistant()
            pendingText[target, default: ""] += event.data ?? ""
            scheduleFlush()

        case "message.thought":
            let target = ensureAssistant()
            if let index = messages.firstIndex(where: { $0.id == target }) {
                messages[index].thought += event.data ?? ""
            }

        case "tool.started":
            let target = ensureAssistant()
            guard let index = messages.firstIndex(where: { $0.id == target }) else { break }
            messages[index].tools.append(ToolRun(
                id: event.toolCallId ?? UUID().uuidString,
                name: event.tool ?? "tool", label: event.label, kind: event.kind,
                status: "running", title: event.title, readOnly: event.readOnly,
                paths: (event.locations ?? []).compactMap(\.path)))

        case "tool.output", "tool.finished":
            guard let id = event.toolCallId,
                  let messageIndex = messages.lastIndex(where: {
                      $0.tools.contains { $0.id == id } }),
                  let toolIndex = messages[messageIndex].tools
                      .firstIndex(where: { $0.id == id }) else { break }
            if event.type == "tool.finished" {
                messages[messageIndex].tools[toolIndex].status = event.status ?? "completed"
                messages[messageIndex].tools[toolIndex].exitCode = event.exitCode
            }
            if let paths = event.locations?.compactMap(\.path), !paths.isEmpty {
                messages[messageIndex].tools[toolIndex].paths = paths
            }

        case "message.usage":
            usage = event.usage

        case "message.done":
            flushNow()
            isStreaming = false
            if let index = messages.lastIndex(where: { $0.role == "assistant" }) {
                messages[index].isStreaming = false
                messages[index].stopReason = event.stopReason
                // grok delegates a file read to the client's fs capability and
                // does not always send a terminal tool_call_update for it before
                // the turn ends. Once the turn is done nothing can still be
                // running, so settle any tool left in-flight rather than leaving
                // a spinner turning forever.
                for i in messages[index].tools.indices
                where messages[index].tools[i].status == "running" {
                    messages[index].tools[i].status = "completed"
                }
            }
            if let usage = event.usage { self.usage = usage }
            banner = (event.stopReason == "cancelled") ? "Stopped." : nil

        case "message.error":
            flushNow()
            isStreaming = false
            errorBanner = event.message
            if let index = messages.lastIndex(where: { $0.role == "assistant" }) {
                messages[index].isStreaming = false
                messages[index].errorMessage = event.message
            }

        case "cancelled":
            isStreaming = false
            banner = "Stopped."

        case "commands.available":
            if let list = event.commands { commands = list; acpAvailable = true }

        case "session.titled":
            banner = nil

        case "command.result":
            // Reflect a model switch straight away; the session struct this
            // screen was pushed with still holds the old value.
            if event.command == "model", let value = event.text {
                currentModel = value
                Task { await loadCommands() }
            }
            let text = event.text ?? "Done."
            messages.append(ChatMessage(id: UUID().uuidString, role: "system",
                                        text: "/\(event.command ?? "") — \(text)"))

        case "command.error":
            errorBanner = event.message

        case "config.changed":
            Task { await loadCommands() }

        case "agent.restarting":
            banner = "Restarting the agent…"

        case "error":
            errorBanner = event.message

        default:
            break
        }
    }

    @discardableResult
    private func ensureAssistant() -> String {
        if let last = messages.last, last.role == "assistant", last.isStreaming {
            return last.id
        }
        let message = ChatMessage(id: UUID().uuidString, role: "assistant",
                                  isStreaming: true)
        messages.append(message)
        return message.id
    }

    private func scheduleFlush() {
        guard flushTask == nil else { return }
        flushTask = Task { [weak self] in
            guard let self else { return }
            try? await Task.sleep(for: self.flushInterval)
            self.flushNow()
        }
    }

    private func flushNow() {
        flushTask?.cancel()
        flushTask = nil
        for (id, text) in pendingText {
            guard !text.isEmpty,
                  let index = messages.firstIndex(where: { $0.id == id }) else { continue }
            messages[index].text += text
        }
        pendingText.removeAll()
    }

    // MARK: Sending

    func send(text: String, model: String?, effort: String?, attachments: [String]) async {
        errorBanner = nil
        banner = nil
        messages.append(ChatMessage(id: UUID().uuidString, role: "user", text: text))
        isStreaming = true
        do {
            try await socket?.prompt(text: text, model: model, effort: effort,
                                     attachments: attachments)
        } catch {
            isStreaming = false
            errorBanner = error.localizedDescription
        }
    }

    func run(command: CommandSpec, argument: String?) async {
        errorBanner = nil
        do {
            try await socket?.command(name: command.name, args: argument)
            if command.source == "acp" { isStreaming = true }
        } catch {
            errorBanner = error.localizedDescription
        }
    }

    func cancel() async {
        try? await socket?.cancel()
    }

    var contextPercent: Int? {
        guard let window = contextWindow, window > 0,
              let total = usage?.totalTokens else { return nil }
        return min(100, Int(Double(total) / Double(window) * 100))
    }
}

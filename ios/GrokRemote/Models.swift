import Foundation

// MARK: - Credentials

/// Device credentials, bound to the origin they were issued by.
///
/// The origin is part of the identity on purpose. Settings lets the server
/// address be edited, and a token minted by one Bridge must never be sent to a
/// different host just because the user pasted a new URL -- that would hand the
/// only credential this app has to whoever owns that address.
struct DeviceCredentials: Codable, Equatable {
    var baseURL: URL
    var deviceId: String
    var secret: String

    var bearerToken: String { "\(deviceId).\(secret)" }

    /// Scheme + host + port. Two URLs matching here are the same Bridge.
    var origin: String {
        var parts = "\(baseURL.scheme ?? "https")://\(baseURL.host ?? "")"
        if let port = baseURL.port { parts += ":\(port)" }
        return parts
    }

    func matches(origin other: URL) -> Bool {
        var parts = "\(other.scheme ?? "https")://\(other.host ?? "")"
        if let port = other.port { parts += ":\(port)" }
        return parts == origin
    }
}

// MARK: - Models

struct ModelInfo: Codable, Identifiable, Hashable {
    var id: String
    var name: String
    var description: String?
    var contextWindow: Int?
    var reasoningEfforts: [String]
    var supportsReasoningEffort: Bool
    var vision: Bool

    enum CodingKeys: String, CodingKey {
        case id, name, description, contextWindow, reasoningEfforts
        case supportsReasoningEffort, vision
    }

    init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        name = (try? c.decode(String.self, forKey: .name)) ?? id
        description = try? c.decode(String.self, forKey: .description)
        contextWindow = try? c.decode(Int.self, forKey: .contextWindow)
        reasoningEfforts = (try? c.decode([String].self, forKey: .reasoningEfforts)) ?? []
        supportsReasoningEffort =
            (try? c.decode(Bool.self, forKey: .supportsReasoningEffort)) ?? false
        vision = (try? c.decode(Bool.self, forKey: .vision)) ?? false
    }
}

struct ModelsResponse: Codable {
    var models: [ModelInfo]
    var defaultModel: String?
    enum CodingKeys: String, CodingKey {
        case models
        case defaultModel = "default"
    }
}

// MARK: - Sessions

struct ChatSession: Codable, Identifiable, Hashable {
    var id: String
    var cwd: String
    var title: String?
    var createdAt: Double
    var lastActiveAt: Double
    var lastMessagePreview: String?
    var model: String?
    var reasoningEffort: String?
    var state: String?
    var cwdExists: Bool?
    var messageCount: Int?
    var headBranch: String?
    var lastTurnSummary: String?

    enum CodingKeys: String, CodingKey {
        case id, cwd, title, model, state, cwdExists, messageCount, headBranch
        case lastTurnSummary, reasoningEffort
        case createdAt = "created_at"
        case lastActiveAt = "last_active_at"
        case lastMessagePreview = "last_message_preview"
    }

    /// Directory name, which reads better than an absolute path in a list.
    var folderName: String { (cwd as NSString).lastPathComponent }
    var displayTitle: String { title?.isEmpty == false ? title! : folderName }
}

struct SessionsResponse: Codable { var sessions: [ChatSession] }

// MARK: - History

struct HistoryMessage: Codable, Hashable {
    var role: String
    var text: String
    var model: String?
    var toolCalls: [HistoryToolCall]?
}

struct HistoryToolCall: Codable, Hashable {
    var id: String?
    var name: String?
}

struct HistoryPage: Codable {
    var messages: [HistoryMessage]
    var total: Int
    var hasMore: Bool
    var nextBefore: Int?
}

// MARK: - Workspace

struct RepoInfo: Codable, Identifiable, Hashable {
    var path: String
    var name: String
    var isWorktree: Bool
    var id: String { path }
}

struct RepoStatus: Codable, Hashable {
    var branch: String?
    var dirtyCount: Int?
    var lastCommitAt: Double?
    var isWorktree: Bool?
    /// Set when git could not answer in time -- `git status` genuinely hangs in
    /// some worktrees, and a blank row would read as "clean".
    var statusUnavailable: String?
}

struct FSEntry: Codable, Identifiable, Hashable {
    var name: String
    var path: String
    var isRepo: Bool
    var isDenied: Bool
    var isHidden: Bool
    var id: String { path }
}

struct FSListing: Codable {
    var path: String
    var parent: String?
    var entries: [FSEntry]
}

// MARK: - Commands

struct CommandOption: Codable, Hashable {
    var value: String
    var label: String?
    var detail: String?
    var contextWindow: Int?
}

struct CommandSpec: Codable, Identifiable, Hashable {
    var name: String
    /// "acp" for commands the agent itself provides, "bridge" for ours.
    var source: String
    var kind: String
    var description: String
    var aliases: [String]
    var argType: String
    var argHint: String?
    var group: String
    var options: [CommandOption]?
    var disabled: Bool?

    var id: String { "\(source).\(name)" }
    var takesArgument: Bool { argType != "none" }
    var isEnum: Bool { argType == "enum" }
}

struct CommandsResponse: Codable {
    var commands: [CommandSpec]
    /// False when no agent is running, in which case the agent's own commands
    /// cannot be listed yet.
    var acpAvailable: Bool
}

// MARK: - Reasoning effort

enum ReasoningEffort: String, CaseIterable, Identifiable, Codable {
    case none, minimal, low, medium, high, xhigh, max
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
}

// MARK: - Wire events

/// One event from the Bridge's own protocol (grok_bridge/protocol.py), never
/// grok's raw ACP frames. Fields are per-type; only the relevant ones populate.
struct BridgeEvent: Codable {
    init(type: String) { self.type = type }

    var type: String
    var seq: Int?
    var jobId: String?

    var data: String?
    var message: String?
    var code: String?
    var stopReason: String?

    var toolCallId: String?
    var tool: String?
    var label: String?
    var kind: String?
    var title: String?
    var status: String?
    var readOnly: Bool?
    var exitCode: Int?
    var locations: [ToolLocation]?

    var usage: Usage?
    var commands: [CommandSpec]?
    var entries: [PlanEntry]?
    var mode: String?
    var command: String?
    var text: String?

    // hello.ack
    var sessionId: String?
    var currentSeq: Int?
    var sessionState: String?
    var replayed: Int?
    var cwd: String?

    enum CodingKeys: String, CodingKey {
        case type, seq, data, message, code, stopReason, toolCallId, tool, label
        case kind, title, status, readOnly, exitCode, locations, usage, commands
        case entries, mode, command, text
        case sessionId, currentSeq, sessionState, replayed, cwd
        case jobId = "job_id"
    }
}

struct ToolLocation: Codable, Hashable { var path: String? }

struct PlanEntry: Codable, Hashable {
    var content: String
    var priority: String?
    var status: String?
}

/// Usage as reported by grok on the turn's terminal event. Richer than a token
/// count: it carries cache hits and actual cost.
struct Usage: Codable, Hashable {
    var inputTokens: Int?
    var outputTokens: Int?
    var totalTokens: Int?
    var cachedReadTokens: Int?
    var reasoningTokens: Int?
    var costUsdTicks: Int?

    var costUSD: Double? {
        guard let ticks = costUsdTicks else { return nil }
        return Double(ticks) / 1_000_000_000.0
    }
}


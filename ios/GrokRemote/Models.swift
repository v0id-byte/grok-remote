import Foundation

struct DeviceCredentials: Codable {
    var baseURL: URL
    var deviceId: String
    var secret: String

    var bearerToken: String { "\(deviceId).\(secret)" }
}

struct ModelInfo: Codable, Identifiable, Hashable {
    var id: String
    var isDefault: Bool
    var vision: Bool

    enum CodingKeys: String, CodingKey {
        case id, vision
        case isDefault = "default"
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

struct ChatSession: Codable, Identifiable, Hashable {
    var id: String
    var cwd: String
    var title: String?
    var createdAt: Double
    var lastActiveAt: Double
    var lastMessagePreview: String?

    enum CodingKeys: String, CodingKey {
        case id, cwd, title
        case createdAt = "created_at"
        case lastActiveAt = "last_active_at"
        case lastMessagePreview = "last_message_preview"
    }
}

struct SessionsResponse: Codable {
    var sessions: [ChatSession]
}

/// Reasoning-depth options, matching grok CLI's own --reasoning-effort enum
/// (plan v1.3: "切换思考深度... 跟 grok CLI 自己的枚举保持一致").
enum ReasoningEffort: String, CaseIterable, Identifiable {
    case none, minimal, low, medium, high, xhigh, max
    var id: String { rawValue }
    var label: String { rawValue.capitalized }
}

/// One event from the Bridge's own WebSocket protocol (grok_bridge/protocol.py) --
/// never grok's raw NDJSON. Fields are optional per-type; only the relevant
/// ones are populated for a given `type`.
struct BridgeEvent: Codable {
    var type: String
    var data: String?
    var jobId: String?
    var seq: Int?
    var toolCallId: String?
    var tool: String?
    var kind: String?
    var status: String?
    var stopReason: String?
    var message: String?

    enum CodingKeys: String, CodingKey {
        case type, data, tool, kind, status, message
        case jobId = "job_id"
        case seq
        case toolCallId
        case stopReason
    }
}

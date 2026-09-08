import Foundation
#if canImport(UIKit)
import UIKit
#endif

enum BridgeError: LocalizedError {
    case notPaired
    case originMismatch
    case http(Int, String)
    case invalidResponse
    case socketClosed(String)

    var errorDescription: String? {
        switch self {
        case .notPaired:
            return "This device is not paired with a Bridge yet."
        case .originMismatch:
            return "Those credentials belong to a different server. Pair again."
        case .http(let code, let body):
            switch code {
            case 401: return "The Bridge rejected this device's token. Pair again."
            case 403: return "The Bridge refused: \(body.isEmpty ? "not allowed" : body)"
            case 404: return "Not found on the Bridge."
            case 409: return "That session is busy running a turn."
            case 429: return "Too many attempts. Wait a moment and retry."
            default:  return "Bridge error \(code)\(body.isEmpty ? "" : ": \(body)")"
            }
        case .invalidResponse:
            return "The Bridge sent a response this app could not read."
        case .socketClosed(let reason):
            return reason
        }
    }
}

/// Talks to the Bridge's HTTP + WebSocket surface (grok_bridge/app.py).
///
/// Authentication is a single layer: the Bridge's own per-device bearer token.
/// Cloudflare Access was removed deliberately -- an Access Service Token is a
/// machine-to-machine client secret, and shipping one inside the app bundle
/// leaks it to anyone who runs `strings` on the IPA.
final class BridgeClient {
    var credentials: DeviceCredentials?

    private let session: URLSession = {
        let config = URLSessionConfiguration.default
        config.timeoutIntervalForRequest = 30
        config.waitsForConnectivity = false
        return URLSession(configuration: config)
    }()

    private let decoder = JSONDecoder()

    // MARK: - Requests

    private func request(_ path: String, method: String = "GET",
                         query: [URLQueryItem] = [],
                         body: [String: Any]? = nil) throws -> URLRequest {
        guard let creds = credentials else { throw BridgeError.notPaired }
        var components = URLComponents(
            url: creds.baseURL.appendingPathComponent(path),
            resolvingAgainstBaseURL: false)
        if !query.isEmpty { components?.queryItems = query }
        guard let url = components?.url else { throw BridgeError.invalidResponse }

        var req = URLRequest(url: url)
        req.httpMethod = method
        req.setValue("Bearer \(creds.bearerToken)", forHTTPHeaderField: "Authorization")
        if let body {
            req.httpBody = try JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    private func send(_ req: URLRequest) async throws -> Data {
        let (data, response) = try await session.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw BridgeError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            var detail = ""
            if let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let message = object["detail"] as? String {
                detail = message
            }
            throw BridgeError.http(http.statusCode, detail)
        }
        return data
    }

    private func get<T: Decodable>(_ path: String, query: [URLQueryItem] = []) async throws -> T {
        try decoder.decode(T.self, from: try await send(try request(path, query: query)))
    }

    // MARK: - Pairing

    func pair(baseURL: URL, pairingToken: String) async throws -> DeviceCredentials {
        var req = URLRequest(url: baseURL.appendingPathComponent("v1/pair"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "pairing_token": pairingToken,
            "device_name": Self.deviceName,
        ])
        let data = try await send(req)
        guard let object = try JSONSerialization.jsonObject(with: data) as? [String: Any],
              let deviceId = object["device_id"] as? String,
              let secret = object["secret"] as? String else {
            throw BridgeError.invalidResponse
        }
        return DeviceCredentials(baseURL: baseURL, deviceId: deviceId, secret: secret)
    }

    func revokeSelf() async throws {
        guard let creds = credentials else { throw BridgeError.notPaired }
        _ = try await send(try request("v1/devices/\(creds.deviceId)", method: "DELETE"))
    }

    // MARK: - Health

    struct Health: Codable {
        var status: String
        var grokReachable: Bool
        var activeAgents: Int?
        var uptimeS: Double?
        var authFailures: Int?
        var pairingOpen: Bool?

        enum CodingKeys: String, CodingKey {
            case status
            case grokReachable = "grok_reachable"
            case activeAgents = "active_agents"
            case uptimeS = "uptime_s"
            case authFailures = "auth_failures"
            case pairingOpen = "pairing_open"
        }
    }

    /// Health is unauthenticated, so it also works as a reachability probe for a
    /// server address the user is still typing.
    func health(at baseURL: URL? = nil) async throws -> Health {
        let url = (baseURL ?? credentials?.baseURL)?.appendingPathComponent("health")
        guard let url else { throw BridgeError.notPaired }
        return try decoder.decode(Health.self, from: try await send(URLRequest(url: url)))
    }

    // MARK: - Catalogue

    func models() async throws -> ModelsResponse { try await get("v1/models") }
    func sessions() async throws -> [ChatSession] {
        (try await get("v1/sessions") as SessionsResponse).sessions
    }
    func recentDirs() async throws -> [String] {
        struct Response: Codable { var dirs: [String] }
        return (try await get("v1/recent-dirs") as Response).dirs
    }
    func repos(refresh: Bool = false) async throws -> [RepoInfo] {
        struct Response: Codable { var repos: [RepoInfo] }
        let query = refresh ? [URLQueryItem(name: "refresh", value: "true")] : []
        return (try await get("v1/repos", query: query) as Response).repos
    }
    func repoStatus(paths: [String]) async throws -> [String: RepoStatus] {
        struct Response: Codable { var status: [String: RepoStatus] }
        let data = try await send(try request("v1/repos/status", method: "POST",
                                              body: ["paths": paths]))
        return (try decoder.decode(Response.self, from: data)).status
    }
    func listDirectory(_ path: String) async throws -> FSListing {
        try await get("v1/fs/list", query: [URLQueryItem(name: "path", value: path)])
    }

    // MARK: - Session lifecycle

    func createSession(cwd: String) async throws -> ChatSession {
        struct Created: Codable { var id: String; var cwd: String }
        let created = try decoder.decode(
            Created.self,
            from: try await send(try request("v1/sessions", method: "POST",
                                             body: ["cwd": cwd])))
        return ChatSession(id: created.id, cwd: created.cwd, title: nil,
                           createdAt: Date().timeIntervalSince1970,
                           lastActiveAt: Date().timeIntervalSince1970,
                           lastMessagePreview: nil, model: nil, reasoningEffort: nil,
                           state: "stopped", cwdExists: true, messageCount: nil,
                           headBranch: nil, lastTurnSummary: nil)
    }

    func patchSession(_ id: String, fields: [String: Any]) async throws {
        _ = try await send(try request("v1/sessions/\(id)", method: "PATCH", body: fields))
    }

    func deleteSession(_ id: String) async throws {
        _ = try await send(try request("v1/sessions/\(id)", method: "DELETE"))
    }

    func history(_ id: String, limit: Int = 50, before: Int? = nil) async throws -> HistoryPage {
        var query = [URLQueryItem(name: "limit", value: String(limit))]
        if let before { query.append(URLQueryItem(name: "before", value: String(before))) }
        return try await get("v1/sessions/\(id)/messages", query: query)
    }

    func commands(_ id: String) async throws -> CommandsResponse {
        try await get("v1/sessions/\(id)/commands")
    }

    struct CommandResult: Codable {
        var command: String?
        var kind: String?
        var text: String?
        var note: String?
    }

    func runCommand(_ id: String, name: String, args: String?) async throws -> CommandResult {
        var body: [String: Any] = ["name": name]
        if let args { body["args"] = args }
        return try decoder.decode(
            CommandResult.self,
            from: try await send(try request("v1/sessions/\(id)/command",
                                             method: "POST", body: body)))
    }

    // MARK: - Uploads

    func upload(data: Data, filename: String, mimeType: String) async throws -> String {
        guard credentials != nil else { throw BridgeError.notPaired }
        var req = try request("v1/upload", method: "POST")
        let boundary = "Boundary-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)",
                     forHTTPHeaderField: "Content-Type")

        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n"
            .data(using: .utf8)!)
        body.append("Content-Type: \(mimeType)\r\n\r\n".data(using: .utf8)!)
        body.append(data)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body

        struct Uploaded: Codable { var path: String }
        return (try decoder.decode(Uploaded.self, from: try await send(req))).path
    }

    // MARK: - v2 socket

    /// Open the session's event stream. `afterSeq == nil` is a fresh entry
    /// (tail: only an in-flight turn is replayed, since history is already
    /// loaded); a value resumes an interrupted screen exactly after that seq.
    func connect(sessionId: String, afterSeq: Int?) throws -> SessionSocket {
        guard let creds = credentials else { throw BridgeError.notPaired }
        var components = URLComponents(
            url: creds.baseURL.appendingPathComponent("v2/chat"),
            resolvingAgainstBaseURL: false)
        components?.scheme = (creds.baseURL.scheme == "https") ? "wss" : "ws"
        guard let url = components?.url else { throw BridgeError.invalidResponse }

        var req = URLRequest(url: url)
        // Authentication belongs on the upgrade: the Bridge rejects the
        // handshake outright rather than accepting a socket and then asking.
        req.setValue("Bearer \(creds.bearerToken)", forHTTPHeaderField: "Authorization")
        return SessionSocket(task: session.webSocketTask(with: req),
                             sessionId: sessionId, afterSeq: afterSeq)
    }

    static var deviceName: String {
        #if canImport(UIKit)
        return UIDevice.current.name
        #else
        return "iOS device"
        #endif
    }
}

/// A session-scoped, resumable connection.
///
/// A dropped socket is not a cancellation: the turn keeps running on the Mac and
/// the next connection replays whatever was missed. That matters because iOS
/// suspends backgrounded apps routinely, so disconnection is the normal case.
final class SessionSocket {
    private let task: URLSessionWebSocketTask
    private let sessionId: String
    private let afterSeq: Int?
    private var continuation: AsyncThrowingStream<BridgeEvent, Error>.Continuation?
    private let decoder = JSONDecoder()

    init(task: URLSessionWebSocketTask, sessionId: String, afterSeq: Int?) {
        self.task = task
        self.sessionId = sessionId
        self.afterSeq = afterSeq
    }

    func events() -> AsyncThrowingStream<BridgeEvent, Error> {
        AsyncThrowingStream { continuation in
            self.continuation = continuation
            task.resume()
            Task {
                do {
                    var hello: [String: Any] = ["type": "hello",
                                                "protocolVersion": 2,
                                                "sessionId": self.sessionId]
                    if let afterSeq = self.afterSeq { hello["afterSeq"] = afterSeq }
                    try await self.send(hello)
                    await self.receiveLoop()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { @Sendable _ in
                self.task.cancel(with: .goingAway, reason: nil)
            }
        }
    }

    func prompt(text: String, model: String?, effort: String?, attachments: [String]) async throws {
        var frame: [String: Any] = ["type": "prompt", "text": text,
                                    "attachments": attachments]
        if let model { frame["model"] = model }
        if let effort { frame["reasoningEffort"] = effort }
        try await send(frame)
    }

    func command(name: String, args: String?) async throws {
        var frame: [String: Any] = ["type": "command", "name": name]
        if let args { frame["args"] = args }
        try await send(frame)
    }

    func cancel() async throws { try await send(["type": "cancel"]) }

    func close() { task.cancel(with: .goingAway, reason: nil); continuation?.finish() }

    private func send(_ frame: [String: Any]) async throws {
        let data = try JSONSerialization.data(withJSONObject: frame)
        try await task.send(.string(String(decoding: data, as: UTF8.self)))
    }

    private func receiveLoop() async {
        while true {
            do {
                let message = try await task.receive()
                guard case .string(let text) = message,
                      let data = text.data(using: .utf8) else { continue }
                // Everything on this socket, hello.ack included, is one event
                // type; the caller switches on `type`. Undecodable frames are
                // surfaced rather than dropped so a protocol change is visible.
                if let event = try? decoder.decode(BridgeEvent.self, from: data) {
                    continuation?.yield(event)
                } else {
                    continuation?.yield(BridgeEvent(type: "message.unknown"))
                }
            } catch {
                continuation?.finish(throwing: error)
                return
            }
        }
    }
}

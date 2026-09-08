import Foundation

enum BridgeError: Error, LocalizedError {
    case http(Int, String)
    case invalidResponse

    // Without this, Swift bridges the enum to a generic NSError whose
    // localizedDescription is a useless "The operation couldn't be
    // completed" -- exactly what showed up in the pairing screen when the
    // real failure (a 403 from Cloudflare Access, missing headers on the
    // pairing request) was silently swallowed. Surface the real status/body.
    var errorDescription: String? {
        switch self {
        case .http(let status, let body):
            let trimmed = body.trimmingCharacters(in: .whitespacesAndNewlines)
            return "HTTP \(status)" + (trimmed.isEmpty ? "" : ": \(trimmed.prefix(200))")
        case .invalidResponse:
            return "Invalid response from server"
        }
    }
}

/// Talks to the Bridge's HTTP + WebSocket surface (grok_bridge/app.py).
///
/// Authentication is a single layer: the Bridge's own per-device bearer token.
/// Cloudflare Access was removed deliberately -- an Access Service Token is a
/// machine-to-machine client secret, and shipping one inside the app bundle
/// leaks it to anyone who runs `strings` on the IPA. The Bridge compensates
/// with short-lived pairing tokens, rate limiting, and a kernel sandbox around
/// the agent process (see plan v2 §0.1/§0.2/§0.6c).
final class BridgeClient {
    var credentials: DeviceCredentials?

    private func request(_ path: String, method: String = "GET", body: [String: Any]? = nil) -> URLRequest {
        var req = URLRequest(url: credentials!.baseURL.appendingPathComponent(path))
        req.httpMethod = method
        if let creds = credentials {
            req.setValue("Bearer \(creds.bearerToken)", forHTTPHeaderField: "Authorization")
        }
        if let body {
            req.httpBody = try? JSONSerialization.data(withJSONObject: body)
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    private func send(_ req: URLRequest) async throws -> Data {
        let (data, response) = try await URLSession.shared.data(for: req)
        guard let http = response as? HTTPURLResponse else { throw BridgeError.invalidResponse }
        guard (200..<300).contains(http.statusCode) else {
            let message = String(data: data, encoding: .utf8) ?? ""
            throw BridgeError.http(http.statusCode, message)
        }
        return data
    }

    // MARK: - Pairing

    func pair(baseURL: URL, pairingToken: String) async throws -> DeviceCredentials {
        var req = URLRequest(url: baseURL.appendingPathComponent("v1/pair"))
        req.httpMethod = "POST"
        req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        req.httpBody = try JSONSerialization.data(withJSONObject: [
            "pairing_token": pairingToken,
            "device_name": UIDeviceName.current,
        ])
        let data = try await send(req)
        struct PairResponse: Codable { let device_id: String; let secret: String }
        let resp = try JSONDecoder().decode(PairResponse.self, from: data)
        return DeviceCredentials(baseURL: baseURL, deviceId: resp.device_id, secret: resp.secret)
    }

    // MARK: - REST

    func health() async throws -> Bool {
        let data = try await send(request("health"))
        struct HealthResponse: Codable { let status: String }
        return (try? JSONDecoder().decode(HealthResponse.self, from: data))?.status == "ok"
    }

    func fetchModels() async throws -> ModelsResponse {
        let data = try await send(request("v1/models"))
        return try JSONDecoder().decode(ModelsResponse.self, from: data)
    }

    func fetchSessions() async throws -> [ChatSession] {
        let data = try await send(request("v1/sessions"))
        return try JSONDecoder().decode(SessionsResponse.self, from: data).sessions
    }

    func fetchRecentDirs() async throws -> [String] {
        let data = try await send(request("v1/recent-dirs"))
        struct Resp: Codable { let dirs: [String] }
        return try JSONDecoder().decode(Resp.self, from: data).dirs
    }

    func createSession(cwd: String) async throws -> ChatSession {
        let data = try await send(request("v1/sessions", method: "POST", body: ["cwd": cwd]))
        struct Resp: Codable { let id: String; let cwd: String }
        let resp = try JSONDecoder().decode(Resp.self, from: data)
        return ChatSession(id: resp.id, cwd: resp.cwd, title: nil, createdAt: 0, lastActiveAt: 0, lastMessagePreview: nil)
    }

    /// Uploads one image/file to /v1/upload and returns the server-side path
    /// to pass back as a `attachments` entry on the next chat turn (plan
    /// v1.3 §1: appended into the prompt as `@<path>`).
    func upload(data: Data, filename: String, mimeType: String) async throws -> String {
        var req = request("v1/upload", method: "POST")
        let boundary = "GrokRemote-\(UUID().uuidString)"
        req.setValue("multipart/form-data; boundary=\(boundary)", forHTTPHeaderField: "Content-Type")

        var body = Data()
        body.append("--\(boundary)\r\n".data(using: .utf8)!)
        body.append("Content-Disposition: form-data; name=\"file\"; filename=\"\(filename)\"\r\n".data(using: .utf8)!)
        body.append("Content-Type: \(mimeType)\r\n\r\n".data(using: .utf8)!)
        body.append(data)
        body.append("\r\n--\(boundary)--\r\n".data(using: .utf8)!)
        req.httpBody = body

        let respData = try await send(req)
        struct Resp: Codable { let path: String }
        return try JSONDecoder().decode(Resp.self, from: respData).path
    }

    // MARK: - Chat WebSocket

    /// Opens `/v1/chat`, sends the first frame, and yields Bridge events as
    /// they stream in. Call `cancel()` on the returned handle to stop the
    /// in-flight turn (plan v1.3: "停止生成").
    func chat(sessionId: String, text: String, model: String?, reasoningEffort: String?, attachments: [String]) -> ChatStream {
        let wsURL = credentials!.baseURL
            .appendingPathComponent("v1/chat")
            .absoluteString
            .replacingOccurrences(of: "https://", with: "wss://")
            .replacingOccurrences(of: "http://", with: "ws://")
        var req = URLRequest(url: URL(string: wsURL)!)
        let task = URLSession.shared.webSocketTask(with: req)
        let firstFrame: [String: Any] = [
            "sessionId": sessionId,
            "text": text,
            "model": model as Any,
            "reasoningEffort": reasoningEffort as Any,
            "attachments": attachments,
            "token": credentials!.bearerToken,
        ]
        return ChatStream(task: task, firstFrame: firstFrame)
    }
}

/// Wraps a WebSocketTask as an AsyncThrowingStream of BridgeEvent, plus a
/// cancel() that sends the Bridge's {"type":"cancel"} control message.
final class ChatStream {
    private let task: URLSessionWebSocketTask
    private let firstFrame: [String: Any]

    init(task: URLSessionWebSocketTask, firstFrame: [String: Any]) {
        self.task = task
        self.firstFrame = firstFrame
    }

    func events() -> AsyncThrowingStream<BridgeEvent, Error> {
        AsyncThrowingStream { continuation in
            task.resume()
            guard let data = try? JSONSerialization.data(withJSONObject: firstFrame),
                  let text = String(data: data, encoding: .utf8) else {
                continuation.finish(throwing: BridgeError.invalidResponse)
                return
            }
            task.send(.string(text)) { [weak self] error in
                if let error { continuation.finish(throwing: error); return }
                self?.receiveLoop(continuation)
            }
            continuation.onTermination = { [task] _ in
                task.cancel(with: .goingAway, reason: nil)
            }
        }
    }

    func cancel() {
        let payload = try? JSONSerialization.data(withJSONObject: ["type": "cancel"])
        guard let payload, let text = String(data: payload, encoding: .utf8) else { return }
        task.send(.string(text)) { _ in }
    }

    private func receiveLoop(_ continuation: AsyncThrowingStream<BridgeEvent, Error>.Continuation) {
        task.receive { [weak self] result in
            switch result {
            case .failure(let error):
                continuation.finish(throwing: error)
            case .success(let message):
                if case .string(let text) = message,
                   let data = text.data(using: .utf8),
                   let event = try? JSONDecoder().decode(BridgeEvent.self, from: data) {
                    continuation.yield(event)
                    if ["message.done", "message.error", "cancelled"].contains(event.type) {
                        continuation.finish()
                        return
                    }
                }
                self?.receiveLoop(continuation)
            }
        }
    }
}

enum UIDeviceName {
    static var current: String {
        #if canImport(UIKit)
        return UIDevice.current.name
        #else
        return "iPhone"
        #endif
    }
}

#if canImport(UIKit)
import UIKit
#endif

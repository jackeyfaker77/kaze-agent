import { EventEmitter } from "node:events";
import { randomUUID } from "node:crypto";
import { readFileSync } from "node:fs";
import type { BridgeRequest, BridgeResponse } from "./shared.js";
import { bridgeRequestTimeoutMs } from "./bridgeTimeoutPolicy.js";

export type RemoteConnection = { url: string; token: string };

export function remoteConnectionFromEnvironment(env: NodeJS.ProcessEnv): RemoteConnection | null {
  if (!env.KAZE_RUNTIME_URL && !env.KAZE_RUNTIME_TOKEN_FILE) return null;
  if (!env.KAZE_RUNTIME_URL || !env.KAZE_RUNTIME_TOKEN_FILE) throw new Error("远程模式需要同时提供 KAZE_RUNTIME_URL 和 KAZE_RUNTIME_TOKEN_FILE");
  const url = new URL(env.KAZE_RUNTIME_URL);
  if (url.username || url.password || url.search || url.hash ||
      (url.protocol !== "wss:" && !(url.protocol === "ws:" && ["127.0.0.1", "[::1]", "localhost"].includes(url.hostname)))) {
    throw new Error("远程连接须使用 wss://；仅本机隧道允许 ws://，地址不能携带凭据、查询参数或片段");
  }
  const token = readFileSync(env.KAZE_RUNTIME_TOKEN_FILE, "utf8").trim();
  if (!/^[\x21-\x7e]{32,512}$/.test(token)) throw new Error("Runtime token 必须是 32–512 个非空白 ASCII 字符");
  return { url: url.href, token };
}

type Pending = { method: string; finish: (response: BridgeResponse) => void };
type Connection = {
  socket: WebSocket;
  pending: Map<string, Pending>;
  ready: boolean;
  authenticated: boolean;
  start: Promise<void>;
  disconnect: () => void;
};

/** No local process fallback; reconnection never replays accepted mutations. */
export class RemoteBridgeClient extends EventEmitter {
  readonly mode = "remote";
  private connection: Connection | null = null;
  private enabled = false;
  private retry: NodeJS.Timeout | null = null;
  private error: string | null = null;
  private delay = 500;

  constructor(private readonly options: RemoteConnection,
    private readonly createSocket: (url: string) => WebSocket = url => new WebSocket(url)) { super(); }

  start(): Promise<void> {
    this.enabled = true;
    if (this.connection) return this.connection.start;
    if (this.retry) { clearTimeout(this.retry); this.retry = null; }
    const socket = this.createSocket(this.options.url);
    const connection: Connection = { socket, pending: new Map(), ready: false, authenticated: false, start: Promise.resolve(), disconnect: () => {} };
    this.connection = connection;
    let rejectStart: (error: Error) => void = () => {};
    let resolveStart: () => void = () => {};
    connection.start = new Promise<void>((resolve, reject) => { resolveStart = resolve; rejectStart = reject; });
    const timer = setTimeout(() => fail("远程连接或认证超时"), 10000);
    const fail = (message: string) => {
      clearTimeout(timer);
      rejectStart(new Error(message));
      for (const [id, request] of connection.pending) request.finish(this.failure(id, request.method, "remote_outcome_unknown", "连接已断开，请重新连接后核对服务器记录；请求不会自动重发。"));
      connection.pending.clear();
      if (this.connection !== connection) return;
      this.connection = null;
      this.error = message;
      connection.ready = false;
      try { socket.close(); } catch { /* A transport may already be closed. */ }
      this.emit("exit", message);
      if (this.enabled && !this.retry) {
        this.retry = setTimeout(() => { this.retry = null; void this.start().catch(() => {}); }, this.delay);
        this.retry.unref();
        this.delay = Math.min(this.delay * 2, 10000);
      }
    };
    connection.disconnect = () => fail("远程连接已关闭");
    socket.addEventListener("open", () => {
      if (this.connection === connection) socket.send(JSON.stringify({ type: "auth", token: this.options.token }));
    });
    socket.addEventListener("error", () => fail("远程连接失败，请检查地址、网络和证书"));
    socket.addEventListener("close", () => fail("远程连接已断开，请检查服务或认证信息"));
    socket.addEventListener("message", event => {
      if (this.connection !== connection) return;
      try {
        const frame = JSON.parse(String(event.data));
        if (frame.type === "auth.ok" && frame.protocol === 1 && !connection.authenticated) {
          connection.authenticated = true;
          void this.send(connection, { method: "health", payload: {} }).then(response => {
            if (response.error || response.payload.protocol !== 1 || response.payload.transport !== "remote" || !response.payload.ok) { fail("服务器协议或健康检查失败"); return; }
            if (this.connection !== connection) return;
            clearTimeout(timer); connection.ready = true; this.error = null; this.delay = 500; resolveStart();
            this.emit("event", { id: randomUUID(), type: "event", method: "bridge.connected", payload: { mode: "remote" } });
          }).catch(() => fail("远程健康检查失败"));
        } else if (frame.type === "event" && connection.authenticated) {
          // Other clients may publish while our health RPC is still pending.
          // The connected event will reconcile snapshots after readiness.
          if (connection.ready) this.emit("event", frame);
        } else if (frame.type === "response" && typeof frame.id === "string") {
          connection.pending.get(frame.id)?.finish(frame);
        } else { fail("服务器返回了无效的协议数据"); }
      } catch { fail("服务器返回了无效的协议数据"); }
    });
    return connection.start;
  }

  private failure(id: string, method: string, code: string, message: string): BridgeResponse {
    return { id, type: "response", method, payload: {}, error: { code, message } };
  }

  private send(connection: Connection, request: Omit<BridgeRequest, "id">): Promise<BridgeResponse> {
    const id = randomUUID();
    return new Promise(resolve => {
      const timer = setTimeout(() => finish(this.failure(id, request.method, "remote_outcome_unknown", "请求等待超时，请核对服务器记录；请求不会自动重发。")), bridgeRequestTimeoutMs(request.method));
      const finish = (response: BridgeResponse) => { clearTimeout(timer); connection.pending.delete(id); resolve(response); };
      connection.pending.set(id, { method: request.method, finish });
      try { connection.socket.send(JSON.stringify({ ...request, id })); }
      catch { finish(this.failure(id, request.method, "remote_outcome_unknown", "发送结果未知，请核对服务器记录。")); }
    });
  }

  async invoke(request: Omit<BridgeRequest, "id">): Promise<BridgeResponse> {
    try { await this.start(); }
    catch { return this.failure("not-connected", request.method, "remote_unavailable", this.error ?? "远程服务尚未连接"); }
    const connection = this.connection;
    if (!connection?.ready) return this.failure("not-connected", request.method, "remote_unavailable", "远程服务尚未连接");
    const response = await this.send(connection, request);
    if (!response.error && ["session.get", "session.create", "session.draft"].includes(request.method)) {
      this.emit("event", { id: response.id, type: "event", method: "session.selected", payload: { session_key: response.payload.session_key } });
    }
    return response;
  }

  async stop(): Promise<void> {
    this.enabled = false;
    if (this.retry) { clearTimeout(this.retry); this.retry = null; }
    const connection = this.connection;
    if (connection) {
      // Closing this socket never sends a Host shutdown command.
      connection.disconnect();
    }
  }
  async restart(): Promise<void> { await this.stop(); await this.start(); }
  isRunning(): boolean { return Boolean(this.connection?.ready && !this.error); }
  getLastError(): string | null { return this.error; }
}

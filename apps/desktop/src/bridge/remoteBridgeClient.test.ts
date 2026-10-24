import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { RemoteBridgeClient, remoteConnectionFromEnvironment } from "./remoteBridgeClient.js";

class Socket extends EventTarget {
  sent: Record<string, unknown>[] = [];
  closed = false;
  send(value: string) {
    const frame = JSON.parse(value);
    this.sent.push(frame);
    if (frame.type === "auth") queueMicrotask(() => this.receive({ type: "auth.ok", protocol: 1 }));
    else if (frame.method === "health") queueMicrotask(() => this.receive({ ...frame, type: "response", payload: { ok: true, protocol: 1, transport: "remote" }, error: null }));
  }
  receive(frame: object) { this.dispatchEvent(new MessageEvent("message", { data: JSON.stringify(frame) })); }
  close() { if (!this.closed) { this.closed = true; this.dispatchEvent(new Event("close")); } }
}

function setup() {
  const sockets: Socket[] = [];
  const client = new RemoteBridgeClient({ url: "ws://127.0.0.1:9000/", token: "a".repeat(32) }, () => {
    const socket = new Socket(); sockets.push(socket);
    queueMicrotask(() => socket.dispatchEvent(new Event("open")));
    return socket as unknown as WebSocket;
  });
  return { client, sockets };
}

test("remote mode is opt-in and requires protected transport and a token file", () => {
  assert.equal(remoteConnectionFromEnvironment({}), null);
  const root = mkdtempSync(join(tmpdir(), "kaze-remote-"));
  const token = join(root, "token"); writeFileSync(token, "a".repeat(32));
  try {
    for (const url of ["ws://example.com", "https://example.com", "wss://user:pass@example.com", "wss://example.com?token=private", "wss://example.com/#token"]) {
      assert.throws(() => remoteConnectionFromEnvironment({ KAZE_RUNTIME_URL: url, KAZE_RUNTIME_TOKEN_FILE: token }));
    }
    assert.equal(remoteConnectionFromEnvironment({ KAZE_RUNTIME_URL: "wss://example.com/runtime", KAZE_RUNTIME_TOKEN_FILE: token })?.url, "wss://example.com/runtime");
    assert.throws(() => remoteConnectionFromEnvironment({ KAZE_RUNTIME_URL: "ws://localhost:9000" }));
    writeFileSync(token, "short");
    assert.throws(() => remoteConnectionFromEnvironment({ KAZE_RUNTIME_URL: "ws://localhost:9000", KAZE_RUNTIME_TOKEN_FILE: token }));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

test("disconnect settles in-flight writes as unknown; reconnect never resends them", async () => {
  const { client, sockets } = setup();
  try {
    await client.start();
    assert.equal(client.isRunning(), true);
    const pending = client.invoke({ method: "chat.send", payload: { content: "once" } });
    await new Promise(resolve => setImmediate(resolve));
    sockets[0]!.close();
    assert.equal((await pending).error?.code, "remote_outcome_unknown");
    await client.start();
    assert.equal(sockets.length, 2);
    assert.equal(sockets.flatMap(s => s.sent).filter(f => f.method === "chat.send").length, 1);
    assert.equal(client.isRunning(), true);
  } finally { await client.stop(); }
});

test("stale socket frames cannot resolve requests on a newer connection; stop sends no shutdown RPC", async () => {
  const { client, sockets } = setup();
  try {
    await client.start(); await client.restart();
    const pending = client.invoke({ method: "session.get", payload: { session_key: "desktop:shared" } });
    await new Promise(resolve => setImmediate(resolve));
    const request = sockets[1]!.sent.at(-1)!;
    sockets[0]!.receive({ ...request, type: "response", payload: { session_key: "wrong" }, error: null });
    sockets[1]!.receive({ ...request, type: "response", payload: { session_key: "desktop:shared" }, error: null });
    assert.equal((await pending).payload.session_key, "desktop:shared");
    await client.stop();
    assert.equal(client.isRunning(), false);
    assert.ok(sockets.every(s => s.closed));
    assert.ok(sockets.flatMap(s => s.sent).every(f => f.type === "auth" || ["health", "session.get"].includes(String(f.method))));
  } finally { await client.stop(); }
});

test("failed authentication does not expose the token or fall back to a local process", async () => {
  const socket = new Socket();
  socket.send = () => queueMicrotask(() => socket.close());
  const client = new RemoteBridgeClient({ url: "ws://localhost:9000/", token: "secret-token" }, () => {
    queueMicrotask(() => socket.dispatchEvent(new Event("open")));
    return socket as unknown as WebSocket;
  });
  try {
    await assert.rejects(client.start());
    assert.equal(client.isRunning(), false);
    assert.ok(!client.getLastError()?.includes("secret-token"));
  } finally { await client.stop(); }
});

test("events from other desktops during the health handshake do not break connection", async () => {
  const socket = new Socket();
  const send = socket.send.bind(socket);
  socket.send = value => {
    if (JSON.parse(value).method === "health") {
      socket.receive({ id: "other-client", type: "event", method: "chat.delta", payload: { content_delta: "in flight" } });
    }
    send(value);
  };
  const client = new RemoteBridgeClient({ url: "ws://localhost:9000/", token: "a".repeat(32) }, () => {
    queueMicrotask(() => socket.dispatchEvent(new Event("open")));
    return socket as unknown as WebSocket;
  });
  try { await client.start(); assert.equal(client.isRunning(), true); }
  finally { await client.stop(); }
});

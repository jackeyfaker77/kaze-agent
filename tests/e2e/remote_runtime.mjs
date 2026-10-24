// Real Python Host, WebSocket client, AgentLoop, SQLite and optional Calendar MCP.
import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, existsSync, writeFileSync, openSync, closeSync, unlinkSync } from "node:fs";
import { resolve } from "node:path";
import { spawn } from "node:child_process";
import { setTimeout as delay } from "node:timers/promises";
import { RemoteBridgeClient } from "../../apps/desktop/src/bridge/remoteBridgeClient.ts";

mkdirSync(".tmp", { recursive: true });
const workspace = mkdtempSync(resolve(".tmp/remote-probe-"));
const python = resolve(process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python");
const calendarPython = process.env.KAZE_TEST_CALENDAR_PYTHON || "";
let host;
async function until(predicate, timeout = 30000) {
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) { if (predicate()) return; await delay(50); }
  throw new Error(`Timed out: ${workspace}`);
}
async function launch(port = 0) {
  const readyFile = resolve(workspace, "remote-ready.json");
  if (existsSync(readyFile)) unlinkSync(readyFile);
  const log = openSync(resolve(workspace, `host-${Date.now()}.log`), "a");
  host = spawn(python, [resolve("tests/e2e/remote_runtime_backend.py"), workspace, String(port), calendarPython], {
    stdio: ["ignore", log, log], env: { ...process.env, PYTHONIOENCODING: "utf-8" }, windowsHide: true,
  });
  closeSync(log);
  await until(() => { if (host.exitCode !== null) throw new Error(`Host exited ${host.exitCode}`); return existsSync(readyFile); });
  return JSON.parse(readFileSync(readyFile, "utf8"));
}
async function stop() {
  if (!host || host.exitCode !== null) return;
  const status = JSON.parse(readFileSync(resolve(workspace, "private_runtime/host/status.json"), "utf8"));
  writeFileSync(resolve(workspace, "private_runtime/host/stop.json"), JSON.stringify({ owner_id: status.owner_id }));
  await until(() => host.exitCode !== null);
  assert.equal(host.exitCode, 0);
}
const ready = await launch();
const options = { url: `ws://127.0.0.1:${ready.port}`, token: ready.token };
const first = new RemoteBridgeClient(options), second = new RemoteBridgeClient(options);
const rpc = async (client, method, payload = {}) => {
  const response = await client.invoke({ method, payload });
  assert.equal(response.error, null, JSON.stringify(response.error));
  return response.payload;
};
const report = { workspace, two_clients: false, disconnected_turn: false, host_restart: false, calendar_mcp: false };
try {
  await Promise.all([first.start(), second.start()]);
  const initial = await rpc(first, "health");
  await rpc(first, "chat.send", { session_key: "desktop:shared", content: "hello" });
  assert.equal((await rpc(second, "session.get", { session_key: "desktop:shared" })).messages.length, 2);
  report.two_clients = true;
  const pending = first.invoke({ method: "chat.send", payload: { session_key: "desktop:slow", content: "slow-input" } });
  await until(() => existsSync(resolve(workspace, "slow-started")));
  await first.stop();
  assert.equal((await pending).error.code, "remote_outcome_unknown");
  await delay(2300);
  assert.equal((await rpc(second, "session.get", { session_key: "desktop:slow" })).messages.length, 2);
  report.disconnected_turn = true;
  if (calendarPython) {
    const created = await rpc(second, "chat.send", { session_key: "desktop:todo", content: "create-remote-todo" });
    assert.ok(created.reply.includes("remote-shared-todo"));
    const listed = await rpc(first, "chat.send", { session_key: "desktop:todo-other", content: "list-remote-todos" });
    assert.ok(listed.reply.includes("remote-shared-todo"));
    report.calendar_mcp = true;
  }
  const beforeCalls = readFileSync(resolve(workspace, "calls.jsonl"), "utf8");
  await stop();
  await launch(ready.port);
  await until(() => second.isRunning());
  const latest = await rpc(second, "health");
  assert.equal(latest.root_id, initial.root_id);
  assert.notEqual(latest.epoch, initial.epoch);
  assert.equal((await rpc(second, "session.get", { session_key: "desktop:slow" })).messages.length, 2);
  assert.equal(readFileSync(resolve(workspace, "calls.jsonl"), "utf8"), beforeCalls);
  if (calendarPython) {
    const listed = await rpc(second, "chat.send", { session_key: "desktop:todo-restart", content: "list-remote-todos" });
    assert.ok(listed.reply.includes("remote-shared-todo"));
  }
  report.host_restart = true;
  console.log(JSON.stringify(report));
} finally {
  await first.stop(); await second.stop(); await stop();
  writeFileSync(resolve(workspace, "report.json"), JSON.stringify(report, null, 2));
}

// node --import tsx tests/e2e/cancel_write_crash.mjs
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readdirSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { DesktopBridgeClient } from "../../apps/desktop/src/bridge/bridgeClient.ts";

mkdirSync(resolve(".tmp"), { recursive: true });
const reports = [];
for (const fault of ["before_append", "after_commit", "before_cleanup"]) {
  const workspace = mkdtempSync(resolve(".tmp/cancel-crash-"));
  const base = {
    executable: resolve(process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python"),
    args: [resolve("tests/e2e/desktop_lifecycle_backend.py"), workspace], cwd: process.cwd(),
  };
  let client = new DesktopBridgeClient({ ...base, args: [...base.args, `--crash-write=${fault}`] });
  let calls = 0;
  client.on("event", e => { if (e.method === "probe.provider") calls++; });
  const rpc = async (method, payload = {}) => {
    const r = await client.invoke({ method, payload });
    assert.equal(r.error, null, r.error?.message);
    return r.payload;
  };
  const key = "desktop:crash-recovery";
  const expected = [["user", "hold-recovery original"], ["assistant", "retained partial"]];
  try {
    await client.start();
    const original = client.invoke({ method: "chat.send", payload: { session_key: key, content: "hold-recovery original", media: ["retained.txt"] } });
    for (let n = 0; calls === 0 && n < 400; n++) await delay(25);
    assert.equal(calls, 1);
    await rpc("chat.cancel", { session_key: key });
    assert.match((await original).error?.message ?? "", /injected failure/);
    const journal = resolve(workspace, "private_runtime/pending_chat_writes");
    assert.equal(readdirSync(journal).filter(n => n.endsWith(".json")).length, 1);
    const crashed = await client.invoke({ method: "probe.crash", payload: {} });
    assert.equal(crashed.error?.code, "bridge_exit");
    await client.stop();
    client = new DesktopBridgeClient(base);
    await client.start();
    assert.equal((await rpc("health")).pending_chat_writes, 0);
    let restored = (await rpc("session.get", { session_key: key })).messages;
    assert.deepEqual(restored.map(m => [m.role, m.content]), expected);
    assert.deepEqual(restored[0].media, ["retained.txt"]);
    assert.equal(restored[1].reasoning_content, "retained reasoning");
    const originalIds = restored.map(m => m.id);
    assert.equal(readdirSync(journal).filter(n => n.endsWith(".json")).length, 0);
    expected.push(["user", "continue"], ["assistant", "verified reply"]);
    restored = (await rpc("chat.send", { session_key: key, content: "continue" })).session.messages;
    assert.deepEqual(restored.map(m => [m.role, m.content]), expected);
    assert.deepEqual(restored.slice(0, 2).map(m => m.id), originalIds);
    const ids = restored.map(m => m.id);
    await client.stop();
    client = new DesktopBridgeClient(base);
    await client.start();
    restored = (await rpc("session.get", { session_key: key })).messages;
    assert.deepEqual(restored.map(m => m.id), ids);
    assert.deepEqual(restored.map(m => [m.role, m.content]), expected);
    reports.push({ fault, abrupt_exit: true, recovered_before_retry: true, stable_ids_after_second_restart: true });
  } finally { await client.stop(); }
}
writeFileSync(resolve(".tmp/cancel-write-crash-result.json"), JSON.stringify(reports, null, 2));
console.log(JSON.stringify(reports));

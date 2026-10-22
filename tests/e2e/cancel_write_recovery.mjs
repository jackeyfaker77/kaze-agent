// Run from the repository root: node --import tsx tests/e2e/cancel_write_recovery.mjs
// Actual desktop bridge, Python process and SQLite; only the model and write fault are fixtures.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { DesktopBridgeClient } from "../../apps/desktop/src/bridge/bridgeClient.ts";

mkdirSync(resolve(".tmp"), { recursive: true });
const workspace = mkdtempSync(resolve(".tmp/cancel-write-recovery-"));
const attachment = resolve(workspace, "input.txt");
writeFileSync(attachment, "Original attachment retained across retry and restart.");
const command = {
  executable: resolve(process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python"),
  args: [resolve("tests/e2e/desktop_lifecycle_backend.py"), workspace, "--recover-write"],
  cwd: process.cwd(),
};
let client = new DesktopBridgeClient(command);
let calls = 0;
client.on("event", (event) => {
  if (event.method === "probe.provider") calls++;
});
const rpc = async (method, payload = {}) => {
  const result = await client.invoke({ method, payload });
  assert.equal(result.error, null, result.error?.message);
  return result.payload;
};
const key = "desktop:write-recovery";
const expected = [
  ["user", "hold-recovery original"], ["assistant", "retained partial"],
  ["user", "continue"], ["assistant", "verified reply"],
];
function check(messages, ids) {
  assert.deepEqual(messages.map((m) => [m.role, m.content]), expected);
  assert.equal(new Set(messages.map((m) => m.id)).size, expected.length);
  assert.deepEqual(messages[0].media, [attachment]);
  assert.equal(messages[1].reasoning_content, "retained reasoning");
  assert.equal(messages[1].metadata.interrupted_reply, true);
  if (ids) assert.deepEqual(messages.map((m) => m.id), ids);
}
try {
  await client.start();
  const sending = client.invoke({ method: "chat.send", payload: {
    session_key: key, content: "hold-recovery original", media: [attachment],
  } });
  for (let attempt = 0; calls === 0 && attempt < 400; attempt++) await delay(25);
  assert.equal(calls, 1, "original model call started");
  assert.equal((await rpc("chat.cancel", { session_key: key })).cancelled, true);
  assert.match((await sending).error?.message ?? "", /injected failure after SQLite commit/);
  const recovered = await rpc("chat.send", { session_key: key, content: "continue" });
  check(recovered.session.messages);
  assert.equal(calls, 2, "recovery must not replay the original model call");
  const ids = recovered.session.messages.map((m) => m.id);

  // Use a NEW client and Python process, with fault injection disabled, to reopen the same database.
  await client.stop();
  client = new DesktopBridgeClient({ ...command, args: command.args.slice(0, 2) });
  await client.start();
  check((await rpc("session.get", { session_key: key })).messages, ids);
  expected.push(["user", "after restart"], ["assistant", "verified reply"]);
  const next = await rpc("chat.send", { session_key: key, content: "after restart" });
  check(next.session.messages);
  const nextIds = next.session.messages.map((m) => m.id);
  await client.stop();
  client = new DesktopBridgeClient({ ...command, args: command.args.slice(0, 2) });
  await client.start();
  check((await rpc("session.get", { session_key: key })).messages, nextIds);
  const report = {
    workspace, fault: "SQLite committed the first message then raised before returning its ID",
    original_input_and_partial_reply_recovered: true,
    attachment_and_reasoning_retained: true, original_model_call_replayed: false,
    python_process_restarts: 2, stable_message_ids: true,
    final_messages: expected.length, duplicates: 0,
    model: "fixture", user_configuration: "not loaded or changed",
  };
  writeFileSync(resolve(".tmp/cancel-write-recovery-result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  await client.stop();
}

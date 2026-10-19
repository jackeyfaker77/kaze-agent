// Run from the repository root: node --import tsx tests/e2e/desktop_lifecycle.mjs
// Real IPC, persistence, controller and playback; cloud/device providers are fixtures.
import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, writeFileSync } from "node:fs";
import { resolve } from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { DesktopBridgeClient } from "../../apps/desktop/src/bridge/bridgeClient.ts";
import { DesktopVoiceController } from "../../apps/desktop/src/voice/controller.ts";
import { BrowserVoicePlayback } from "../../apps/desktop/src/voice/playback.ts";
import { createVoicePlaybackCallbacks, handleVoiceBridgeEvent } from "../../apps/desktop/src/voice/bridgeEvents.ts";

mkdirSync(resolve(".tmp"), { recursive: true });
const workspace = mkdtempSync(resolve(".tmp/lifecycle-probe-"));
const client = new DesktopBridgeClient({
  executable: resolve(process.platform === "win32" ? ".venv/Scripts/python.exe" : ".venv/bin/python"),
  args: [resolve("tests/e2e/desktop_lifecycle_backend.py"), workspace],
  cwd: process.cwd(),
});
const frames = [];
const calls = [];
let controller;
let playback;
client.on("event", (event) => {
  frames.push(event);
  if (event.method === "probe.provider") calls.push(event.payload.content);
  if (controller) handleVoiceBridgeEvent(event, controller, playback);
});
const rpc = (method, payload = {}) => client.invoke({ method, payload });
async function until(condition) {
  for (let attempt = 0; attempt < 400; attempt++) {
    if (condition()) return;
    await delay(25);
  }
  throw new Error("lifecycle probe timed out");
}
const report = { workspace };
try {
  await client.start();
  const first = rpc("chat.send", { session_key: "desktop:a", content: "hold-a" });
  const second = rpc("chat.send", { session_key: "desktop:b", content: "hold-b" });
  await until(() => calls.length === 2);
  // Both real integration slots are occupied. The next request must be cancellable.
  const queued = rpc("chat.send", {
    session_key: "desktop:q", content: "queued input", media: ["queued.png"],
  });
  assert.equal((await rpc("chat.cancel", { session_key: "desktop:q" })).payload.cancelled, true);
  const stopped = await queued;
  assert.equal(stopped.payload.cancelled, true);
  assert.equal(stopped.payload.session.messages[0].media[0], "queued.png");
  assert.equal(calls.length, 2);
  for (const key of ["a", "b"]) {
    assert.equal((await rpc("chat.cancel", { session_key: `desktop:${key}` })).payload.cancelled, true);
  }
  for (const result of await Promise.all([first, second])) {
    assert.equal(result.payload.cancelled, true);
    assert.equal(result.payload.session.messages.length, 1);
  }
  assert.equal((await rpc("chat.send", { session_key: "desktop:q", content: "retry" })).payload.reply, "verified reply");
  assert.equal(calls.length, 3);
  assert.ok(String(calls[2]).includes("retry"));
  report.cancel = {
    queued: true, model_calls_for_cancelled_queue: 0,
    before_first_output_preserved: true, retry: true,
  };

  let completed = 0;
  controller = new DesktopVoiceController({
    recorder: {
      start: async () => {}, stop: async () => new Uint8Array([1, 2]), cancel: async () => {},
    },
    bridge: {
      invoke: async (request) => {
        if (request.method === "voice.transcribe") {
          return {
            id: "fixture-asr", type: "response", method: request.method,
            payload: { text: "voice probe" }, error: null,
          };
        }
        const result = await client.invoke(request);
        frames.push(result);
        completed++;
        return result;
      },
    },
    isEnabled: () => true,
    sessionKey: () => "desktop:voice",
    publishState: () => {},
    onNewInput: (_old, next) => playback.beginTurn(next),
  });
  playback = new BrowserVoicePlayback(() => {
    throw new Error("TTS disabled must not open an audio player");
  }, createVoicePlaybackCallbacks(controller));
  for (let turn = 1; turn <= 2; turn++) {
    assert.equal(controller.startPress("hotkey"), true);
    await delay(350);
    controller.release();
    await until(() => completed === turn);
    await delay(0);
    assert.equal(controller.currentState.kind, "idle");
    assert.equal(controller.currentTurnId, null);
  }
  assert.deepEqual(
    frames.filter((frame) => frame.method.startsWith("voice.") || frame.type === "response").map((frame) => frame.method),
    ["voice.reply.started", "voice.tts.finished", "chat.send", "voice.reply.started", "voice.tts.finished", "chat.send"],
  );
  report.voice = {
    real_bridge_client_and_server: true, real_controller_and_playback_queue: true,
    consecutive_turns: 2, final_state: controller.currentState.kind,
    cloud_asr: "stubbed; configuration unchanged", cloud_tts: "disabled",
  };
  controller.dispose();
  playback.dispose();
  controller = null;

  // Restart the actual Python process so the assertions cannot pass from cached sessions.
  await client.stop();
  await client.start();
  for (const [key, count] of [["a", 1], ["b", 1], ["q", 3], ["voice", 4]]) {
    const reopened = await rpc("session.get", { session_key: `desktop:${key}` });
    assert.equal(reopened.payload.messages.length, count);
    if (key === "q") assert.deepEqual(reopened.payload.messages[0].media, ["queued.png"]);
  }
  report.restart_sqlite_reload = true;
  writeFileSync(resolve(".tmp/lifecycle-probe-result.json"), JSON.stringify(report, null, 2));
  console.log(JSON.stringify(report));
} finally {
  controller?.dispose();
  playback?.dispose();
  await client.stop();
}

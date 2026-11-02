import assert from "node:assert/strict";
import test from "node:test";
import { resolve } from "node:path";
import { ResidentModelSettings } from "./residentSettings.js";
import type { SettingsFormData } from "./bridge/shared.js";
import type { BridgeClient } from "./bridge/bridgeContract.js";

const form = { modelRevision: "revision-1", models: { registrations: [{ id: "model-2", model: "second" }] } } as SettingsFormData;
function fixture(error?: {code: string; message: string}) {
  const calls: {method: string; payload: Record<string, unknown>}[] = [];
  const bridge = { localWorkspace: resolve("workspace"), invoke: async (request: typeof calls[number]) => {
    calls.push(request);
    return request.method === "health"
      ? { payload: {ok:true,local_desktop_version:2,workspace:resolve("workspace"),model_revision:"revision-1"} }
      : { payload: {ok:true,health:{ok:true,message:"switched"}}, error };
  } } as unknown as BridgeClient;
  return {calls, manager:new ResidentModelSettings(bridge)};
}

test("resident model save uses the Host revision and does not restart any process", async () => {
  const {calls, manager} = fixture();
  assert.equal((await manager.save(form)).ok,true);
  assert.deepEqual(calls.map(c=>c.method),["health","models.save"]);
  assert.deepEqual(calls[1].payload,{expected_revision:"revision-1",registrations:form.models.registrations});
});

test("unknown model-save outcomes are not resent or rolled back by the desktop", async () => {
  const {calls, manager} = fixture({code:"remote_outcome_unknown",message:"核对服务器记录"});
  await assert.rejects(manager.save(form),/核对/);
  assert.deepEqual(calls.map(c=>c.method),["health","models.save"]);
});

import test from "node:test";
import assert from "node:assert/strict";
import { resolve } from "node:path";
import { verifyLocalHost } from "./localHostCapabilities.js";
import type { BridgeClient } from "./bridgeContract.js";

test("local privileges require installed provenance, supported Host and matching workspace", async () => {
  const payload = { ok: true, local_desktop_version: 2, workspace: resolve("workspace") };
  const bridge = { localWorkspace: resolve("workspace"), invoke: async () => ({ payload }) } as unknown as BridgeClient;
  assert.equal(await verifyLocalHost(bridge), payload);
  await assert.rejects(verifyLocalHost({ ...bridge, localWorkspace: undefined } as BridgeClient), /本机常驻/);
  payload.workspace = resolve("another-workspace");
  await assert.rejects(verifyLocalHost(bridge), /不匹配/);
  payload.workspace = resolve("workspace"); payload.local_desktop_version = 0;
  await assert.rejects(verifyLocalHost(bridge), /需要更新/);
});

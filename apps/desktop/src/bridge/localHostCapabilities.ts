import { resolve } from "node:path";
import type { BridgeClient } from "./bridgeContract.js";

/** A loopback URL alone may be an SSH tunnel. Only installed connections qualify. */
export async function verifyLocalHost(bridge: BridgeClient) {
  if (!bridge.localWorkspace) throw new Error("此操作需要本机常驻 Host");
  const response = await bridge.invoke({ method: "health", payload: {} });
  if (response.error) throw new Error(response.error.message);
  const health = response.payload;
  if (health.ok !== true || health.local_desktop_version !== 2 || typeof health.workspace !== "string"
      || resolve(health.workspace).toLowerCase() !== resolve(bridge.localWorkspace).toLowerCase()) {
    throw new Error("本机 Host 需要更新，或连接的工作区不匹配");
  }
  return health;
}

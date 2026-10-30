import { existsSync, readFileSync } from "node:fs";
import { isAbsolute, resolve } from "node:path";
import { remoteConnectionFromEnvironment, type RemoteConnection } from "./remoteBridgeClient.js";

/** Explicit environment wins. A broken installed connection must never spawn a second backend. */
export function desktopRuntimeConnection(env: NodeJS.ProcessEnv, workspace: string): RemoteConnection | null {
  if (env.KAZE_RUNTIME_URL || env.KAZE_RUNTIME_TOKEN_FILE) return remoteConnectionFromEnvironment(env);
  const file = resolve(workspace, ".desktop", "runtime-connection.json");
  if (!existsSync(file)) return null;
  const data = JSON.parse(readFileSync(file, "utf8").replace(/^\uFEFF/, ""));
  if (!data || data.version !== 1 || typeof data.url !== "string" || typeof data.tokenFile !== "string" || !isAbsolute(data.tokenFile)) {
    throw new Error("本机 Runtime Host 连接文件无效，请修复部署或显式卸载；不会启动内嵌后端");
  }
  const url = new URL(data.url);
  if (url.protocol !== "ws:" || url.hostname !== "127.0.0.1") throw new Error("本机 Runtime Host 必须连接 127.0.0.1");
  return remoteConnectionFromEnvironment({ KAZE_RUNTIME_URL: data.url, KAZE_RUNTIME_TOKEN_FILE: data.tokenFile });
}

import type { EventEmitter } from "node:events";
import type { DesktopBridgeClient } from "./bridgeClient.js";

export type BridgeClient = Pick<DesktopBridgeClient, "start" | "stop" | "restart" | "invoke" | "isRunning" | "getLastError"> & Pick<EventEmitter, "on"> & { readonly mode?: "remote" };

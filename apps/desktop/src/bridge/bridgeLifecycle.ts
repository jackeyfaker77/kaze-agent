import { BrowserWindow } from "electron";
import type { BridgeClient } from "./bridgeContract.js";
import type { LocalAssetRegistry } from "../assets/localAssetRegistry.js";
import type { BridgeEvent, LocalAssetTransport } from "./shared.js";

/** Starts the Python bridge process and logs startup failures at the app boundary. */
export async function startBridge(bridge: BridgeClient): Promise<void> {
  try {
    await bridge.start();
  } catch (error) {
    console.error("[desktop] bridge start failed", error);
  }
}

/** Forwards bridge events to every open renderer window. */
export function wireBridgeEvents(
  bridge: BridgeClient,
  localAssets: LocalAssetRegistry,
  onEvent?: (event: BridgeEvent) => void,
): void {
  bridge.on("event", (payload) => {
    onEvent?.(payload);
    const transport: LocalAssetTransport<BridgeEvent> = {
      value: payload,
      assets: bridge.mode === "remote" ? [] : localAssets.grantTrustedPayload(payload.payload),
    };
    for (const window of BrowserWindow.getAllWindows()) {
      window.webContents.send("desktop:event", transport);
    }
  });
  bridge.on("exit", (message) => {
    const transport: LocalAssetTransport<BridgeEvent> = {
      value: {
        id: "bridge-exit",
        type: "event",
        method: "bridge.exit",
        payload: { message },
      },
      assets: [],
    };
    for (const window of BrowserWindow.getAllWindows()) {
      window.webContents.send("desktop:event", transport);
    }
  });
}

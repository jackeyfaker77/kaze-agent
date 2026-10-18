import type { BridgeEvent } from "../../../src/bridge/shared";
import type { Session } from "./api";

/** Refresh the catalog for every commit, but replace only the open conversation. */
export function handleCommittedEvent(
  event: BridgeEvent,
  currentKey: string,
  refresh: () => void,
  updateCurrent: (session: Session | undefined) => void,
): boolean {
  if (event.method !== "chat.done" && event.method !== "message.pushed") return false;
  refresh();
  if (event.payload.session_key === currentKey) updateCurrent(event.payload.session as Session | undefined);
  return true;
}

import type { DesktopBridgeClient } from "../bridge/bridgeClient.js";
import type { BridgeEvent } from "../bridge/shared.js";

type SessionObservationBubbleTarget = {
  acceptSessionReply(sessionKey: string, reply: string): void;
};

/** Shows ordinary conversation replies on the application pet. */
export function wireSessionReplyBubbles(
  bridge: DesktopBridgeClient,
  target: SessionObservationBubbleTarget,
): void {
  bridge.on("event", (event: BridgeEvent) => {
    if (event.method === "message.pushed") {
      publishReply(target, String(event.payload.session_key ?? ""), String(event.payload.content ?? ""));
      return;
    }
    if (event.method === "chat.done") {
      const sessionKey = typeof event.payload.session_key === "string" ? event.payload.session_key : "";
      const reply = typeof event.payload.reply === "string" ? event.payload.reply : "";
      publishReply(target, sessionKey, reply);
      return;
    }
    if (event.method !== "session.updated" || event.id !== "proactive") return;
    const session = event.payload.session;
    const sessionValue = session && typeof session === "object"
      ? session as { metadata?: unknown; messages?: unknown }
      : null;
    const sessionMetadata = sessionValue?.metadata;
    const sessionSessionKey = sessionMetadata && typeof sessionMetadata === "object"
      && typeof (sessionMetadata as { session_key?: unknown }).session_key === "string"
      ? (sessionMetadata as { session_key: string }).session_key
      : "";
    const sessionKey = typeof event.payload.session_key === "string"
      ? event.payload.session_key
      : sessionSessionKey;

    // New incremental events carry the changed message directly. Keep the old
    // full-snapshot fallback for older bridge versions and persisted fixtures.
    const changedMessage = event.payload.message;
    const message = changedMessage && typeof changedMessage === "object"
      ? changedMessage
      : sessionValue?.messages && Array.isArray(sessionValue.messages)
        ? sessionValue.messages.at(-1)
        : null;
    if (!message || typeof message !== "object") return;
    const messageValue = message as {
      role?: unknown;
      content?: unknown;
      metadata?: unknown;
      proactive?: unknown;
    };
    const messageMetadata = messageValue.metadata;
    const proactive = messageValue.proactive === true
      || (messageMetadata && typeof messageMetadata === "object"
        && (messageMetadata as { proactive?: unknown }).proactive === true);
    if (messageValue.role !== "assistant" || !proactive) return;
    const reply = typeof messageValue.content === "string" ? messageValue.content : "";
    publishReply(target, sessionKey, reply);
  });
}

function publishReply(target: SessionObservationBubbleTarget, sessionKey: string, reply: string): void {
    if (sessionKey && reply.trim()) target.acceptSessionReply(sessionKey, reply);
}

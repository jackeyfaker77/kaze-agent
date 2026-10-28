import type { BridgeEvent } from "../../../src/bridge/shared";
import type { Message, Session } from "./api";

export type MessagePart =
  | { kind: "thinking"; id: string; text: string; done: boolean }
  | { kind: "text"; id: string; text: string }
  | { kind: "tool"; id: string; name: string; args: unknown; finalArgs?: unknown; status: string; result?: string };
export type TurnDisplay = {
  requestId: string;
  parts: MessagePart[];
  phase: "running" | "awaiting-history";
  /** No source offsets in chat.delta: freeze on a transport gap, never guess overlap. */
  frozen?: boolean;
};
type SessionDisplay = { turns: TurnDisplay[]; settled: string[] };
export type ConversationDisplay = Record<string, SessionDisplay>;
export type DisplayAction =
  | { type: "event"; event: BridgeEvent }
  | { type: "snapshot"; session: Session }
  | { type: "stop"; sessionKey: string; requestId: string; rpcIdentity?: boolean }
  | { type: "disconnect" }
  | { type: "forget"; sessionKey: string };

export function record(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}
function fields(message: Message) { return { ...record(message.extra), ...message }; }
export function messageRequestId(message: Message): string {
  const id = record(fields(message).metadata).request_id;
  return typeof id === "string" ? id : "";
}
export function assistantDisplayKey(message: Message, index: number): string {
  return messageRequestId(message) || message.id || `legacy:${index}`;
}
export function thinkingDuration(message: Message): number | undefined {
  const value = record(record(fields(message).metadata).turn_metrics).thinking_duration_ms;
  return typeof value === "number" && Number.isFinite(value) && value >= 0 ? value : undefined;
}
export function interruptedMessage(message: Message): boolean {
  return record(fields(message).metadata).interrupted_reply === true;
}
function text(value: unknown): string { return typeof value === "string" ? value : ""; }
function printable(value: unknown): string {
  return typeof value === "string" ? value : value === undefined ? "" : JSON.stringify(value, null, 2);
}
function closeThinking(parts: MessagePart[]): MessagePart[] {
  return parts.map(part => part.kind === "thinking" && !part.done ? { ...part, done: true } : part);
}
function appendText(parts: MessagePart[], kind: "thinking" | "text", value: string): MessagePart[] {
  if (!value) return parts;
  const previous = parts.at(-1);
  if (previous?.kind === kind && (previous.kind !== "thinking" || !previous.done)) {
    return [...parts.slice(0, -1), { ...previous, text: previous.text + value }];
  }
  const id = `${kind}:${parts.filter(part => part.kind === kind).length}`;
  return [...parts, kind === "thinking" ? { kind, id, text: value, done: false } : { kind, id, text: value }];
}

/** History has round-level facts, not a token timeline. Do not invent interleaving or per-block timings. */
export function partsFromMessage(message: Message): MessagePart[] {
  const data = fields(message);
  let parts: MessagePart[] = [];
  const groups = Array.isArray(data.tool_chain) ? data.tool_chain : [];
  groups.forEach((raw, index) => {
    const group = record(raw);
    parts = appendText(parts, "thinking", text(group.reasoning_content));
    parts = closeThinking(parts);
    parts = appendText(parts, "text", text(group.text));
    const calls = Array.isArray(group.calls) ? group.calls : [];
    calls.forEach((rawCall, callIndex) => {
      const call = record(rawCall);
      parts.push({ kind: "tool", id: `tool:${index + 1}:${text(call.call_id) || `legacy:${callIndex}`}`,
        name: text(call.name) || "工具", args: call.arguments, finalArgs: call.final_arguments,
        status: text(call.status) || "unknown", result: printable(call.result) });
    });
  });
  parts = appendText(parts, "thinking", text(data.reasoning_content));
  parts = closeThinking(parts);
  parts = appendText(parts, "text", message.content);
  return parts;
}

/** Last visible text is the answer; text followed by tools remains process commentary. */
export function splitMessageParts(parts: MessagePart[]): { process: MessagePart[]; answer?: Extract<MessagePart, { kind: "text" }> } {
  let last = parts.length - 1;
  while (last >= 0 && parts[last].kind === "thinking") last--;
  const candidate = parts[last];
  if (candidate?.kind !== "text") return { process: parts };
  return { process: parts.filter((_, index) => index !== last), answer: candidate };
}

function snapshot(state: ConversationDisplay, session: Session): ConversationDisplay {
  const current = state[session.session_key] ?? { turns: [], settled: [] };
  const ids = session.messages.filter(message => message.role === "assistant").map(messageRequestId).filter(Boolean);
  const settled = [...new Set([...current.settled, ...ids])];
  const turns = current.turns.filter(turn => !settled.includes(turn.requestId));
  if (settled.length === current.settled.length && turns.length === current.turns.length) return state;
  return { ...state, [session.session_key]: { turns, settled } };
}
function stopTurn(turn: TurnDisplay, frozen = false): TurnDisplay {
  return { ...turn, phase: "awaiting-history", frozen: frozen || turn.frozen, parts: closeThinking(turn.parts).map(part =>
    part.kind === "tool" && part.status === "running" ? { ...part, status: "unknown" } : part) };
}

/** Pure display reducer. It never invokes RPC, a model, a tool, or a transport send. */
export function conversationDisplayReducer(state: ConversationDisplay, action: DisplayAction): ConversationDisplay {
  if (action.type === "snapshot") return snapshot(state, action.session);
  if (action.type === "forget") {
    const next = { ...state }; delete next[action.sessionKey]; return next;
  }
  if (action.type === "disconnect") return Object.fromEntries(Object.entries(state).map(([key, value]) =>
    [key, { ...value, turns: value.turns.map(turn => stopTurn(turn, true)) }]));
  if (action.type === "stop") {
    const current = state[action.sessionKey];
    if (!current) return state;
    return { ...state, [action.sessionKey]: { ...current, turns: current.turns.map(turn =>
      turn.requestId === action.requestId || (action.rpcIdentity && turn.requestId.endsWith(`:${action.requestId}`)) ? stopTurn(turn, true) : turn) } };
  }
  const event = action.event, payload = event.payload;
  const key = text(payload.session_key);
  if (!key) return state;
  if (event.method === "chat.done" || event.method === "message.pushed") {
    const session = payload.session as Session | undefined;
    let next = session?.session_key === key && Array.isArray(session.messages) ? snapshot(state, session) : state;
    if (event.method === "chat.done") next = conversationDisplayReducer(next, { type: "stop", sessionKey: key, requestId: event.id });
    return next;
  }
  if (!["chat.delta", "chat.tool.started", "chat.tool.completed"].includes(event.method) || !event.id) return state;
  const current = state[key] ?? { turns: [], settled: [] };
  if (current.settled.includes(event.id)) return state;
  const previous = current.turns.find(turn => turn.requestId === event.id);
  if (previous?.frozen) return state;
  let parts = previous?.parts ?? [];
  if (event.method === "chat.delta") {
    parts = appendText(parts, "thinking", text(payload.thinking_delta));
    if (text(payload.content_delta)) parts = appendText(closeThinking(parts), "text", text(payload.content_delta));
  } else {
    const callId = text(payload.call_id);
    if (!callId) return state;
    const id = `tool:${payload.iteration}:${callId}`;
    const existing = parts.find(part => part.id === id && part.kind === "tool");
    const started = event.method === "chat.tool.started";
    const tool: Extract<MessagePart, { kind: "tool" }> = {
      kind: "tool", id, name: text(payload.tool_name) || "工具", args: payload.arguments,
      ...(existing?.kind === "tool" ? existing : {}),
      ...(started ? {} : { finalArgs: payload.final_arguments, result: text(payload.result_preview) }),
      status: started ? (existing?.kind === "tool" ? existing.status : "running") : text(payload.status) || "unknown",
    };
    // A repeated start enriches an output-first record, never regresses its status.
    tool.name = text(payload.tool_name) || tool.name;
    tool.args = payload.arguments ?? tool.args;
    parts = existing ? parts.map(part => part.id === id ? tool : part) : [...closeThinking(parts), tool];
  }
  if (parts === previous?.parts || (!previous && !parts.length)) return state;
  const turn: TurnDisplay = { requestId: event.id, parts, phase: "running" };
  return { ...state, [key]: { ...current, turns: previous ? current.turns.map(item => item === previous ? turn : item) : [...current.turns, turn] } };
}

/** Stream and durable message share a React identity through the handoff. */
export function conversationRows(session: Session | null, state: ConversationDisplay, sessionKey: string) {
  const messages = session?.messages ?? [];
  const occurrences = new Map<string, number>();
  const rows = messages.filter(message => message.role === "user" || message.role === "assistant").map((message, index) => {
    const base = `${sessionKey}:${message.role}:${assistantDisplayKey(message, index)}`;
    const occurrence = occurrences.get(base) ?? 0;
    occurrences.set(base, occurrence + 1);
    return { key: occurrence ? `${base}:message:${message.id ?? occurrence}` : base, message, turn: undefined as TurnDisplay | undefined };
  });
  const committed = new Set(messages.filter(message => message.role === "assistant").map(messageRequestId));
  for (const turn of state[sessionKey]?.turns ?? []) {
    if (committed.has(turn.requestId)) continue;
    const row = { key: `${sessionKey}:assistant:${turn.requestId}`, message: { role: "assistant", content: "" }, turn };
    // An older unresolved turn belongs next to its persisted input, not below a later answer.
    const anchor = rows.findIndex(item => item.message.role === "user" && messageRequestId(item.message) === turn.requestId);
    if (anchor >= 0) rows.splice(anchor + 1, 0, row);
    else rows.push(row);
  }
  return rows;
}

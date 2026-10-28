import assert from "node:assert/strict";
import { test } from "node:test";
import type { BridgeEvent } from "../../../src/bridge/shared";
import type { Message, Session } from "./api";
import { conversationDisplayReducer as reduce, conversationRows, partsFromMessage, splitMessageParts, thinkingDuration, type ConversationDisplay } from "./conversationDisplay";

const event = (method: string, payload: Record<string, unknown> = {}, id = "rpc-a", sessionKey = "desktop:a"): BridgeEvent =>
  ({ id, type: "event", method, payload: { session_key: sessionKey, ...payload } });
const apply = (state: ConversationDisplay, value: BridgeEvent) => reduce(state, { type: "event", event: value });
const delta = (thinking = "", content = "", id = "rpc-a", key = "desktop:a") => event("chat.delta", { thinking_delta: thinking, content_delta: content }, id, key);
const tool = (method = "chat.tool.started", extra: Record<string, unknown> = {}) => event(method, { iteration: 1, call_id: "call-1", tool_name: "shell", arguments: { command: "echo hi" }, ...extra });
const message = (id = "rpc-a", content = "answer"): Message => ({ role: "assistant", content, metadata: { request_id: id } });
const snapshot = (messages = [message()], sessionKey = "desktop:a"): Session => ({ session_key: sessionKey, title: "Test", messages });

test("thinking/text/tools preserve adjacent runs and finish thinking at boundaries", () => {
  let state = apply({}, delta("think"));
  state = apply(state, delta(" more"));
  state = apply(state, delta("", "checking"));
  state = apply(state, tool());
  state = apply(state, delta("next", "answer"));
  const parts = state["desktop:a"].turns[0].parts;
  assert.deepEqual(parts.map(part => part.kind), ["thinking", "text", "tool", "thinking", "text"]);
  assert.equal(parts[0].kind === "thinking" && parts[0].text, "think more");
  assert.equal(parts[0].kind === "thinking" && parts[0].done, true);
  assert.equal(splitMessageParts(parts).answer?.text, "answer");
  assert.equal(splitMessageParts(parts).process.length, 4);
});

test("duplicate starts and output-before-start have one tool and retain terminal status and final args", () => {
  let state = apply({}, tool("chat.tool.completed", { status: "denied", final_arguments: { command: "blocked" }, result_preview: "denied" }));
  state = apply(state, tool());
  state = apply(state, tool());
  const parts = state["desktop:a"].turns[0].parts;
  assert.equal(parts.length, 1);
  assert.equal(parts[0].kind === "tool" && parts[0].status, "denied");
  assert.deepEqual(parts[0].kind === "tool" && parts[0].finalArgs, { command: "blocked" });
});

test("tool completion matches call and iteration without clearing another parallel tool", () => {
  let state = apply({}, tool());
  state = apply(state, tool("chat.tool.started", { call_id: "call-2" }));
  state = apply(state, tool("chat.tool.started", { iteration: 2 }));
  state = apply(state, tool("chat.tool.completed", { status: "success", result_preview: "hi" }));
  assert.deepEqual(state["desktop:a"].turns[0].parts.map(part => part.kind === "tool" && part.status), ["success", "running", "running"]);
});

test("sessions and late predecessor turns remain independent", () => {
  let state = apply({}, delta("", "old"));
  state = apply(state, delta("", "new", "rpc-b"));
  state = apply(state, delta("", "elsewhere", "rpc-c", "desktop:b"));
  state = apply(state, event("chat.done", { session: snapshot() }));
  state = apply(state, delta("", "late old"));
  assert.deepEqual(state["desktop:a"].turns.map(turn => turn.requestId), ["rpc-b"]);
  assert.equal(state["desktop:b"].turns[0].parts[0].kind === "text" && state["desktop:b"].turns[0].parts[0].text, "elsewhere");
  assert.equal(conversationRows(null, state, "desktop:b").length, 1);
});

test("unrelated pushes and missing commit snapshots do not erase a live turn", () => {
  const state = apply({}, delta("", "partial"));
  let next = apply(state, event("message.pushed", { session: snapshot([message("proactive", "reminder")]) }, "push"));
  assert.deepEqual(next["desktop:a"].turns, state["desktop:a"].turns);
  next = apply(next, event("chat.done"));
  assert.equal(next["desktop:a"].turns[0].phase, "awaiting-history");
  assert.equal(next["desktop:a"].turns[0].parts.length, 1);
});

for (const order of ["event-first", "response-first"] as const) {
  test(`durable handoff is idempotent with ${order} and stable rendering identity`, () => {
    let state = apply({}, delta("", "answer"));
    const liveKey = conversationRows(null, state, "desktop:a")[0].key;
    const saved = snapshot();
    const actions = [
      { type: "event" as const, event: event("chat.done", { session: saved }) },
      { type: "snapshot" as const, session: saved },
    ];
    if (order === "response-first") actions.reverse();
    for (const action of [...actions, ...actions]) state = reduce(state, action);
    state = apply(state, delta("", "late"));
    const rows = conversationRows(saved, state, "desktop:a");
    assert.equal(rows.length, 1);
    assert.equal(rows[0].key, liveKey);
    assert.equal(rows[0].message.content, "answer");
    assert.equal(state["desktop:a"].turns.length, 0);
  });
}

test("cancel waits for persisted partial history; unknown tools never become successes", () => {
  let state = apply({}, delta("thinking"));
  state = apply(state, tool());
  state = reduce(state, { type: "stop", sessionKey: "desktop:a", requestId: "rpc-a" });
  assert.equal(state["desktop:a"].turns[0].phase, "awaiting-history");
  assert.equal(state["desktop:a"].turns[0].parts[1].kind === "tool" && state["desktop:a"].turns[0].parts[1].status, "unknown");
  state = reduce(state, { type: "snapshot", session: snapshot([{ ...message(), metadata: { request_id: "rpc-a", interrupted_reply: true } }]) });
  assert.equal(state["desktop:a"].turns.length, 0);
});

test("disconnect freezes offset-free streams until matching history, never content-based dedupe", () => {
  let state = apply({}, delta("", "ha"));
  state = apply(state, delta("", "ha"));
  assert.equal(state["desktop:a"].turns[0].parts[0].kind === "text" && state["desktop:a"].turns[0].parts[0].text, "haha");
  state = reduce(state, { type: "disconnect" });
  state = reduce(state, { type: "snapshot", session: snapshot([{ role: "user", content: "hello" }]) });
  const frozen = state;
  state = apply(state, delta("", "haha"));
  assert.equal(state, frozen);
  state = reduce(state, { type: "snapshot", session: snapshot([message("rpc-a", "haha!")]) });
  assert.equal(state["desktop:a"].turns.length, 0);
});

test("a remote RPC finishes only its own prefixed stream, leaving a successor client running", () => {
  let state = apply({}, delta("", "first", "host-client:rpc-a"));
  state = apply(state, delta("", "second", "other-client:rpc-b"));
  state = reduce(state, { type: "stop", sessionKey: "desktop:a", requestId: "rpc-a", rpcIdentity: true });
  assert.deepEqual(state["desktop:a"].turns.map(turn => turn.phase), ["awaiting-history", "running"]);
  state = apply(state, delta("", " continued", "other-client:rpc-b"));
  const part = state["desktop:a"].turns[1].parts[0];
  assert.equal(part.kind === "text" && part.text, "second continued");
});

test("history adapter retains round facts, final reasoning, previews and backend duration", () => {
  const saved = { ...message(), reasoning_content: "final thought", metadata: { request_id: "rpc-a", turn_metrics: { thinking_duration_ms: 12500 } }, tool_chain: [
    { reasoning_content: "first thought", text: "checking", calls: [{ call_id: "call-1", name: "shell", arguments: { command: "echo hi" }, final_arguments: { command: "echo hi" }, result: "hi", status: "success" }] },
  ] };
  const parts = partsFromMessage(saved);
  assert.deepEqual(parts.map(part => part.kind), ["thinking", "text", "tool", "thinking", "text"]);
  assert.equal(parts[2].id, "tool:1:call-1");
  assert.equal(thinkingDuration(saved), 12500);
  assert.equal(thinkingDuration(message()), undefined);
  assert.equal(thinkingDuration({ ...message(), metadata: { turn_metrics: { thinking_duration_ms: -10 } } }), undefined);
});

test("legacy extras and plain-text history render without inventing timing or tool success", () => {
  assert.deepEqual(partsFromMessage({ role: "assistant", content: "old answer" }), [{ kind: "text", id: "text:0", text: "old answer" }]);
  const parts = partsFromMessage({ role: "assistant", content: "", extra: { reasoning_content: "old thought", tool_chain: [{ calls: [{ name: "old-tool" }] }] } });
  assert.equal(parts[0].kind === "tool" && parts[0].status, "unknown");
  assert.equal(parts[1].kind === "thinking" && parts[1].text, "old thought");
  assert.equal(splitMessageParts(parts).answer, undefined);
});

test("reducer leaves input state untouched and explicit deletion forgets only the selected session", () => {
  const state = apply({}, delta("", "A"));
  const before = JSON.stringify(state);
  const next = apply(state, delta("", "B"));
  assert.equal(JSON.stringify(state), before);
  const withOther = apply(next, delta("", "C", "rpc-c", "desktop:b"));
  const forgotten = reduce(withOther, { type: "forget", sessionKey: "desktop:a" });
  assert.equal(forgotten["desktop:a"], undefined);
  assert.equal(forgotten["desktop:b"], withOther["desktop:b"]);
});

test("an unresolved earlier turn remains beside its original input after a later turn commits", () => {
  let state = apply({}, delta("", "partial A"));
  state = reduce(state, { type: "stop", sessionKey: "desktop:a", requestId: "rpc-a" });
  const saved = snapshot([
    { role: "user", content: "input A", metadata: { request_id: "rpc-a" } },
    { role: "user", content: "input B", metadata: { request_id: "rpc-b" } },
    message("rpc-b", "answer B"),
  ]);
  const rows = conversationRows(saved, state, "desktop:a");
  assert.deepEqual(rows.map(row => row.turn?.requestId ?? row.message.content), ["input A", "rpc-a", "input B", "answer B"]);
});

test("multiple historical assistant rows with one request do not share a React key", () => {
  const rows = conversationRows(snapshot([message(), { ...message(), id: "second-message" }]), {}, "desktop:a");
  assert.equal(new Set(rows.map(row => row.key)).size, 2);
});

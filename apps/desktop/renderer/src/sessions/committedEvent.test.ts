import assert from "node:assert/strict";
import { test } from "node:test";
import { handleCommittedEvent } from "./committedEvent";
import type { Session } from "./api";

test("a Telegram commit refreshes the sidebar without replacing the open draft or stream", () => {
  const effects: string[] = [];
  const handled = handleCommittedEvent({ id: "1", type: "event", method: "message.pushed", payload: { session_key: "telegram:42" } }, "desktop:draft", () => effects.push("refresh"), () => effects.push("replace"));
  assert.equal(handled, true);
  assert.deepEqual(effects, ["refresh"]);
});

test("the open conversation receives the committed snapshot", () => {
  const session: Session = { session_key: "telegram:42", title: "Telegram", messages: [{ role: "assistant", content: "reply" }] };
  let actual: Session | undefined;
  handleCommittedEvent({ id: "2", type: "event", method: "message.pushed", payload: { session_key: session.session_key, session } }, session.session_key, () => {}, value => { actual = value; });
  assert.equal(actual, session);
});

test("streaming deltas do not reload the workbench on every token", () => {
  const unexpected = () => { assert.fail("not a committed message"); };
  assert.equal(handleCommittedEvent({ id: "3", type: "event", method: "chat.delta", payload: { session_key: "desktop:draft" } }, "desktop:draft", unexpected, unexpected), false);
});

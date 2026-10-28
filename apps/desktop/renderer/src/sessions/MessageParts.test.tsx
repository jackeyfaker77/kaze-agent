import assert from "node:assert/strict";
import { test } from "node:test";
import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { MessageParts } from "./MessageParts";
import type { MessagePart } from "./conversationDisplay";

const parts: MessagePart[] = [
  { kind: "thinking", id: "thinking:0", text: "private thought", done: true },
  { kind: "text", id: "text:0", text: "checking" },
  { kind: "tool", id: "tool:1:a", name: "shell", args: { command: "echo hi" }, status: "running" },
  { kind: "text", id: "text:1", text: "final answer" },
];

test("running process is open, thinking/tool details start folded, and answer is outside the disclosure", () => {
  const html = renderToStaticMarkup(<MessageParts parts={parts} running />);
  assert.match(html, /aria-expanded="true"/);
  assert.match(html, /aria-expanded="false"/);
  assert.match(html, /aria-controls=/);
  assert.match(html, /运行中/);
  assert.match(html, /checking/);
  assert.doesNotMatch(html, /private thought/);
  assert.match(html, /message-answer/);
  assert.match(html, /final answer/);
  assert.match(html, /chat-stream-cursor/);
});

test("settled history collapses process and exposes backend duration without hiding final reply", () => {
  const html = renderToStaticMarkup(<MessageParts parts={parts} durationMs={12500} />);
  assert.match(html, /思考用时 12.5 秒/);
  assert.doesNotMatch(html, /aria-expanded="true"/);
  assert.doesNotMatch(html, /chat-stream-cursor/);
  assert.match(html, /final answer/);
});

test("missing history metrics are not fabricated; unknown display remains visible", () => {
  const html = renderToStaticMarkup(<MessageParts parts={parts} awaitingHistory />);
  assert.doesNotMatch(html, /思考用时/);
  assert.match(html, /等待历史同步/);
  assert.match(html, /未确认的工具结果/);
});

test("tool names and summaries are escaped instead of executing model-produced markup", () => {
  const html = renderToStaticMarkup(<MessageParts running parts={[{ kind: "tool", id: "t", name: "<script>bad()</script>", args: { command: "<img src=x onerror=bad()>" }, status: "blocked" }]} />);
  assert.doesNotMatch(html, /<script>|<img src=x/);
  assert.match(html, /&lt;script&gt;/);
  assert.match(html, /已阻止/);
});

import assert from "node:assert/strict";
import { test } from "node:test";
import { recoverAttachments, recoverDraft } from "./recoverDraft";

test("a failed send restores text and attachments for retry", () => {
  assert.equal(recoverDraft("", "请读取附件"), "请读取附件");
  assert.deepEqual(recoverAttachments([], ["E:/report.pdf"]), ["E:/report.pdf"]);
});

test("failure retains the next draft and newly added files", () => {
  assert.equal(recoverDraft("我补充一点", "上条消息"), "上条消息\n\n我补充一点");
  assert.equal(recoverDraft("我补充一点", ""), "我补充一点");
  const current = ["E:/new.png", "E:/report.pdf"];
  assert.deepEqual(recoverAttachments(current, ["E:/report.pdf"]), ["E:/report.pdf", "E:/new.png"]);
  assert.deepEqual(current, ["E:/new.png", "E:/report.pdf"]);
});

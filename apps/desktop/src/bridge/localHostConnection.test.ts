import assert from "node:assert/strict";
import { test } from "node:test";
import { mkdtempSync, mkdirSync, writeFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { desktopRuntimeConnection } from "./localHostConnection.js";

test("installed localhost Host is used on ordinary desktop launches; absent deployment retains local mode", () => {
  const root = mkdtempSync(join(tmpdir(), "kaze-installed-host-"));
  try {
    assert.equal(desktopRuntimeConnection({}, root), null);
    mkdirSync(join(root, ".desktop"));
    const tokenFile = join(root, "token"); writeFileSync(tokenFile, "a".repeat(48));
    const file = join(root, ".desktop/runtime-connection.json");
    writeFileSync(file, "\uFEFF" + JSON.stringify({ version: 1, url: "ws://127.0.0.1:8765", tokenFile }));
    assert.equal(desktopRuntimeConnection({}, root)?.url, "ws://127.0.0.1:8765/");
    assert.equal(desktopRuntimeConnection({ KAZE_RUNTIME_URL: "wss://example.com", KAZE_RUNTIME_TOKEN_FILE: tokenFile }, root)?.url, "wss://example.com/");
    assert.throws(() => desktopRuntimeConnection({ KAZE_RUNTIME_URL: "wss://example.com" }, root));
    for (const content of ["{", "null", JSON.stringify({version:2}), JSON.stringify({version:1,url:"ws://example.com",tokenFile}), JSON.stringify({version:1,url:"ws://127.0.0.1:8765",tokenFile:"relative"})]) {
      writeFileSync(file, content); assert.throws(() => desktopRuntimeConnection({}, root));
    }
    writeFileSync(file, JSON.stringify({ version:1,url:"ws://127.0.0.1:8765",tokenFile }));
    rmSync(tokenFile); assert.throws(() => desktopRuntimeConnection({}, root));
  } finally { rmSync(root, { recursive: true, force: true }); }
});

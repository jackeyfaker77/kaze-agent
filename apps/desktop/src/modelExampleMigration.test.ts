import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import { configureSettingsConfigPath, loadSettingsData, removeUnconfiguredModelExample, saveSettings } from "./settings.js";
import { ensureDesktopRuntimeConfig } from "./runtimePaths.js";

const qwen = '[[llm.registrations]]\nid = "00000000-0000-4000-a000-000000000002"\nprovider = "qwen"\nmodel = "qwen-vl-plus"\napi_key = "sk-..."\n\n';
const deepseek = '[[llm.registrations]]\nid = "00000000-0000-4000-a000-000000000001"\nprovider = "deepseek"\nmodel = "deepseek-v4-flash"\napi_key = "test-credential"\n\n';

test("startup removes the example before backend loads; migration preserves other bytes", () => {
  const dir = mkdtempSync(join(tmpdir(), "hasaki-model-migration-"));
  const path = join(dir, "config.toml");
  const suffix = '# Keep this comment\n[voice]\nenabled = false\n';
  const original = qwen + deepseek + suffix;
  writeFileSync(path, original);
  ensureDesktopRuntimeConfig({ configPath: path, configTemplatePath: "unused", workspacePath: dir, bridge: { executable: "unused", args: [], cwd: dir } });
  assert.equal(readFileSync(path, "utf8"), deepseek + suffix);
  assert.equal(readFileSync(`${path}.before-example-cleanup.bak`, "utf8"), original);
  removeUnconfiguredModelExample(path);
  assert.equal(readFileSync(path, "utf8"), deepseek + suffix);
});

test("loading and saving a stale settings snapshot cannot resurrect the example", async () => {
  const dir = mkdtempSync(join(tmpdir(), "hasaki-stale-model-"));
  const path = join(dir, "config.toml");
  writeFileSync(path, deepseek + qwen);
  configureSettingsConfigPath(path);
  const form = loadSettingsData().formData;
  assert.deepEqual(form.models.registrations.map(r => r.model), ["deepseek-v4-flash"]);
  form.models.registrations.unshift({ id: "00000000-0000-4000-a000-000000000002", provider: "qwen", model: "qwen-vl-plus", apiKey: "sk-...", baseUrl: "", effort: "none" });
  await saveSettings(form, async () => ({ ok: true, message: "ok" }));
  assert.deepEqual(loadSettingsData().formData.models.registrations.map(r => r.model), ["deepseek-v4-flash"]);
});

test("a configured Qwen and user-created registrations are retained", () => {
  const dir = mkdtempSync(join(tmpdir(), "hasaki-configured-model-"));
  const path = join(dir, "config.toml");
  for (const entry of [qwen.replace("sk-...", "real-test-key"), qwen.replace("000000000002", "000000000099")]) {
    const original = entry + deepseek;
    writeFileSync(path, original);
    removeUnconfiguredModelExample(path);
    assert.equal(readFileSync(path, "utf8"), original);
  }
});

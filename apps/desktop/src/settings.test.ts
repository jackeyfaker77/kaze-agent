import assert from "node:assert/strict";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { describe, it } from "node:test";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { configureSettingsConfigPath, loadSettingsData, saveSettings } from "./settings.js";
import { desktopSettingsDefaults } from "./settingsContract.js";

describe("desktop settings config path", () => {
  it("requires the runtime path contract instead of falling back to the repository root", () => {
    assert.throws(() => loadSettingsData(), /桌面配置路径尚未初始化/);
  });

  it("loads settings from the configured workspace path", () => {
    const directory = mkdtempSync(join(tmpdir(), "shiori-settings-"));
    const configPath = join(directory, "workspace", "config.toml");
    try {
      mkdirSync(join(directory, "workspace"), { recursive: true });
      writeFileSync(configPath, "[llm]\n", { encoding: "utf-8" });
      configureSettingsConfigPath(configPath);

      const snapshot = loadSettingsData();

      assert.equal(snapshot.configPath, configPath);
      assert.deepEqual(snapshot.formData.voice, {
        enabled: false,
        hotkey: "Ctrl+Space",
        microphoneDeviceId: "",
        asrEnabled: false,
        asrProvider: desktopSettingsDefaults.asrProvider,
        asrBaseUrl: desktopSettingsDefaults.asrBaseUrl,
        asrSecretId: "",
        asrSecretKey: "",
        ttsEnabled: false,
        ttsProvider: desktopSettingsDefaults.ttsProvider,
        ttsBaseUrl: desktopSettingsDefaults.ttsBaseUrl,
        ttsModel: desktopSettingsDefaults.ttsModel,
        ttsApiKey: "",
        ttsVoiceId: "",
        ttsVolume: desktopSettingsDefaults.ttsVolume,
      });
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("saves without restarting the bridge", async () => {
    const directory = mkdtempSync(join(tmpdir(), "shiori-settings-save-"));
    const configPath = join(directory, "workspace", "config.toml");
    try {
      mkdirSync(join(directory, "workspace"), { recursive: true });
      writeFileSync(configPath, "[llm]\n", { encoding: "utf-8" });
      configureSettingsConfigPath(configPath);
      const formData = loadSettingsData().formData;
      formData.models.registrations = [{
        id: "00000000-0000-4000-a000-000000000001",
        provider: "openai",
        baseUrl: "",
        apiKey: "",
        model: "test-model",
        effort: "none",
      }];
      let healthChecks = 0;

      const result = await saveSettings(formData, async () => {
        healthChecks += 1;
        return { ok: true, message: "ok" };
      });

      assert.equal(healthChecks, 1);
      assert.equal(result.ok, true);
      assert.match(readFileSync(configPath, "utf-8"), /model = "test-model"/);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
});

describe("proactive policy settings", () => {
  it("roundtrips the complete Wake, agent, Drift and prefixed policy configuration", async () => {
    const directory = mkdtempSync(join(tmpdir(), "kaze-wake-settings-"));
    const configPath = join(directory, "config.toml");
    try {
      writeFileSync(configPath, `[llm]
[proactive]
enabled = true
lifecycle = "wake"
session_key = "desktop:daily"
model = "proactive-model"
recent_chat_messages = 40
[proactive.agent]
model = "agent-model"
max_steps = 42
web_fetch_max_chars = 12000
context_prob = 0.2
delivery_cooldown_hours = 0.5
[proactive.drift]
enabled = true
max_steps = 25
min_interval_hours = 0.5
[proactive.feed]
poll_interval_seconds = 200
[proactive.overrides.anyaction]
anyaction_enabled = false
anyaction_daily_max_actions = 2
anyaction_timezone = "UTC"
`, "utf-8");
      configureSettingsConfigPath(configPath);
      const form = loadSettingsData().formData;
      const initial = form.proactive;
      assert.equal(initial?.lifecycle, "wake");
      assert.equal(initial?.agentModel, "agent-model");
      assert.equal(initial?.driftEnabled, true);
      assert.equal(initial?.feedPollIntervalSeconds, 200);
      assert.equal(initial?.anyactionEnabled, false);
      assert.equal(initial?.dailyMaxActions, 2);
      assert.equal(initial?.timezone, "UTC");
      form.models.registrations = [{ id: "agent", provider: "openai", baseUrl: "", apiKey: "", model: "test-model", effort: "none" }];
      await saveSettings(form, async () => ({ ok: true, message: "ok" }));
      assert.deepEqual(loadSettingsData().formData.proactive, initial);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("loads quiet profile defaults before applying explicit overrides", () => {
    const directory = mkdtempSync(join(tmpdir(), "kaze-quiet-settings-"));
    try {
      const configPath = join(directory, "config.toml");
      writeFileSync(configPath, '[proactive]\nprofile = "quiet"\n[proactive.overrides.safety]\ndelivery_dedupe_hours = 36\n', "utf-8");
      configureSettingsConfigPath(configPath);
      const policy = loadSettingsData().formData.proactive;
      assert.equal(policy?.dailyMaxActions, 12);
      assert.equal(policy?.minIntervalSeconds, 600);
      assert.equal(policy?.messageDedupeRecentN, 8);
      assert.equal(policy?.contextOnlyMinIntervalHours, 24);
      assert.equal(policy?.deliveryDedupeHours, 36);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
  it("preserves admission limits, custom profiles and trigger overrides across saves", async () => {
    const directory = mkdtempSync(join(tmpdir(), "kaze-proactive-settings-"));
    const configPath = join(directory, "config.toml");
    try {
      writeFileSync(configPath, `
[llm]
[proactive]
enabled = true
session_key = "telegram:42"
default_channel = "telegram"
default_chat_id = "42"
interval_seconds = 600
profile = "custom"
adaptive_enabled = true
energy_contact_enabled = false
energy_contact_threshold = 0.15
delivery_dedupe_hours = 12
message_dedupe_enabled = false
message_dedupe_recent_n = 8
tick_interval_s0 = 700

[proactive.anyaction]
enabled = true
daily_max_actions = 3
min_interval_seconds = 3600
probability_min = 0.2
probability_max = 0.7
idle_scale_minutes = 120
reset_hour_local = 9
timezone = "UTC"

[proactive.profiles.custom.trigger]
tick_interval_s0 = 900
tick_interval_s1 = 300

[proactive.overrides.trigger]
# Keep this manual tuning when saving another setting.
tick_interval_s1 = 180
tick_jitter = 0.1
`, { encoding: "utf-8" });
      configureSettingsConfigPath(configPath);
      const formData = loadSettingsData().formData;
      const initial = formData.proactive;
      assert.equal(initial?.dailyMaxActions, 3);
      assert.equal(initial?.profile, "custom");
      assert.equal(initial?.energyContactEnabled, false);
      assert.equal(initial?.deliveryDedupeHours, 12);
      assert.equal(initial?.messageDedupeEnabled, false);
      assert.equal(initial?.messageDedupeRecentN, 8);
      assert.equal(initial?.tickIntervalS0, 700);
      formData.models.registrations = [{ id: "agent", provider: "openai", baseUrl: "", apiKey: "", model: "test-model", effort: "none" }];
      await saveSettings(formData, async () => ({ ok: true, message: "ok" }));
      assert.deepEqual(loadSettingsData().formData.proactive, initial);
      const content = readFileSync(configPath, "utf-8");
      assert.match(content, /\[proactive\.profiles\.custom\.trigger\]/);
      assert.match(content, /# Keep this manual tuning/);
      assert.match(content, /tick_interval_s1 = 180/);
      assert.match(content, /probability_max = 0.7/);
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("rejects invalid admission settings before rewriting the config", async () => {
    const directory = mkdtempSync(join(tmpdir(), "kaze-proactive-invalid-"));
    const configPath = join(directory, "config.toml");
    try {
      const original = "[llm]\n";
      writeFileSync(configPath, original, { encoding: "utf-8" });
      configureSettingsConfigPath(configPath);
      const formData = loadSettingsData().formData;
      formData.models.registrations = [{ id: "agent", provider: "openai", baseUrl: "", apiKey: "", model: "test-model", effort: "none" }];
      assert.ok(formData.proactive);
      for (const change of [
        { dailyMaxActions: -1 }, { minIntervalSeconds: -1 },
        { probabilityMin: 0.8, probabilityMax: 0.1 }, { timezone: "bad/timezone" },
        { deliveryDedupeHours: 0 }, { messageDedupeRecentN: 0 },
      ]) {
        await assert.rejects(saveSettings({ ...formData, proactive: { ...formData.proactive, ...change } }, async () => ({ ok: true, message: "ok" })));
        assert.equal(readFileSync(configPath, "utf-8"), original);
      }
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });
});

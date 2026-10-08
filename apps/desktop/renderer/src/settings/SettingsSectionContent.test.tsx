/// <reference types="node" />

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { renderToStaticMarkup } from "react-dom/server";
import type { SettingsFormData } from "../../../src/bridge/shared.js";
import { SettingsSectionContent } from "./SettingsSectionContent.js";

function createSettingsFormData(): SettingsFormData {
  return {
    models: {
      registrations: [{ id: "00000000-0000-4000-a000-000000000001", provider: "openai", model: "gpt-agent", apiKey: "agent-key", baseUrl: "https://agent.example", effort: "high" }],
    },
    channels: {
      telegramToken: "telegram-token",
      qqBotUin: "10001",
      qqBotAppId: "qq-app",
      qqBotClientSecret: "qq-secret",
    },
    memory: {
      enabled: true,
      engine: "default_memory",
      embeddingModel: "embed-model",
      embeddingApiKey: "embed-key",
      embeddingBaseUrl: "https://embed.example",
      outputDimensionality: "1536",
    },
    voice: {
      enabled: true,
      hotkey: "Ctrl+Space",
      microphoneDeviceId: "",
      asrProvider: "tencent",
      asrBaseUrl: "https://asr.tencentcloudapi.com/",
      asrSecretId: "secret-id",
      asrSecretKey: "secret-key",
      ttsProvider: "minimax",
      ttsBaseUrl: "https://api.minimaxi.com/v1/t2a_v2",
      ttsModel: "speech-2.8-turbo",
      ttsApiKey: "tts-key",
      ttsVolume: 2,
    },
    advanced: {
      maxTokens: 4000,
      maxIterations: 10,
      devMode: false,
      streamingEnabled: false,
      memoryWindow: 20,
      searchEnabled: true,
      spawnEnabled: true,
      memoryOptimizerEnabled: false,
      memoryOptimizerIntervalSeconds: 3600,
      pluginsRawToml: "",
    },
  };
}

describe("SettingsSectionContent", () => {
  const draft = createSettingsFormData();
  const updateDraft = () => undefined;

  it("routes every settings domain to its editor", () => {
    const cases = [
      { sectionId: "models", subsectionId: "catalog", expected: "gpt-agent" },
      { sectionId: "channels", subsectionId: "qqbot", expected: "qq-app" },
      { sectionId: "memory", subsectionId: "embedding", expected: "embed-model" },
      { sectionId: "voice", subsectionId: "provider", expected: "secret-id" },
      { sectionId: "advanced", subsectionId: "general", expected: "max_tokens" },
    ] as const;

    cases.forEach(({ sectionId, subsectionId, expected }) => {
      const markup = renderToStaticMarkup(
        <SettingsSectionContent
          sectionId={sectionId}
          subsectionId={subsectionId}
          draft={draft}
          updateDraft={updateDraft}
        />,
      );
      assert.match(markup, new RegExp(expected));
    });
  });

  it("shows Wake and Drift controls without inactive Default limits", () => {
    const markup = renderToStaticMarkup(
      <SettingsSectionContent sectionId="advanced" subsectionId="general" draft={draft} updateDraft={updateDraft} />,
    );
    assert.match(markup, /<select[^>]*aria-label="主动策略"/);
    assert.match(markup, /Wake：按资讯价值唤醒/);
    assert.match(markup, /aria-label="空闲时主动联系"/);
    assert.doesNotMatch(markup, /aria-label="每日推送上限"/);
    assert.doesNotMatch(markup, /aria-label="检查间隔（秒）"/);
  });

  it("shows Default cadence and hides quotas when admission limits are disabled", () => {
    const markup = renderToStaticMarkup(
      <SettingsSectionContent sectionId="advanced" subsectionId="general" draft={{ ...draft, proactive: {
        enabled: true, sessionKey: "daily", channel: "desktop", chatId: "", intervalSeconds: 600,
        lifecycle: "default", anyactionEnabled: false,
      } }} updateDraft={updateDraft} />,
    );
    assert.match(markup, /aria-label="检查频率"/);
    assert.match(markup, /aria-label="检查近期消息是否重复"/);
    assert.doesNotMatch(markup, /aria-label="检查间隔（秒）"/);
    assert.doesNotMatch(markup, /aria-label="每日推送上限"/);
  });

  it("shows model registration previews without exposing detail fields", () => {
    const markup = renderToStaticMarkup(
      <SettingsSectionContent
        sectionId="models"
        subsectionId="catalog"
        draft={draft}
        updateDraft={updateDraft}
      />,
    );

    assert.doesNotMatch(markup, />名称</);
    assert.match(markup, />gpt-agent</);
    assert.match(markup, />https:\/\/agent\.example</);
    assert.doesNotMatch(markup, /value="gpt-agent"/);
    assert.doesNotMatch(markup, /agent-key/);
  });

  it("renders the global TTS volume control", () => {
    const markup = renderToStaticMarkup(
      <SettingsSectionContent
        sectionId="voice"
        subsectionId="provider"
        draft={draft}
        updateDraft={updateDraft}
      />,
    );

    assert.match(markup, /aria-label="TTS 音量"/);
    assert.match(markup, /type="range"/);
    assert.match(markup, /value="2"/);
  });
});

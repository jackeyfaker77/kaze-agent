import type { ProactiveSettingsFormData } from "./bridge/shared.js";

/** Defaults shared by settings persistence and the proactive editor. */
export const proactiveSettingsDefaults = {
  enabled: false, sessionKey: "", channel: "desktop", chatId: "", intervalSeconds: 1800,
  lifecycle: "wake" as "default" | "wake", proactiveModel: "", agentModel: "",
  agentMaxSteps: 35, contentLimit: 5, webFetchMaxChars: 8000, contextProbability: 0.03,
  deliveryCooldownHours: 1, driftEnabled: false, driftMaxSteps: 20, driftMinIntervalHours: 3,
  judgeSendThreshold: 0.6, recentChatMessages: 20, contextOnlyDailyMax: 1,
  contextOnlyMinIntervalHours: 12, feedPollIntervalSeconds: 150,
  profile: "daily", adaptiveEnabled: true, energyContactEnabled: true,
  energyContactThreshold: 0.2, scoreWeightEnergy: 0.35,
  deliveryDedupeHours: 10, messageDedupeEnabled: true, messageDedupeRecentN: 5,
  anyactionEnabled: true, dailyMaxActions: 48, minIntervalSeconds: 180,
  probabilityMin: 0.2, probabilityMax: 0.82, idleScaleMinutes: 30,
  resetHourLocal: 12, timezone: "Asia/Shanghai", policyRawToml: "",
} satisfies ProactiveSettingsFormData;

export function proactivePresetDefaults(profile: string) {
  const profiles: Record<string, Partial<ProactiveSettingsFormData>> = {
    quiet: { dailyMaxActions: 12, minIntervalSeconds: 600, probabilityMin: 0.05, probabilityMax: 0.3, idleScaleMinutes: 120, deliveryDedupeHours: 24, messageDedupeRecentN: 8, judgeSendThreshold: 0.75, contextOnlyMinIntervalHours: 24 },
    dev_verify: { dailyMaxActions: 999, minIntervalSeconds: 20, probabilityMin: 0.75, probabilityMax: 0.98, idleScaleMinutes: 15, deliveryDedupeHours: 1, judgeSendThreshold: 0.28, contextOnlyDailyMax: 20, contextOnlyMinIntervalHours: 1 },
  };
  return { ...proactiveSettingsDefaults, ...profiles[profile], profile };
}

/** Apply the same policy precedence as the backend, accepting both saved key styles. */
export function proactivePolicyValues(proactive: Record<string, unknown>): Record<string, unknown> {
  const record = (value: unknown): Record<string, unknown> =>
    value !== null && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
  const flatten = (tables: Record<string, unknown>) => Object.fromEntries(
    Object.entries(tables).flatMap(([category, table]) => Object.entries(record(table)).map(([key, value]) => [
      category === "anyaction" && !key.startsWith("anyaction_") ? `anyaction_${key}` : key, value,
    ])),
  );
  const profile = String(proactive.profile ?? proactive.preset ?? "daily");
  return {
    ...flatten(record(record(proactive.profiles)[profile])),
    ...flatten(record(proactive.overrides)),
    ...flatten({ anyaction: proactive.anyaction }),
    ...proactive,
  };
}

/** Retain profile and override tables verbatim when another setting is saved. */
export function extractProactivePolicyToml(content: string): string {
  return [...content.matchAll(/^\[proactive\.(?:profiles|overrides)(?:\.[^\]]+)?\][^\r\n]*\r?\n[\s\S]*?(?=^\[|$(?![\s\S]))/gm)]
    .map(match => match[0]).join("").trim();
}

/** Serialize proactive settings without discarding advanced trigger overrides. */
export function renderProactiveSettings(value?: ProactiveSettingsFormData): string[] {
  const settings = { ...proactiveSettingsDefaults, ...value };
  return [
    "[proactive]",
    `enabled = ${settings.enabled}`,
    `lifecycle = ${JSON.stringify(settings.lifecycle)}`,
    `model = ${JSON.stringify(settings.proactiveModel)}`,
    `session_key = ${JSON.stringify(settings.sessionKey)}`,
    `default_channel = ${JSON.stringify(settings.channel)}`,
    `default_chat_id = ${JSON.stringify(settings.chatId)}`,
    `interval_seconds = ${settings.intervalSeconds}`,
    `profile = ${JSON.stringify(settings.profile)}`,
    `adaptive_enabled = ${settings.adaptiveEnabled}`,
    `energy_contact_enabled = ${settings.energyContactEnabled}`,
    `energy_contact_threshold = ${settings.energyContactThreshold}`,
    `score_weight_energy = ${settings.scoreWeightEnergy}`,
    `delivery_dedupe_hours = ${settings.deliveryDedupeHours}`,
    `message_dedupe_enabled = ${settings.messageDedupeEnabled}`,
    `message_dedupe_recent_n = ${settings.messageDedupeRecentN}`,
    `judge_send_threshold = ${settings.judgeSendThreshold}`,
    `recent_chat_messages = ${settings.recentChatMessages}`,
    `context_only_daily_max = ${settings.contextOnlyDailyMax}`,
    `context_only_min_interval_hours = ${settings.contextOnlyMinIntervalHours}`,
    ...([
      ["tick_interval_s0", settings.tickIntervalS0],
      ["tick_interval_s1", settings.tickIntervalS1],
      ["tick_jitter", settings.tickJitter],
    ] as const).filter(([, val]) => val !== undefined).map(([key, val]) => `${key} = ${val}`),
    "",
    "[proactive.anyaction]",
    `enabled = ${settings.anyactionEnabled}`,
    `daily_max_actions = ${settings.dailyMaxActions}`,
    `min_interval_seconds = ${settings.minIntervalSeconds}`,
    `probability_min = ${settings.probabilityMin}`,
    `probability_max = ${settings.probabilityMax}`,
    `idle_scale_minutes = ${settings.idleScaleMinutes}`,
    `reset_hour_local = ${settings.resetHourLocal}`,
    `timezone = ${JSON.stringify(settings.timezone)}`,
    "",
    "[proactive.agent]",
    `model = ${JSON.stringify(settings.agentModel)}`,
    `max_steps = ${settings.agentMaxSteps}`,
    `content_limit = ${settings.contentLimit}`,
    `web_fetch_max_chars = ${settings.webFetchMaxChars}`,
    `context_prob = ${settings.contextProbability}`,
    `delivery_cooldown_hours = ${settings.deliveryCooldownHours}`,
    "",
    "[proactive.drift]",
    `enabled = ${settings.driftEnabled}`,
    `max_steps = ${settings.driftMaxSteps}`,
    `min_interval_hours = ${settings.driftMinIntervalHours}`,
    "",
    "[proactive.feed]",
    `poll_interval_seconds = ${settings.feedPollIntervalSeconds}`,
    "",
    settings.policyRawToml,
  ];
}

/** Reject values that would fail the backend's proactive policy validation. */
export function validateProactiveSettings(value?: ProactiveSettingsFormData): void {
  const settings = { ...proactiveSettingsDefaults, ...value };
  if (!["default", "wake"].includes(settings.lifecycle)) throw new Error("请选择主动策略");
  for (const [name, val, minimum] of [
    ["主动判断步数", settings.agentMaxSteps, 1], ["候选数量", settings.contentLimit, 1],
    ["正文读取上限", settings.webFetchMaxChars, 1], ["自主探索步数", settings.driftMaxSteps, 3],
    ["近期对话数量", settings.recentChatMessages, 1], ["场景推送额度", settings.contextOnlyDailyMax, 0],
    ["源刷新间隔", settings.feedPollIntervalSeconds, 1],
  ] as const) {
    if (!Number.isInteger(val) || val < minimum) throw new Error(`${name}请填写不小于 ${minimum} 的整数`);
  }
  for (const val of [settings.deliveryCooldownHours, settings.driftMinIntervalHours, settings.contextOnlyMinIntervalHours]) {
    if (!Number.isFinite(val) || val < 0) throw new Error("冷却时间请填写非负数");
  }
  if (!Number.isFinite(settings.contextProbability) || settings.contextProbability < 0 || settings.contextProbability > 1) throw new Error("场景判断概率请填写 0 到 1 之间的数值");
  if (!Number.isInteger(settings.intervalSeconds) || settings.intervalSeconds < 60) {
    throw new Error("主动检查间隔请填写不小于 60 的整数");
  }
  if (!settings.profile.trim()) throw new Error("请选择主动检查频率");
  if (!Number.isFinite(settings.deliveryDedupeHours) || settings.deliveryDedupeHours <= 0) throw new Error("投递去重时长请填写正数");
  if (!Number.isInteger(settings.messageDedupeRecentN) || settings.messageDedupeRecentN < 1 || settings.messageDedupeRecentN > 50) throw new Error("近期消息数量请填写 1 到 50 的整数");
  for (const [name, val] of [
    ["每日推送上限", settings.dailyMaxActions], ["推送最小间隔", settings.minIntervalSeconds],
  ] as const) {
    if (!Number.isInteger(val) || val < 0) throw new Error(`${name}请填写非负整数`);
  }
  for (const val of [settings.probabilityMin, settings.probabilityMax, settings.energyContactThreshold, settings.scoreWeightEnergy, settings.judgeSendThreshold]) {
    if (!Number.isFinite(val) || val < 0 || val > 1) throw new Error("主动检查概率与电量阈值请填写 0 到 1 之间的数值");
  }
  if (settings.probabilityMin > settings.probabilityMax) throw new Error("最小推送概率不能大于最大推送概率");
  if (!Number.isFinite(settings.idleScaleMinutes) || settings.idleScaleMinutes <= 0) throw new Error("空闲时间尺度请填写正数");
  if (!Number.isInteger(settings.resetHourLocal) || settings.resetHourLocal < 0 || settings.resetHourLocal > 23) throw new Error("配额重置时间请填写 0 到 23 的整数");
  try { new Intl.DateTimeFormat("en", { timeZone: settings.timezone }); }
  catch { throw new Error("配额时区请填写有效时区，例如 Asia/Shanghai"); }
}

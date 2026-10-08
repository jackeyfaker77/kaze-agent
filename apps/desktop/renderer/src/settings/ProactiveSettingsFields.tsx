import { useId } from "react";
import { SettingsField as Field } from "./SettingsField";
import { SettingsToggleField, settingsInputClass } from "./SettingsFieldPrimitives";
import type { SettingsSectionEditorProps } from "./settingsPageTypes";
import { proactivePresetDefaults, proactiveSettingsDefaults } from "../../../src/proactiveSettings";
import type { ProactiveSettingsFormData } from "../../../src/bridge/shared";
import { parseSettingsNumber } from "./settingsSectionUtils";

/** 选择完整主动策略、目标会话与自主探索配置。 */
export function ProactiveSettingsFields({ draft, updateDraft }: Pick<SettingsSectionEditorProps, "draft" | "updateDraft">) {
  const prefix = useId();
  const proactive = { ...proactiveSettingsDefaults, ...draft.proactive };
  const inputClass = settingsInputClass.replace(" focus:outline-none", "");
  function updateProactive(change: Partial<ProactiveSettingsFormData>) {
    updateDraft(current => ({ ...current, proactive: { ...proactiveSettingsDefaults, ...current.proactive, ...change } }));
  }
  return <>
    <SettingsToggleField label="主动检查" checked={proactive.enabled} onChange={enabled => updateProactive({ enabled })} />
    <Field label="主动策略" htmlFor={`${prefix}-lifecycle`}>
      <select id={`${prefix}-lifecycle`} aria-label="主动策略" className={inputClass} value={proactive.lifecycle} onChange={event => updateProactive({ lifecycle: event.target.value as "default" | "wake" })}>
        <option value="wake">Wake：按资讯价值唤醒</option>
        <option value="default">Default：按电量与推送限制检查</option>
      </select>
    </Field>
    <Field label="主动检查会话" htmlFor={`${prefix}-session`}><input id={`${prefix}-session`} aria-label="主动检查会话" className={inputClass} value={proactive.sessionKey} onChange={event => updateProactive({ sessionKey: event.target.value })} /></Field>
    <Field label="推送渠道" htmlFor={`${prefix}-channel`}><input id={`${prefix}-channel`} aria-label="推送渠道" className={inputClass} value={proactive.channel} onChange={event => updateProactive({ channel: event.target.value })} /></Field>
    <Field label="渠道目标" htmlFor={`${prefix}-target`}><input id={`${prefix}-target`} aria-label="渠道目标" className={inputClass} value={proactive.chatId} placeholder="桌面推送可使用上方会话" onChange={event => updateProactive({ chatId: event.target.value })} /></Field>
    {proactive.lifecycle === "wake" ? <p className="text-sm text-[#667085]">结合兴趣、资讯新鲜度和最近已发送消息判断是否联系。</p> : <>
      <Field label="检查频率" htmlFor={`${prefix}-profile`}>
        <select id={`${prefix}-profile`} aria-label="检查频率" className={inputClass} value={proactive.profile} onChange={event => {
          const profile = event.target.value;
          const values = proactivePresetDefaults(profile);
          updateProactive({ profile, dailyMaxActions: values.dailyMaxActions, minIntervalSeconds: values.minIntervalSeconds, probabilityMin: values.probabilityMin, probabilityMax: values.probabilityMax, idleScaleMinutes: values.idleScaleMinutes, deliveryDedupeHours: values.deliveryDedupeHours, messageDedupeRecentN: values.messageDedupeRecentN, judgeSendThreshold: values.judgeSendThreshold, contextOnlyDailyMax: values.contextOnlyDailyMax, contextOnlyMinIntervalHours: values.contextOnlyMinIntervalHours, tickIntervalS0: undefined, tickIntervalS1: undefined, tickJitter: undefined });
        }}>
          <option value="daily">日常（4–8 分钟）</option>
          <option value="quiet">安静（15–30 分钟）</option>
          <option value="dev_verify">调试（30–60 秒）</option>
          {!["daily", "quiet", "dev_verify"].includes(proactive.profile) ? <option value={proactive.profile}>{proactive.profile}</option> : null}
        </select>
      </Field>
      <SettingsToggleField label="限制主动推送频率" checked={proactive.anyactionEnabled} onChange={anyactionEnabled => updateProactive({ anyactionEnabled })} />
      {proactive.anyactionEnabled ? <>
        <Field label="每日推送上限" htmlFor={`${prefix}-quota`}><input id={`${prefix}-quota`} aria-label="每日推送上限" className={inputClass} type="number" min={0} step={1} value={proactive.dailyMaxActions} onChange={event => updateProactive({ dailyMaxActions: parseSettingsNumber(event.target.value, proactive.dailyMaxActions) })} /></Field>
        <Field label="推送最小间隔（秒）" htmlFor={`${prefix}-cooldown`}><input id={`${prefix}-cooldown`} aria-label="推送最小间隔（秒）" className={inputClass} type="number" min={0} step={1} value={proactive.minIntervalSeconds} onChange={event => updateProactive({ minIntervalSeconds: parseSettingsNumber(event.target.value, proactive.minIntervalSeconds) })} /></Field>
      </> : null}
      <SettingsToggleField label="检查近期消息是否重复" checked={proactive.messageDedupeEnabled} onChange={messageDedupeEnabled => updateProactive({ messageDedupeEnabled })} />
    </>}
    <SettingsToggleField label="空闲时主动联系" checked={proactive.driftEnabled} onChange={driftEnabled => updateProactive({ driftEnabled })} />
    {proactive.driftEnabled ? <p className="text-sm text-[#667085]">通过可用的 Drift 活动探索和跟进，仍由模型判断是否值得发送。</p> : null}
    <Field label="主动判断模型" htmlFor={`${prefix}-model`}><input id={`${prefix}-model`} aria-label="主动判断模型" className={inputClass} value={proactive.agentModel} placeholder="留空使用默认模型" onChange={event => updateProactive({ agentModel: event.target.value })} /></Field>
    {proactive.lifecycle === "default" ? <Field label="主动判断最多步数" htmlFor={`${prefix}-steps`}><input id={`${prefix}-steps`} aria-label="主动判断最多步数" className={inputClass} type="number" min={1} step={1} value={proactive.agentMaxSteps} onChange={event => updateProactive({ agentMaxSteps: parseSettingsNumber(event.target.value, proactive.agentMaxSteps) })} /></Field> : null}
  </>;
}

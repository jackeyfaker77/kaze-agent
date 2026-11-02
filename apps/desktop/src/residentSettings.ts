import type { SettingsFormData, SaveSettingsResult } from "./bridge/shared.js";
import type { BridgeClient } from "./bridge/bridgeContract.js";
import { verifyLocalHost } from "./bridge/localHostCapabilities.js";

/** Config persistence and model activation are one Host-owned operation. */
export class ResidentModelSettings {
  private saving = false;
  constructor(private readonly bridge: BridgeClient) {}

  async save(form: SettingsFormData): Promise<SaveSettingsResult> {
    if (this.saving) throw new Error("模型切换正在进行，请稍后再试");
    this.saving = true;
    try {
      await verifyLocalHost(this.bridge);
      if (!form.modelRevision) throw new Error("请重新加载模型配置后再保存");
      const result = await this.bridge.invoke({ method: "models.save", payload: {
        expected_revision: form.modelRevision, registrations: form.models.registrations,
      } });
      // Unknown transport outcomes are never retried or blindly rolled back.
      if (result.error) throw new Error(result.error.message);
      return result.payload as unknown as SaveSettingsResult;
    } finally { this.saving = false; }
  }
}

export function createResidentModelSettings(_workspace: string, bridge: BridgeClient): ResidentModelSettings {
  return new ResidentModelSettings(bridge);
}

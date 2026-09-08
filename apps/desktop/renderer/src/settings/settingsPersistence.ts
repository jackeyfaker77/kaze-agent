import type {
  DesktopApi,
  SaveSettingsResult,
  SettingsFormData,
  SettingsSnapshot,
} from "../../../src/bridge/shared.js";

type SettingsLoadApi = Pick<DesktopApi, "readSettings">;
type SettingsSaveApi = Pick<DesktopApi, "readSettings" | "saveSettings" | "invoke">;

export type SettingsPageLoadResult = {
  snapshot: SettingsSnapshot;
};

export type SettingsPageSaveResult = {
  saveResult: SaveSettingsResult;
  snapshot: SettingsSnapshot;
  nextDraft: SettingsFormData;
};

/** Deep-clones mutable settings form data before local edits. */
export function cloneSettings(data: SettingsFormData): SettingsFormData {
  return JSON.parse(JSON.stringify(data)) as SettingsFormData;
}

/** Compares two settings payloads as persisted user data. */
export function settingsEqual(
  left: SettingsFormData | null,
  right: SettingsFormData | null,
): boolean {
  if (!left || !right) return false;
  return JSON.stringify(left) === JSON.stringify(right);
}

/** Returns whether a failed settings load should retry once the desktop bridge recovers. */
export function shouldRetryFailedSettingsLoad(options: {
  bridgeReady: boolean;
  loadError: string;
}): boolean {
  return options.bridgeReady && Boolean(options.loadError.trim());
}

/** Loads the settings page data from the persisted runtime configuration. */
export async function loadSettingsPageData(api: SettingsLoadApi): Promise<SettingsPageLoadResult> {
  const nextSnapshot = await api.readSettings();
  return {
    snapshot: nextSnapshot,
  };
}

/** Saves settings and reloads the persisted snapshot without restarting the bridge. */
export async function saveSettingsPageData(
  api: SettingsSaveApi,
  draft: SettingsFormData,
): Promise<SettingsPageSaveResult> {
  const persistedDraft = cloneSettings(draft);
  const saveResult = await api.saveSettings(persistedDraft);
  const snapshot = await api.readSettings();
  const nextDraft = cloneSettings(snapshot.formData);


  return {
    saveResult,
    snapshot,
    nextDraft,
  };
}

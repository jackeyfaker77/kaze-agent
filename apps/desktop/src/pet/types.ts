/** Runtime state supported by the Codex-compatible sprite atlas. */
export type DesktopPetState =
  | "idle"
  | "running-right"
  | "running-left"
  | "waving"
  | "jumping"
  | "failed"
  | "waiting"
  | "running"
  | "review";

/** States a application-wide package may expose as a temporary action overlay. */
export type DesktopPetActionState =
  | "idle"
  | "running-right"
  | "running-left"
  | "waving"
  | "jumping";

export type DesktopPetActionPayload = {
  action_id: string;
  session_key: string;
  channel: "desktop";
  kind: "move" | "play";
  name?: string;
  target?: "top_left" | "top_right" | "center" | "bottom_left" | "bottom_right";
  animation?: "" | "idle" | "run";
  state?: DesktopPetState;
};

/** One validated application-wide pet package available to the desktop shell. */
export type DesktopPetPackage = {
  id: string;
  displayName: string;
  /** Opaque trusted local-asset URL, never a renderer-visible filesystem path. */
  spritesheetUrl: string;
};

/** Resolved binding used to load one desktop pet window. */
export type DesktopPetBinding = {
  sessionKey: string;
  package: DesktopPetPackage;
  actions?: Record<string, DesktopPetActionState>;
};

export type DesktopPetPosition = { x: number; y: number };

/** Available display area for positioning the transparent desktop-pet window. */
export type DesktopPetWorkArea = {
  x: number;
  y: number;
  width: number;
  height: number;
};

/** Persisted application-level desktop-pet configuration. */
export type DesktopPetSettings = {
  visible: boolean;
  sessionKey: string | null;
  packageId: string | null;
  positions: Record<string, DesktopPetPosition>;
};

export const defaultDesktopPetSettings: DesktopPetSettings = {
  visible: false,
  sessionKey: null,
  packageId: null,
  positions: {},
};

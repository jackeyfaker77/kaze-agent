type PresentableWindow = {
  once(event: string, handler: () => void): void;
  isMinimized(): boolean;
  restore(): void;
  isVisible(): boolean;
  show(): void;
  focus(): void;
};

const readiness = new WeakMap<PresentableWindow, { ready: boolean; requested: boolean }>();

/** A foreground request must not expose the native empty surface before its first paint. */
export function prepareDesktopWindowPresentation(window: PresentableWindow): void {
  const state = { ready: false, requested: false };
  readiness.set(window, state);
  window.once("ready-to-show", () => {
    state.ready = true;
    if (state.requested) showDesktopWindow(window);
  });
}

export function showDesktopWindow(window: PresentableWindow): void {
  const state = readiness.get(window);
  if (state && !state.ready) {
    state.requested = true;
    return;
  }
  if (window.isMinimized()) window.restore();
  if (!window.isVisible()) window.show();
  window.focus();
}

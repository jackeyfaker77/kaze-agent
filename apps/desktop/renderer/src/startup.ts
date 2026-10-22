/** Keep the HTML status visible until the application has committed its first render. */
export async function startDesktopRenderer(load: () => Promise<{ mount: (ready: () => void) => void }>): Promise<void> {
  const status = document.getElementById("startup-status");
  const message = document.getElementById("startup-message");
  const timer = window.setTimeout(() => {
    if (message) message.textContent = "启动时间较长，可以继续等待或重新加载。";
  }, 10_000);
  try {
    const application = await load();
    application.mount(() => {
      window.clearTimeout(timer);
      status?.remove();
    });
  } catch (error) {
    window.clearTimeout(timer);
    if (message) message.textContent = "工作区加载失败，请重新加载。";
    try {
      window.miraDesktop?.reportRendererDiagnostic({
        kind: "error", message: error instanceof Error ? error.message : String(error),
        stack: error instanceof Error ? error.stack : undefined,
        details: { phase: "startup" },
      });
    } catch { /* The visible recovery action must survive a missing preload bridge. */ }
  }
}

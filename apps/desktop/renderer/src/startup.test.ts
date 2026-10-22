import assert from "node:assert/strict";
import test from "node:test";
import { startDesktopRenderer } from "./startup";

test("loading and first render keep feedback visible; only a committed mount removes it", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  let removed = false;
  let cleared = 0;
  let slow: () => void = () => {};
  let ready: () => void = () => {};
  const message = { textContent: "loading" };
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    setTimeout(callback: () => void) { slow = callback; return 1; },
    clearTimeout() { cleared++; },
  } });
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    getElementById(id: string) { return id === "startup-message" ? message : { remove() { removed = true; } }; },
  } });
  try {
    await startDesktopRenderer(async () => ({ mount(callback) { ready = callback; } }));
    assert.equal(removed, false);
    slow();
    assert.match(message.textContent, /启动时间较长/);
    ready();
    assert.equal(removed, true);
    assert.equal(cleared, 1);
  } finally {
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow); else Reflect.deleteProperty(globalThis, "window");
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument); else Reflect.deleteProperty(globalThis, "document");
  }
});

test("failed module load stays recoverable even when diagnostics or preload are unavailable", async () => {
  const originalWindow = Object.getOwnPropertyDescriptor(globalThis, "window");
  const originalDocument = Object.getOwnPropertyDescriptor(globalThis, "document");
  const message = { textContent: "loading" };
  let removed = false;
  Object.defineProperty(globalThis, "window", { configurable: true, value: {
    setTimeout() { return 1; }, clearTimeout() {},
    miraDesktop: { reportRendererDiagnostic() { throw new Error("bridge missing"); } },
  } });
  Object.defineProperty(globalThis, "document", { configurable: true, value: {
    getElementById(id: string) { return id === "startup-message" ? message : { remove() { removed = true; } }; },
  } });
  try {
    await startDesktopRenderer(async () => { throw new Error("module unavailable"); });
    assert.equal(removed, false);
    assert.match(message.textContent, /加载失败/);
  } finally {
    if (originalWindow) Object.defineProperty(globalThis, "window", originalWindow); else Reflect.deleteProperty(globalThis, "window");
    if (originalDocument) Object.defineProperty(globalThis, "document", originalDocument); else Reflect.deleteProperty(globalThis, "document");
  }
});

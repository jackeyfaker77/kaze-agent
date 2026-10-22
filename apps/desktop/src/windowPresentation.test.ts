import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { prepareDesktopWindowPresentation, showDesktopWindow } from "./windowPresentation.js";

class Window extends EventEmitter {
  visible = false;
  minimized = false;
  shows = 0;
  focuses = 0;
  isMinimized() { return this.minimized; }
  restore() { this.minimized = false; }
  isVisible() { return this.visible; }
  show() { this.visible = true; this.shows++; }
  focus() { this.focuses++; }
}

test("startup foreground requests wait for the first paint and show only once", () => {
  const window = new Window();
  prepareDesktopWindowPresentation(window);
  showDesktopWindow(window);
  showDesktopWindow(window);
  assert.equal(window.visible, false);
  assert.equal(window.focuses, 0);
  window.emit("ready-to-show");
  assert.equal(window.shows, 1);
  assert.equal(window.focuses, 1);
});

test("a background window does not steal focus when ready; later requests still restore it", () => {
  const window = new Window();
  prepareDesktopWindowPresentation(window);
  window.emit("ready-to-show");
  assert.equal(window.visible, false);
  assert.equal(window.focuses, 0);
  window.minimized = true;
  showDesktopWindow(window);
  assert.equal(window.minimized, false);
  assert.equal(window.visible, true);
});

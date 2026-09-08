/// <reference types="node" />

import assert from "node:assert/strict";
import { afterEach, describe, it } from "node:test";
import { prepareModelRegistrationRemoval } from "./modelRegistrationRemoval.js";

const originalWindowDescriptor = Object.getOwnPropertyDescriptor(globalThis, "window");

afterEach(() => {
  if (originalWindowDescriptor) {
    Object.defineProperty(globalThis, "window", originalWindowDescriptor);
  } else {
    Reflect.deleteProperty(globalThis, "window");
  }
});

describe("prepareModelRegistrationRemoval", () => {
  it("confirms removing an application model without a backend role request", async () => {
    Object.defineProperty(globalThis, "window", {
      configurable: true,
      value: {
        alert: () => undefined,
        confirm: () => true,
        miraDesktop: {
          invoke: async (request: { method: string; payload: Record<string, unknown> }) => {
            throw new Error(`unexpected ${request.method}`);
          },
        },
      },
    });

    const removable = await prepareModelRegistrationRemoval(
      {
        id: "registration-1",
        provider: "openai",
        model: "gpt-agent",
        baseUrl: "",
        apiKey: "",
        effort: "none",
      },
      [
        { id: "registration-1", provider: "openai", model: "gpt-agent", baseUrl: "", apiKey: "", effort: "none" },
        { id: "registration-2", provider: "openai", model: "gpt-next", baseUrl: "", apiKey: "", effort: "high" },
      ],
    );

    assert.equal(removable, true);
  });
});

import assert from "node:assert/strict";
import { describe, it } from "node:test";
import { openCodexLoginPage } from "./codexBrowserLogin.js";

describe("Codex browser login", () => {
  it("opens the authorization URL with this attempt's callback and state", async () => {
    const url = "https://auth.openai.com/oauth/authorize?state=example&redirect_uri=http%3A%2F%2Flocalhost%3A1455%2Fauth%2Fcallback";
    const opened: string[] = [];
    await openCodexLoginPage(url, async value => {
      opened.push(value);
      return { ok: true, error: null };
    });
    assert.deepEqual(opened, [url]);
  });

  it("rejects unexpected authentication pages before opening the browser", async () => {
    for (const url of ["https://example.com/oauth/authorize", "http://auth.openai.com/oauth/authorize", "https://auth.openai.com/codex/device", "https://user:password@auth.openai.com/oauth/authorize"]) {
      await assert.rejects(openCodexLoginPage(url, async () => {
        assert.fail("untrusted login page was opened");
      }), /登录地址无效/);
    }
  });

  it("reports a browser-open failure so login startup can cancel its callback", async () => {
    await assert.rejects(openCodexLoginPage("https://auth.openai.com/oauth/authorize", async () => ({ ok: false, error: "browser unavailable" })), /browser unavailable/);
    await assert.rejects(openCodexLoginPage("https://auth.openai.com/oauth/authorize", async () => { throw new Error("browser failed"); }), /browser failed/);
  });
});

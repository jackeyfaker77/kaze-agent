import type { ExternalLinkOpenResult } from "../../../src/bridge/shared";

export type CodexLogin = {
  login_id: string;
  status: "waiting" | "connected" | "failed" | "cancelled";
  authorization_url: string;
  interval: number;
  error?: string;
};

/** Opens only the OpenAI OAuth page returned for the current login attempt. */
export async function openCodexLoginPage(
  authorizationUrl: string,
  openExternal: (url: string) => Promise<ExternalLinkOpenResult>,
): Promise<void> {
  const url = new URL(authorizationUrl);
  if (url.origin !== "https://auth.openai.com" || url.pathname !== "/oauth/authorize" || url.username || url.password) {
    throw new Error("OpenAI 登录地址无效，请重新登录。");
  }
  const result = await openExternal(url.toString());
  if (!result.ok) throw new Error(result.error || "无法打开浏览器，请重试。");
}

import { mkdirSync } from "node:fs";
import { resolve } from "node:path";

type IdentityApp = {
  setName(name: string): void;
  setAppUserModelId(id: string): void;
  setPath(name: "userData" | "sessionData", path: string): void;
  requestSingleInstanceLock(): boolean;
};

/** Establishes isolated storage before Electron derives the instance lock or cache. */
export function configureApplicationIdentity(app: IdentityApp, workspacePath: string, userDataOverride?: string): boolean {
  const userData = resolve(userDataOverride || resolve(workspacePath, ".desktop", "user-data"));
  const sessionData = resolve(userData, "chromium");
  mkdirSync(sessionData, { recursive: true });
  app.setName("Hasaki Agent");
  app.setAppUserModelId("com.hasaki.agent");
  app.setPath("userData", userData);
  app.setPath("sessionData", sessionData);
  return app.requestSingleInstanceLock();
}

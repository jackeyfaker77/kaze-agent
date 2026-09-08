import assert from "node:assert/strict";
import { mkdtempSync, realpathSync } from "node:fs";
import { tmpdir } from "node:os";
import { resolve } from "node:path";
import { it } from "node:test";
import { configureApplicationIdentity } from "./applicationIdentity.js";

it("sets both storage paths before taking the single-instance lock", () => {
  const workspace = mkdtempSync(resolve(tmpdir(), "hasaki-identity-"));
  const calls: string[] = [];
  const paths: Record<string, string> = {};
  const ownsLock = configureApplicationIdentity({
    setName: name => { calls.push(`name:${name}`); },
    setAppUserModelId: id => { calls.push(`id:${id}`); },
    setPath: (name, value) => { paths[name] = value; calls.push(name); },
    requestSingleInstanceLock: () => {
      assert.ok(paths.userData);
      assert.ok(paths.sessionData);
      return false;
    },
  }, workspace);
  assert.equal(ownsLock, false);
  assert.deepEqual(calls, ["name:Hasaki Agent", "id:com.hasaki.agent", "userData", "sessionData"]);
  assert.equal(paths.userData, resolve(workspace, ".desktop/user-data"));
  assert.equal(realpathSync(paths.sessionData), realpathSync(resolve(paths.userData, "chromium")));
});

it("isolates different checkouts even when the Windows account is shared", () => {
  const base = mkdtempSync(resolve(tmpdir(), "hasaki-checkouts-"));
  const profiles: string[] = [];
  for (const checkout of ["one", "two"]) {
    configureApplicationIdentity({
      setName: () => {}, setAppUserModelId: () => {},
      setPath: (name, value) => { if (name === "userData") profiles.push(value); },
      requestSingleInstanceLock: () => true,
    }, resolve(base, checkout, "workspace"));
  }
  assert.notEqual(profiles[0], profiles[1]);
});

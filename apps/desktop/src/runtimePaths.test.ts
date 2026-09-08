import assert from "node:assert/strict";
import { existsSync, mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { describe, it } from "node:test";
import { dirname, join, resolve } from "node:path";
import { tmpdir } from "node:os";
import { ensureDesktopRuntimeConfig, resolveDesktopConfigPath, resolveDesktopRuntimePaths, resolveDesktopWorkspacePath } from "./runtimePaths.js";

describe("resolveDesktopRuntimePaths", () => {
  it("uses a workspace override without changing the backend executable", () => {
    const paths = resolveDesktopRuntimePaths({ packaged: false, appPath: "E:/CODE/hasaki-agent/apps/desktop",
      homePath: "C:/Users/test", repositoryRoot: "E:/CODE/hasaki-agent", workspacePath: "E:/CODE/hasaki-agent/custom-workspace" });
    assert.equal(paths.workspacePath, resolve("E:/CODE/hasaki-agent/custom-workspace"));
    assert.equal(paths.bridge.executable, resolve("E:/CODE/hasaki-agent/.venv/Scripts/python.exe"));
    assert.equal(paths.configPath, resolve(paths.workspacePath, "config.toml"));
  });
  it("keeps development runtime execution inside the repository and mutable workspace inside the repository", () => {
    const paths = resolveDesktopRuntimePaths({
      packaged: false,
      appPath: "D:/Coding/Shiori/desktop",
      homePath: "C:/Users/shiori",
      repositoryRoot: "D:/Coding/Shiori",
    });

    assert.equal(paths.configPath, resolve("D:/Coding/Shiori/workspace/config.toml"));
    assert.equal(
      paths.configTemplatePath,
      resolve("D:/Coding/Shiori/config/examples/config.example.toml"),
    );
    assert.equal(paths.workspacePath, resolve("D:/Coding/Shiori/workspace"));
    assert.equal(paths.bridge.cwd, resolve("D:/Coding/Shiori/apps/backend"));
    assert.deepEqual(paths.bridge.args, [
      "main.py",
      "bridge",
      "--workspace",
      paths.workspacePath,
      "--config",
      paths.configPath,
    ]);
  });

  it("keeps all mutable packaged data outside the installation resources", () => {
    const paths = resolveDesktopRuntimePaths({
      packaged: true,
      appPath: "C:/Users/shiori/AppData/Local/Programs/Shiori/resources/app.asar",
      homePath: "C:/Users/shiori",
    });

    assert.equal(paths.configPath, resolve("C:/Users/shiori/.hasaki/workspace/config.toml"));
    assert.equal(paths.bridge.executable, resolve("C:/Users/shiori/AppData/Local/Programs/Shiori/resources/runtime/shiori-runtime.exe"));
    assert.deepEqual(paths.bridge.args, [
      "bridge",
      "--workspace",
      paths.workspacePath,
      "--config",
      paths.configPath,
    ]);
  });

  it("creates the first config in the workspace without touching installation resources", () => {
    const directory = mkdtempSync(join(tmpdir(), "shiori-runtime-paths-"));
    try {
      const templatePath = join(directory, "resources", "config.example.toml");
      const configPath = join(directory, "home", ".hasaki", "workspace", "config.toml");
      mkdirSync(dirname(templatePath), { recursive: true });
      writeFileSync(templatePath, "[llm]\n", { encoding: "utf-8" });
      ensureDesktopRuntimeConfig({
        bridge: { executable: "runtime.exe", args: ["bridge"], cwd: directory },
        configPath,
        configTemplatePath: templatePath,
        workspacePath: join(directory, "home", ".hasaki", "workspace"),
      });

      assert.equal(existsSync(configPath), true);
      assert.equal(readFileSync(configPath, "utf-8"), "[llm]\n");
    } finally {
      rmSync(directory, { recursive: true, force: true });
    }
  });

  it("keeps workspace and config derivation in the runtime path contract", () => {
    const workspacePath = resolveDesktopWorkspacePath("C:/Users/shiori");
    assert.equal(workspacePath, resolve("C:/Users/shiori/.hasaki/workspace"));
    assert.equal(resolveDesktopConfigPath(workspacePath), resolve("C:/Users/shiori/.hasaki/workspace/config.toml"));
  });
});

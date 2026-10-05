"""Release gates must execute a real workspace package and the full backend suite."""

import json
import shlex
from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]


def test_workflow_and_root_script_filters_resolve_and_fail_when_unmatched():
    packages = {
        package["name"]: package
        for path in (ROOT / "apps").glob("*/package.json")
        for package in [json.loads(path.read_text(encoding="utf-8"))]
    }
    commands = list(json.loads((ROOT / "package.json").read_text(encoding="utf-8"))["scripts"].values())
    for path in (ROOT / ".github/workflows").glob("*.yml"):
        workflow = yaml.safe_load(path.read_text(encoding="utf-8"))
        commands.extend(
            line.strip()
            for job in workflow["jobs"].values()
            for step in job.get("steps", [])
            for line in step.get("run", "").splitlines()
        )
    filtered = [command for command in commands if command.startswith("pnpm ") and "--filter" in command]
    assert filtered, "No workspace build/release commands were checked"
    for command in filtered:
        tokens = shlex.split(command)
        package_name = tokens[tokens.index("--filter") + 1]
        assert package_name in packages, command
        assert "--fail-if-no-match" in tokens, command
        script = tokens[tokens.index("run") + 1]
        assert script in packages[package_name]["scripts"], command


def test_windows_release_runs_full_regression_before_packaging():
    workflow = yaml.safe_load((ROOT / ".github/workflows/windows-release.yml").read_text(encoding="utf-8"))
    commands = [step["run"] for step in workflow["jobs"]["package"]["steps"] if "run" in step]
    regression = next(command for command in commands if "-m pytest" in command)
    assert regression.rstrip().endswith("tests/backend")
    assert "-W error" in regression
    packaging = next(command for command in commands if "run package:win" in command)
    assert commands.index(regression) < commands.index(packaging)
    # Separate steps preserve each native command's exit status under PowerShell.
    assert all(command.count("pnpm ") <= 1 for command in commands)

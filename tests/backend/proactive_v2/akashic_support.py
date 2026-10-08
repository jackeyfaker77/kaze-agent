"""Akashic 对齐测试的临时身份文件准备。"""

from pathlib import Path

from agent.persona import AKASHIC_BEHAVIOR_RULES


def reset_veda(workspace: Path) -> None:
    path = workspace / "memory/VEDA.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(AKASHIC_BEHAVIOR_RULES, encoding="utf-8")

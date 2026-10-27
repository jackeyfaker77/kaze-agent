"""Restored snapshots cannot prove effects performed after their capture time."""
from pathlib import Path


QUARANTINE = '.kaze-restore-quarantine.json'


class RestoreQuarantined(RuntimeError):
    pass


def require_live_workspace(workspace: Path) -> None:
    target = workspace.resolve()
    for parent in (target, *target.parents):
        marker = parent / QUARANTINE
        # Even a damaged marker must fail closed; parsing is for offline inspection.
        if marker.exists() or marker.is_symlink():
            raise RestoreQuarantined(
                '恢复副本处于隔离状态，不能启动模型、工具、渠道或后台任务。'
                '旧备份不能证明之后的外部结果；请保留隔离并进行离线核对。')

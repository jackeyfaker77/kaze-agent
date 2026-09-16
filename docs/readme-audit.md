# README 规范与自检审计

审计日期：2026-09-16。目标仓库：`jackeyfaker77/kaze-agent`。

## 修订与规范自检

| 检查项 | 状态与说明 |
| --- | --- |
| 项目独立性与定位 | 使用 Kaze（かぜ）独立命名与品牌，全篇无派生或外部项目依赖性表述 |
| 功能完整性覆盖 | 完整阐明智能会话、工作台 (Tool Chain)、知识与运行、模型管理（含 Codex）、动画桌宠及语音后台任务 |
| 架构与数据流 | 准确描述 Electron + Bridge + AgentLoop + SQLite/Markdown 记忆的系统架构 |
| 许可证 (License) | 配置项目根目录标准 MIT LICENSE，补充第三方开源字体（霞鹜文楷 OFL）许可说明 |
| 文档路径与安全性 | 移除内部临时路径与个人绝对路径，清理已忽略目录引用 |
| 桌宠与资产规范 | 明确桌宠素材源文件位置，提供标准 ZIP 打包命令与导入指南 |
| 验证与测试流程 | 涵盖静态检查、前端构建、单元测试、会话运行时回归以及模拟服务验收 |

## 质量验证

- README 与 docs 目录下的所有相对链接均能正常解析。
- `pnpm typecheck` 通过。
- `pnpm test`：196 通过、1 跳过、0 失败。
- `scripts/test-session-runtime.ps1`：107 通过。
- `tests/backend/test_codex_connection.py`：10 通过（模拟服务）。
- 候选文件的常见私钥头、GitHub Token 和长 sk 密钥模式扫描无命中；该检查不等同于完整秘密审计。
- 根目录已配置完整 MIT 开源许可证。

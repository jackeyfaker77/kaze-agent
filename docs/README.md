# Kaze 文档中心 (Documentation)

欢迎查阅 Kaze（かぜ）桌面智能助手的设计与开发文档。

## 目录索引

- [项目总览](../README.md)：快速开始、系统特性、安装与开发说明
- [总体架构](knowledge/architecture/overview.md)：装配、执行入口、状态归属和运行协调
- [项目知识库](knowledge/index.md)：能力地图、模块职责、执行流程和修改影响
- [运行协调](runtime-coordination.md)：会话锁、忙状态、ChatLane 与发送后历史提交
- [可重建会话投影](architecture/conversation-projection-v1.md)：Runtime Event Log、旧历史基线、幂等重放与崩溃恢复
- [可重建执行状态与处置](architecture/execution-projection-v1.md)：执行事实投影、失败与中断查看、副作用核对及显式安全重试
- [收发队列关联与处置](architecture/transport-review-v1.md)：队列与执行双向查看、分段核对、关闭与受限重新排队
- [主动推送指南](_handbook/proactive-guide.md)：Default / Wake、源配置、规则和投递
- [Feed 内容源](integrations/feed.md)：订阅采集、正文缓存、版本确认和投递协调
- [Windows 设备上下文](integrations/device.md)：锁屏、空闲、前台应用、电源状态与有效期
- [日历和待办](integrations/calendar.md)：ICS 订阅、本地任务、版本提醒和发送前核对
- [Codex 订阅连接](codex-connection.md)：ChatGPT 订阅设备授权登录与流式调用机制
- [小风鼬桌宠规范](kaze-pet.md)：桌宠动画状态、图集规格与 ZIP 资源包打包指南
- [品牌设计与视觉资产](kaze-branding.md)：应用图标、UI 徽标与设计规范说明
- [工作台设计审计](workbench-v3-feasibility-audit.md)：工作台数据流与交互记录架构说明
- [README 检查审计](readme-audit.md)：代码规范与文档自检报告

## 许可证说明

本项目核心代码基于 [MIT License](../LICENSE) 开源。相关第三方字体采用 [SIL OFL 1.1](../apps/desktop/renderer/public/licenses/OFL-LXGW-WenKai.txt)。

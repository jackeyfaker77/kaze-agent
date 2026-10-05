# 历史测试契约

项目已从角色、剧情和多种角色主动行为，迁移到普通会话与工作区共享服务。这些历史测试依赖的实现已不在当前生产源码中；直接收集它们会在导入时失败。

为保留历史覆盖和迁移依据，原测试以 `.py.txt` 扩展名存放在本目录，不作为当前 pytest 用例收集。没有修改 pytest 的收集规则。

- 根目录下的模块树：整文件归档。`manifest.json` 记录原路径、已不存在的生产模块、测试名称和原文件 SHA-256；归档时逐文件验证内容一致。
- `fragments/`：从混合测试文件中保留的、明确依赖已删除接口的测试或辅助代码。混合文件中仍有效的用例继续参与回归。
- `migrated/`：重写测试契约时保存的迁移前快照，便于比较新旧行为。

本目录的 `.gitattributes` 禁止 Git 转换归档 `.py.txt` 文件的换行符，确保跨平台检出后原始字节和清单中的 SHA-256 一致。

主要替代关系：

| 历史契约 | 当前回归入口（相对 `tests/backend/`） |
| --- | --- |
| 角色会话、角色桥接服务 | `test_session_architecture.py`、`test_session_desktop_services.py`、`desktop_bridge/test_server.py` |
| 角色渠道绑定和角色消息投递 | `core/channels/test_hub.py`、`infra/channels/test_clients.py`、`agent/tools/test_message_push_targets.py` |
| 角色记忆与权限、角色 Markdown 文件 | `core/memory/test_engine_contract.py`、`core/memory/test_markdown_schema.py` |
| 角色调度元数据 | `agent/test_scheduler_job_store.py`、`agent/test_scheduler_service.py`、`agent/tools/test_schedule.py` |
| 角色桌宠包 | `core/pets/test_packages.py`、`plugins/desktop_pet/test_tool.py` |
| 旧主动循环、漂移、关系/场景触发 | `bootstrap/test_proactive.py`、`bootstrap/test_proactive_config.py`、`bootstrap/test_proactive_defaults.py`、`session/test_presence.py` |
| NovelAI、剧情、表情包插件 | 对应产品模块已删除，无当前功能替代；旧配置兼容由 `agent/test_legacy_config.py` 检查 |

恢复相应产品功能时，应先对照这里的原测试确认行为，再将适用用例迁回活动测试目录。不要直接恢复已失效的导入或角色隔离假设。

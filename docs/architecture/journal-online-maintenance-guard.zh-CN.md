# Journal 在线维护边界修复（2026-11-01）

生产 P1 验收首次在线创建第二个 ExecutionJournal 时，构造器全量 rebuild_projection 清空并重放投影，长时间持有 SQLite 写锁。在线 Host 的 BEGIN IMMEDIATE 等待超时后退出。原始事件未丢失；后续改用冷态准备完成 P1 定向验收。

## 方案与选择

1. **采用：保护 Host 维护边界。** 使用现有 workspace owner.lock，将外部可写 journal 初始化和显式重建限制在冷态；持锁直到维护结束，阻止 Host 同时启动。拥有真实 WorkspaceOwner 的当前进程可正常初始化自己的 journal。身份依据进程内登记、打开的 OS lease 与 PID，不信任状态文件。
2. 普通打开改为增量同步，并管理投影 schema/校验/迁移。可降低启动耗时，但扩大了投影一致性与损坏恢复的改动范围，暂不采用。
3. 增大 busy timeout。只能延后失败，不能消除在线全量重建和阻塞，不采用。

本修复保留冷态全量事实校验、未知结果处理和既有多 journal 离线测试语义。在线诊断必须使用 sqlite mode=ro，不能通过创建 ExecutionJournal 读取生产数据。该限制不是任意 SQLite 写入的全局锁服务，也不改变旧已部署程序；上线后才生效。

## 验证与收件处置

真实子进程验证在线外部初始化和已存在 peer 的显式重建被拒绝、Host 继续执行、冷态维护阻止 Host 启动、冷态重建仍可用。另验证继承的进程内 owner 身份不能越权；生产入口和执行投影回归一并运行。

验证结果：相关投影/Host/Windows 回归 56 passed；最后加入进程身份保护后，真实 Host 子进程与专项回归 15 passed、1 skipped（114.57 秒）。两组有重叠。Ruff、生产与测试目标 Pyright 均通过，diff check 通过。独立 review agent 复审最终代码无待修 findings，并独立验证异常释放锁。上述修复阶段未部署；后续生产发布单独记录验收证据。

收件 #202 对应 execution `c31e04bd1b754ac48d04f51d431caa0f`：模型响应事实记录 CancelledError，执行 interrupted、result=null；无工具副作用、关联 reply 队列或持久发送回执。相邻已发送消息 #200 关联收件 #199，不能算作 #202 的回复。未将其标为完成，也未重试、补发或清除 needs_review。

独立流式预览没有足够持久证据证明完全未显示，因此结论限于“没有已完成回复的证据”，不得把缺少回执等同于绝对没有外部发送。核对结果见 `docs/acceptance/2026-11-01-journal-lock-and-inbound-review.json`。后续部署与重启不得重跑或补发该收件。

# 命令验收重启后的串行阻挡

未结算的 `verify:command` 预占阻止全库新 Goal phase，包括旧 Goal 已 cancelled 的情况。模型调用及纯文件验收未知不扩大为全局阻挡。正常收到已知验收回执后自动解除。

Host 硬退出后，子进程可能仍运行；重新启动不会推测它已经结束。先确认命令及其派生工作已停止，检查产物与共享资源，再通过可信 Host CLI 记录处置：

```powershell
$env:PYTHONPATH = 'apps/backend'
.venv/Scripts/python.exe -m core.goals.cli --workspace '<workspace>' show '<goal_id>'
.venv/Scripts/python.exe -m core.goals.cli --workspace '<workspace>' resolve-command '<goal_id>' '<reservation_id>' --version <current_version> --confirm-stopped --note '进程已停止，产物及共享资源已核对：具体证据'
```

处置写入原 Goal 事件日志，支持重启与投影重建；必须使用当前版本，不能处置仍有在途 phase 的 Goal。只释放串行阻挡，不退款、不恢复取消目标、不伪造验收通过、不自动重试。该入口不向模型注册，不提供自动进程查杀。

回归覆盖真实命令启动后 Host `os._exit(74)`、取消后退出、旧进程迟到写入、处置前持续阻挡、处置重放后放行及额度不变。

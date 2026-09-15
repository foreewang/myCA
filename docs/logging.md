# 日志规范与验收

## 文件职责

| 文件 | 职责 |
| --- | --- |
| `logs/api_server.log` | API 异常、服务生命周期、相机监督器事件、非任务设备诊断 |
| `logs/api_access.log` | 每个请求一条结果摘要；不含异常堆栈、查询串和正文 |
| `logs/task.log` | 任务入队、执行、阶段、终态以及任务内设备和文件读写诊断 |
| `logs/camera_worker.log` | 相机子进程和 SDK 诊断，独立进程写入 |

监督器始终写 API 日志，即使它被任务调用；关联字段仍会保留。设备日志根据当前任务上下文分流，非任务调用写 API 日志。

## 字段与级别

通用字段为带时区的 ISO 时间、级别、logger、`pid`、`request_id`、`task_id`。新生命周期事件使用稳定的 `event=` 名称：`request_completed`、`task_queued`、`task_started`、`task_stage`、`task_completed`、`task_canceled`、`task_failed`。运行时停止而尚未执行的排队任务记为 `task_deferred`，状态恢复仍由现有任务恢复机制负责。

请求摘要包含 `method`、`path`（路由模板）、`status`、`outcome`、`elapsed_ms`。日志在响应正文和请求内后台工作结束后输出；若已发送响应头后发生异常，保留已发送的状态码并标记 `outcome=failed`。服务器生成请求标识并返回 `X-Request-ID`；任务入队时保存该标识，后台执行、监控和同步相机命令继续传递。CLI 无 HTTP 请求，使用 `request_id=-`。没有任务的手动操作使用 `task_id=-`，任务执行期间不应出现该值。

任务阶段记录 `stage`、`well`，同一连续阶段与孔位不重复输出；任务完成、运行失败或取消记录耗时。入队前失败没有执行耗时，记录错误码或原因。相机 IPC 会传递当前请求与任务上下文，每个命令结束后恢复，不能依靠线程或进程自动继承上下文。

- `INFO`：请求结果、任务生命周期和阶段切换。
- `WARNING`：重试、可恢复故障、耗时阈值超限。
- `ERROR`：最终失败，处理边界记录异常堆栈。
- `DEBUG`：逐帧、轮询、寄存器数据等高频细节。

访问日志只记录失败摘要；未处理请求异常由 API 错误处理器记录一次，文件 handler 过滤 Uvicorn 对同一异常的重复报告。CLI 的结果 JSON 保留在标准输出。

## 初始化与轮转

`workflow/logging_config.py` 是主进程配置入口。API 初始化和 CLI `main()` 调用它；库函数本身不擅自初始化文件。`workflow`、`devices` 和 `uvicorn.error` 共享相应文件的 handler，子 logger 通过传播使用父 handler，不重复挂载。重复初始化不能增加文件 writer。

四类日志统一按服务器本地日期每日轮转，保留最近 30 个自然日（含当天，作为一个月的固定保留期），不再按文件大小切分。活动文件名保持不变，例如 `api_server.log`；归档文件带日期，例如 `api_server.log.2026-09-15`。跨过本地午夜后的第一条日志触发轮转，无日志的日期不生成空归档。

初始化和每日轮转时按归档日期清理早于保留窗口的文件，不是简单保留 29 个归档，因此低频日志和长时间停机后也遵守日期保留规则。停止运行期间不会后台删除文件，下次初始化或写入触发清理。旧版 `.1`、`.2` 等数字后缀归档按文件修改日期执行相同清理规则，不重新命名；其他文件不受影响。

相机子进程独立配置 `camera_worker.log`，使用相同的每日轮转与 30 天保留策略，不得与主进程共用轮转文件。保留 `COLONY_CAMERA_WORKER_LOG_LEVEL` 和 `COLONY_CAMERA_WORKER_LOG_PATH`，空路径关闭子进程文件输出。

文件 handler 的锁只保护同一进程。部署保持单 API worker；CLI 离线任务应在 API 停止后执行，不能启动多个独立进程同时写同一日志目录。不要用外部工具同时轮转这些活动文件。

## 生产脱敏与排查

代码在未设置环境变量时默认关闭脱敏。当前 Linux 启动脚本 `start_api.sh` 设置 `COLONY_LOG_REDACT_SENSITIVE=0`，Windows 脚本 `start_api.bat` 设置为 `1`。需要切换时修改实际使用的启动脚本并重启服务；脚本中的赋值会覆盖终端预先设置的同名变量，历史日志不会改变。

设置 `COLONY_LOG_REDACT_SENSITIVE=1` 后，所有文件 formatter 对字段、正文与异常文本中的敏感路径和明确标注的任务 ID 脱敏。任务 ID 映射为稳定的 `<task:xxxxxxxx>`，不同文件使用相同标识。重复脱敏不会改变映射，`task_id=-` 保持不变。路径脱敏由 `COLONY_LOG_REDACT_PATHS` 控制，任务 ID 脱敏由 `COLONY_LOG_REDACT_TASK_ID` 控制，开启总开关时两者默认开启；总开关为 `0` 时两者均不生效。

任务 ID 应通过任务上下文或 `extra={"task_id": task_id}` 传递；正文和异常文本需明确使用 `task_id=...` 或 JSON 的 `"task_id": "..."`。不对正文中的裸任务 ID 全局替换，以免误改相同文本的耗时、状态码或请求标识。

1. 用响应头 `X-Request-ID` 查询访问摘要及 API 错误。
2. 用相同 `request_id` 查 `task_queued`，取得任务标识。
3. 用任务标识查阶段、设备诊断及终态；相机故障同时查 API 和 worker 文件。
4. 需要从原始任务 ID 计算生产标识时，在启用相同脱敏环境变量的 Python 中调用 `sanitize_log_detail("task_id=" + task_id)`，不要把原始 ID 重新写入日志。

## 自动验收

```text
python -m pytest tests/test_daily_logging.py tests/test_logging_routes.py tests/test_logging_integration.py tests/test_core_workflow.py tests/test_api_runtime_integration.py tests/test_camera_process_supervisor.py -q
```

验收覆盖幂等配置、跨 logger 单次写入、轮转、真实 HTTP 请求与失败关联、后台任务和监控线程、设备诊断路由、CLI JSON 输出、生产脱敏及独立 worker 进程。硬件依赖用确定性替身隔离，不触发实际运动或相机采集。

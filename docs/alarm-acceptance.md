# 单一报警接口实现与验证记录

## 2026-11-09 停机路径与报警持久化隔离

新增 `tests/test_alarm_nonblocking.py`：阻塞真实报警服务的存储写入，验证硬限位、超时、普通异常及取消仍能完成双轴停止命令；包括第一轴停止命令返回失败的场景。另验证异步事件顺序、原任务关联、发生时间、只读查询不等待写入、队列满和写入线程启动失败不回退同步写入。测试使用模拟电机，不连接实际设备。

本次全量回归 `python -m pytest tests -q`：881 项测试通过、1 项跳过、96 个子测试通过，存在一条 CUDA 路径提示。补充写入线程故障健康状态检查后，重新执行非阻塞、报警服务及 API 测试，38 项通过；`git diff --check` 通过。

本次交付是工控机端报警记录与查询、业务后端轮询参考实现及接入文档。生产接入使用现有业务后端；现场 TLS、设备身份、服务凭证和看板集成按 [接入文档](alarm-api.md) 配置。

## 实现与证据

| 要求 | 实现位置 | 自动化验证 |
| --- | --- | --- |
| 唯一新增报警路由，只读、全类型、分页 | `workflow/api_server.py` | `test_alarm_api.py`：仅 GET、拒绝类型筛选、凭证检查、参数限制、P0 返回 200、存储故障返回 503 |
| 状态与事件同事务，单调序号和正常重启续传 | `workflow/alarm_store.py` | `test_alarm_store.py`：事务故障回滚、并发写入、重启后的流与序号、重复发生计数 |
| 两次轮询之间发生并恢复仍保留历史 | 存储事件流与后端消费 | `test_alarm_store.py`、`test_alarm_api.py`：真实 SQLite；真实 Uvicorn HTTP 与参考客户端联调 |
| 首次快照、完整分页后应用、旧事件不能覆盖新状态 | 冻结快照及 `tools/poll_alarms.py` | `test_alarm_store.py`、`test_alarm_poller.py`：并发变化、部分分页失败、版本保护 |
| 保留期/流变化明确重同步、历史缺口 | 30 天历史、410、离线流轮换工具 | `test_alarm_store.py`、`test_alarm_poller.py`：活动状态保留、备份恢复轮换、缺口记录；快照提交后退出并超过保留期再启动的真实存储复现 |
| 任务失败、中断、重复任务 ID | `task_store.py`、`task_runtime.py`、`alarm_task_hooks.py` | `test_alarm_task_hooks.py`：每次接收有独立执行编号、任务 JSON 对账重试/去重、失败记录不会被后续成功删除 |
| 相机/运动等明确故障边界及证据恢复 | 相机监督器、Modbus、位移台、物镜、交接模块 | `test_alarm_device_hooks.py` 及原有相机/运动回归：最终通信失败、超时、限位、偏差、停止失败、视频产物；进程重建不等同恢复、停止命令应答不等同静止 |
| 后台报警关联原始任务 | 相机会话与操作上下文 | `test_alarm_camera_correlation.py`：监测、异步启动/停止、新会话隔离、共享相机当前任务优先 |
| 视觉、配置、存储、内部 API 错误 | `alarm_task_hooks.py` 与各模块处理边界 | `test_alarm_task_hooks.py`：模型/推理/图像质量、GPU 降级、补偿偏差、配置、读写失败与恢复、HTTP 诊断关联 |
| 取消、占用、参数错误不产生生产故障噪声 | INFO 历史与采集边界 | 报警任务/设备/API 测试：正常取消、忙碌、合法零菌落、请求覆盖参数、取消或跳过预检 |
| 记录器失败不掩盖原业务异常、不伪装无报警 | `workflow/alarm_service.py` | `test_alarm_service.py`、`test_alarm_task_hooks.py`：写入中断事实、失败恢复重试、原异常保留、读取 503、监测线程启动失败不阻断业务 |
| 监测线程与工作线程状态、新鲜度、磁盘阈值 | 每 5 秒本地采样；GET 只读缓存 | `test_alarm_service.py`、`test_alarm_api.py`：HTTP 存活但任务线程停止；相机状态锁被占用时 GET 仍能通过 HTTP 返回；未观察硬件状态为 unknown |
| 单消费位置、持久化后推进、断线补拉 | `tools/poll_alarms.py` | `test_alarm_poller.py`：消费事务回滚、重启、去重、积压连拉、鉴权/网络/503/协议错误保留状态、超过 60 秒由后端报告通信异常 |
| 不根据轮询停止增加停产/复位控制 | 仅新增观测与只读路由 | 代码中无后端通信租约、ACK 或复位路由；原有任务、设备、硬件占用及 API 生命周期回归通过 |
| 公开内容与内部诊断分离 | 固定报警目录、资源路径摘要、诊断编号 | 报警服务/任务/相机测试验证 SDK 文本和绝对路径不进入事件；HTTP 异常诊断编号可对应请求日志 |

## 测试结果与复现

2026-11-04 缺陷修复后，使用 Windows 上现有 `ca` 环境验证，未连接或操作真实设备。报警专项测试 **115 项通过**；本次相机、核心工作流及 Modbus 回归 **201 项测试和 8 个子测试通过**，两批测试文件不重叠。`git diff --check` 通过。

本次新增验证覆盖：相机启动超时、启动异常和无效握手导致的重启失败专用报警；同一异常按报警代码去重；旧拍照、同步录像及监测响应不能恢复随后发生的故障；短录像有效保存后恢复、空帧/空文件/缺失文件不恢复；物镜误差正负 50 脉冲边界及超差后不写入成功状态；磁盘阈值非法时回退默认值并持续记录，修正配置重启后恢复配置报警。物镜超差现在会中止切换，这是本次明确修正的业务成功判定。

报警专项测试全部使用临时数据库、日志和模拟设备，可执行：

```bash
python -m pytest tests/test_alarm_api.py tests/test_alarm_store.py tests/test_alarm_service.py tests/test_alarm_poller.py tests/test_alarm_task_hooks.py tests/test_alarm_device_hooks.py tests/test_alarm_camera_correlation.py -q
```

本次相关回归命令：

```bash
python -m pytest tests/test_camera_process_supervisor.py tests/test_camera_record_service.py tests/test_camera_reliability.py tests/test_camera_executor_contract.py tests/test_camera_api_contract.py tests/test_core_workflow.py tests/test_modbus_rtu.py tests/test_modbus_cache_invalidation.py tests/test_modbus_switch_gap.py -q
```

## 现场上线验证边界

上述验证证明代码与协议行为，不代表已经部署到工控机或接入真实业务看板。现场还需在已配置的 HTTPS、凭证、实际设备与业务后端下，验证正常网络无积压时的看板展示目标（记录后 10 秒内）、真实故障恢复与磁盘/断网场景。设备软件记录无法补回记录器与任务文件同时不可写期间的全部瞬时故障；这一缺口会被标记。人工确认、备注和通知接入由业务后端持有，不通过只读接口修改设备状态。

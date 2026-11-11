# 报警轮询示例、全量代码与恢复说明

依据 2026-11-04 当前工作区实现整理。本文说明代码实际行为，不表示已经连接真实设备验证。

**出现报警不等于必须停止 API 服务、修改代码并重启。** 先处理报警对应的原因；持续性报警通过后续成功操作或监测采样自动恢复，历史事件则保留。只有原因是代码缺陷时才需要改代码；启动配置变化、进程无法恢复等情况可能需要重启。

本次报警层负责记录和查询，没有新增“有报警就拒绝所有任务”的总开关，也没有自动续跑失败任务。故障仍可能使当前任务按原有异常处理失败或执行停止动作。能查询到 HTTP 200、不再有活动报警、API 进程重启成功，都不能单独证明设备可以安全生产。

## 1. 可以直接参照的请求

```http
GET /api/alarms?mode=events&limit=100
Authorization: Bearer <已配置的 COLONY_ALARM_TOKEN>
```

- `events`：自游标之后的事件，包含发生、重复、恢复，以及任务失败等历史事实。不带游标时，从当前仍保留的最早历史开始。
- `snapshot`：当前未恢复的持续性报警。没有任务失败等纯历史事实，也没有已经恢复的报警。
- `limit`：默认 100，范围 1～500。只接受 `mode`、`cursor`、`limit`，不支持 `category` 筛选或重复参数。
- 下一次请求把返回的 `next_cursor` 原样作为 `cursor` 传回，正确进行 URL 编码。不能自己推算游标。

只看 `snapshot` 会漏掉“两次轮询之间已经发生并恢复”的故障；只反复请求不带游标的 `events` 会重复读历史。因此生产接入应维护事件游标。

## 2. 一次响应长什么样

以下是便于阅读的结构示例，ID、时间和游标均为示意值；`monitoring` 仅展示部分字段。完整 JSON 见 [全量示例文件](examples/alarm-polling.json)。

```json
{
  "protocol_version": 1,
  "device_id": "device-001",
  "stream_id": "stream-example",
  "boot_id": "boot-example",
  "mode": "events",
  "retention_start_sequence": 1,
  "watermark": 101,
  "items": [
    {
      "alarm_id": "alarm-camera-001",
      "category": "camera",
      "code": "CAMERA_SDK_ERROR",
      "severity": "P1",
      "message": "相机采集操作失败，请检查相机连接和运行状态",
      "component": "camera",
      "resource": "default",
      "task_id": "task-001",
      "execution_id": "execution-001",
      "diagnostic_id": "diagnostic-001",
      "active": true,
      "transition": "occurred",
      "count": 1,
      "first_occurred_at": "2026-11-04T02:00:00+00:00",
      "occurred_at": "2026-11-04T02:00:00+00:00",
      "recorded_at": "2026-11-04T02:00:00+00:00",
      "sequence": 101,
      "event_id": "stream-example:101"
    }
  ],
  "has_more": false,
  "next_cursor": "<服务端返回的游标，原样保存>",
  "monitoring": {
    "store": {"status": "healthy", "recording_gap_possible": false},
    "hardware": {"status": "unknown", "reason": "no_active_hardware_probe"}
  }
}
```

`sequence` 决定消费顺序，`event_id` 用于事件去重；`alarm_id` 把同一轮持续故障的发生、重复和恢复串起来。一个报警尚未恢复时重复发生，通常沿用 `alarm_id` 并增加 `count`；恢复后再次发生会生成新的 `alarm_id`。

`task_id`、`execution_id` 可能为空。相同任务 ID 的不同执行由 `execution_id` 区分。`resource` 可能是轴、模型或文件路径的摘要，并不总是可展示的设备名称。原始异常、SDK 文本、堆栈和本机绝对路径不会放进报警文案；使用 `diagnostic_id` 查本地日志。

API 时间带时区，上例 `02:00+00:00` 对应台北时间 10:00。`occurred_at` 是本次发生/更新/恢复事件时间，`first_occurred_at` 保留这一轮故障的首次时间；补录历史的发生时间和记录时间可能不同。

### 同一故障的完整过程

下表是连续事件的字段摘录，不是完整 HTTP 响应。

| sequence | code | alarm_id | transition | active | count | 含义 |
| --- | --- | --- | --- | --- | --- | --- |
| 101 | CAMERA_SDK_ERROR | A | occurred | true | 1 | 相机采集失败 |
| 102 | CAMERA_SDK_ERROR | A | updated | true | 2 | 同一故障再次发生 |
| 103 | TASK_FAILED | B | recorded | false | 1 | 本次任务已经失败，作为历史保存 |
| 104 | CAMERA_SDK_ERROR | A | recovered | false | 2 | 后续实际采集成功，确认相机能力恢复 |

恢复事件的 `code` 和 `message` 仍描述原故障，不能仅看文案判断是否仍在报警；必须看 `transition` 和 `active`。

**`active=false` 有两种不同含义：**

- `recovered`：原来处于活动状态的持续报警已经恢复。
- `recorded`：一次已经发生的事实，例如任务失败或录像文件损坏；从写入时起就不是活动状态，不表示该次任务成功或文件已经修复。

没有新增事件时，增量响应为 `items: []`、`has_more: false`，仍返回有效的 `next_cursor` 和 `monitoring`。**空增量只表示没有新变化，不表示旧报警已经解除。** 后端必须保留已知活动状态。

快照响应使用同样的事件记录结构，并多出 `snapshot_expires_at`、`events_cursor`。快照末页的 `next_cursor` 为 `null`。快照为空只表示当前没有已记录的持续性报警；历史失败仍能通过 `events` 取得，硬件状态仍可能是 `unknown`。

`monitoring` 还包括：

- `monitor`：监测状态、采样时间和采样距今秒数；可能是 `starting`、`healthy`、`degraded`、`failed`、`stale` 或 `stopped`。
- `task_worker`：任务线程是否存活、是否接单、队列大小及采样时间。
- `disk`：报警数据库所在磁盘的剩余比例、当前等级、实际阈值、是否回退到默认阈值。
- `hardware`：当前实现固定说明未主动探测硬件，不能由它认定电机或相机健康。

## 3. 当前全部 51 个公开代码

以下覆盖合并后的 `alarm_catalog()` 全部唯一代码，其中 3 个是 INFO 正常事件。它是当前代码目录的全量清单，不表示能够识别所有可能的硬件故障。未知代码应保留并通用展示，不能丢弃整页。

“持续”表示等待相同 `code + component + resource` 的恢复证据；“历史”表示 `transition=recorded, active=false`，不等待恢复事件。

### 相机与录像（16 个）

| code | category / 级别 | 形式 | 场景及恢复依据 |
| --- | --- | --- | --- |
| CAMERA_SDK_ERROR | camera / P1 | 持续 | SDK 采集操作失败；后续实际采集成功 |
| CAMERA_COMMAND_TIMEOUT | camera / P1 | 持续 | 相机操作超时；后续实际采集成功 |
| CAMERA_WORKER_FAILED | camera / P1 | 持续 | 相机工作进程异常；后续实际采集成功 |
| CAMERA_WORKER_RESTART_FAILED | camera / P1 | 持续 | 工作进程启动/重建失败；后续实际采集成功，单有新 PID 不够 |
| CAMERA_RECORDING_UNHEALTHY | camera / P1 | 持续 | 后台录像异常终止；后续录像取得有效新帧，或短录像有效完成保存 |
| CAMERA_RECORDING_STALLED | camera / P1 | 持续 | 录像持续不产新帧；后续录像取得有效新帧，或短录像有效完成保存 |
| CAMERA_RECORD_RESULT_MISSING | camera / P1 | 历史 | 本次录像结束未取得视频信息 |
| CAMERA_RECORD_PATH_INVALID | camera / P1 | 历史 | 本次录像没有有效文件位置 |
| CAMERA_RECORD_EMPTY | camera / P1 | 历史 | 本次录像未录入有效帧 |
| CAMERA_RECORD_PATH_MISMATCH | camera / P1 | 历史 | 本次录像实际文件与预期不一致 |
| CAMERA_RECORD_FILE_MISSING | storage / P1 | 历史 | 本次录像文件缺失 |
| CAMERA_RECORD_FILE_EMPTY | storage / P1 | 历史 | 本次录像文件为空 |
| CAMERA_RECORD_PROMOTE_FAILED | storage / P1 | 历史 | 录像产物完成保存失败 |
| CAMERA_SHUTDOWN_FINALIZING | camera / P2 | 历史 | 服务退出时录像仍在完成保存 |
| CAMERA_SHUTDOWN_INCOMPLETE | camera / P1 | 历史 | 服务退出前相机操作未正常完成 |
| CAMERA_RECORDING_STOPPED | camera / INFO | 历史 | 录像正常结束并完成保存 |

### 运动、物镜和交接（15 个）

| code | category / 级别 | 形式 | 场景及恢复依据 |
| --- | --- | --- | --- |
| SERIAL_CONNECTION_FAILED | motion / P1 | 持续 | 串口连接失败；后续真实通信事务成功 |
| MODBUS_COMMUNICATION_FAILED | motion / P1 | 持续 | 通信重试耗尽；对应总线/从站后续真实通信成功 |
| MOTION_MOVE_FAILED | motion / P1 | 持续 | 运动操作失败；后续相同范围的运动成功 |
| MOTION_POSITION_UNAVAILABLE | motion / P1 | 持续 | 位置/状态读不到或无效；对应组件取得所需位置或完成运动检查 |
| MOTION_TIMEOUT | motion / P1 | 持续 | 规定时间未到位；对应运动路径后续成功完成 |
| MOTION_ARRIVAL_OUT_OF_TOLERANCE | motion / P1 | 持续 | 到位误差超限；后续到位误差检查通过 |
| MOTION_HARD_LIMIT_EXCEEDED | motion / P0 | 持续 | 实际位置超出硬限位范围；后续已执行的限位验证通过 |
| MOTION_DRIVE_FAULT | motion / P1 | 持续 | 驱动器报告故障；总线路径需故障复位后读到故障位清除，位移台路径需后续运动的状态字验证无故障 |
| MOTION_STOP_FAILED | motion / P0 | 持续 | 停止命令失败，实际停止状态未确认；目前只有 XY 运动成功后的静止验证路径会恢复对应报警，只有停止命令应答不够 |
| MOTION_STATE_UNCERTAIN | motion / P0 | 持续 | 静止确认后位置/状态异常变化；后续静止验证通过 |
| STAGE_RECIPROCATION_FAILED | motion / P1 | 持续 | 往复运动失败；后续往复流程成功完成一次目标移动且未请求停止 |
| OBJECTIVE_SWITCH_FAILED | motion / P1 | 持续 | 物镜/调焦切换失败；对应轴后续切换及位置校验通过 |
| OBJECTIVE_SWITCH_UNSAFE_POSITION | motion / P0 | 持续 | 切换前调焦轴不在安全位；后续安全位置检查通过 |
| HANDOFF_MOVE_FAILED | motion / P1 | 持续 | 移动到交接点失败；后续交接点移动成功 |
| STAGE_RECIPROCATION_STOPPED | motion / INFO | 历史 | 位移台往复流程结束；不等同于整机安全停机证明 |

P0 必须优先按现场设备规程处理；不要为了让报警消失而盲目再次发运动命令。上述恢复依据是软件如何记录状态，不是现场处置操作规程。

当前有一处明确的恢复覆盖限制：Modbus 停止失败可以来自不同从站，但 `MOTION_STOP_FAILED` 的恢复调用仅位于 XY 位移台静止验证路径。其他轴若产生这条报警，目前缺少对应的自动恢复调用；重启也不会替它清除。要覆盖这类情形，需要补齐相应轴的真实静止验证及恢复逻辑，而不是直接删除报警。

### 任务、视觉、配置和文件（14 个）

| code | category / 级别 | 形式 | 场景及恢复依据 |
| --- | --- | --- | --- |
| TASK_FAILED | task / P1 | 历史 | 一次任务失败；后续任务成功不改写该次失败 |
| TASK_INTERRUPTED | task / P1 | 历史 | 服务中断导致一次任务未正常结束 |
| TASK_CANCELED | task / INFO | 历史 | 正常取消任务 |
| TASK_WORKER_UNAVAILABLE | runtime / P1 | 持续 | 任务执行线程不可用；监测重新观察到线程存活 |
| TASK_RECORD_READ_FAILED | storage / P1 | 持续 | 任务记录读失败或格式异常；同一文件后续读取成功 |
| RESULT_READ_FAILED | storage / P1 | 持续 | 结果文件读失败或格式异常；同一文件后续读取成功 |
| STORAGE_WRITE_FAILED | storage / P1 | 持续 | 数据写失败；同一文件后续写入成功 |
| CONFIG_EXECUTION_BLOCKED | configuration / P1 | 两种 | 配置预检边界使用持续状态，下次相同预检成功恢复；任务异常兜底也可能作为历史事实记录 |
| VISION_MODEL_FAILED | vision / P1 | 持续 | 模型加载/运行失败；相同模型资源后续实际推理成功 |
| VISION_INFERENCE_FAILED | vision / P1 | 持续 | 推理失败或结果异常；后续实际推理成功 |
| VISION_INPUT_QUALITY_FAILED | vision / P1 | 持续 | 图像质量不合格；后续实际推理成功 |
| VISION_GPU_DEGRADED | vision / P2 | 持续 | 原本使用 GPU 的计算回退 CPU；实际结果再次表明使用 CUDA 后恢复，CPU 推理成功不够 |
| COMPENSATION_OUT_OF_TOLERANCE | vision / P1 | 持续 | 补偿结束仍超差；后续补偿结果进入容差 |
| API_UNHANDLED_ERROR | runtime / P1 | 历史 | 一次接口发生未处理内部异常；按诊断编号查日志 |

同一文件的恢复匹配使用路径摘要。修复目录权限后，只成功写入另一个新文件，不能消除旧文件的写失败报警；历史文件被删除后，也不会因“文件不再被读取”自动恢复。

### 报警系统自身与磁盘（6 个）

| code | category / 级别 | 形式 | 场景及恢复依据 |
| --- | --- | --- | --- |
| UNKNOWN_ERROR | system / P1 | 兜底 | 未注册代码统一映射到此项，形式由调用点决定；不能假定存在通用自动恢复探测 |
| ALARM_RECORDING_INTERRUPTED | storage / P1 | 历史 | 记录器恢复后补记“之前记录中断，可能有事件缺失”；不能补回全部瞬时故障 |
| ALARM_MONITOR_FAILED | system / P1 | 持续 | 监测采样或线程启动失败；后续完整采样成功恢复 |
| ALARM_DISK_CONFIG_INVALID | system / P2 | 持续 | 磁盘阈值配置无效，回退 20% / 10%；修正环境变量并重启后恢复 |
| DISK_SPACE_LOW | storage / P2 | 持续 | 剩余比例低于 warning，默认 20%；下一次采样达到或高于该阈值恢复 |
| DISK_SPACE_CRITICAL | storage / P1 | 持续 | 剩余比例低于 critical，默认 10%；下一次采样达到或高于该阈值恢复 |

磁盘低于 10% 时，两个容量报警会同时活动。回升到 15% 时只恢复 CRITICAL，LOW 仍活动；达到 20% 才都恢复。监测默认约每 5 秒采样，磁盘阈值只是报警条件，不新增停产控制。

`TASK_WORKER_UNAVAILABLE` 在系统目录和任务目录均定义过，最终合并以任务目录为准，因此实际 `category=runtime`，全量代码去重后为 51 个。

## 4. 查询接口自身失败时的返回

设备故障与 HTTP 调用失败要分开处理。即使设备存在 P0，查询成功仍返回 HTTP 200，故障放在 `items`。HTTP 非 200 说明本次查询失败，不能把它当作空报警。

一般错误格式：

```json
{
  "detail": {
    "error_code": "ALARM_UNAUTHORIZED",
    "message": "报警查询认证失败"
  }
}
```

| HTTP | detail.error_code | 典型触发 | 处理 |
| --- | --- | --- | --- |
| 400 | ALARM_QUERY_INVALID | mode 非法、额外筛选参数或重复参数 | 修正请求，保留已有状态和游标 |
| 400 | ALARM_CURSOR_INVALID | 游标损坏或用错模式 | 检查游标的保存、传输和 mode |
| 401 | ALARM_UNAUTHORIZED | Bearer 凭据缺失或不匹配 | 修正客户端凭据 |
| 405 | HTTP_405 | 对该路由发 POST 等非 GET 请求 | 改为 GET；不存在清除/确认操作 |
| 410 | ALARM_CURSOR_EXPIRED | 历史过期、快照过期、流变化或序号回退 | 按原因重新同步 |
| 422 | REQUEST_VALIDATION_FAILED | limit 超界或不是整数 | 修正 limit |
| 503 | ALARM_AUTH_NOT_CONFIGURED | 服务未设置 COLONY_ALARM_TOKEN | 配置服务环境，通常需要重启使环境生效 |
| 503 | ALARM_SERVICE_UNAVAILABLE | 报警服务未初始化成功 | 查初始化日志，恢复服务 |
| 503 | ALARM_STORE_UNAVAILABLE | 报警数据库未就绪或不可读 | 恢复存储并重试 |
| 500 | INTERNAL_SERVER_ERROR | 未被处理的服务器内部异常 | 保留状态，用请求诊断编号查日志 |

当前应用验证错误采用上表中的统一 `detail` 对象，并非 FastAPI 默认的错误数组。网关还可能返回 403/502/504 或非 JSON；网络断连也可能没有任何 HTTP 响应，客户端需要统一保留最后已知状态。

410 示例：

```json
{
  "detail": {
    "error_code": "ALARM_CURSOR_EXPIRED",
    "message": "报警同步位置已失效，请重新同步",
    "resync_reason": "retention_expired",
    "stream_id": "stream-example",
    "retention_start_sequence": 200
  }
}
```

`resync_reason` 可能为 `retention_expired`、`stream_changed`、`sequence_rolled_back` 或 `snapshot_expired`。快照过期只需要重新取完整快照，不应仅凭它断言历史丢失。

## 5. 正确轮询与看板消警

1. 首次接入不传游标拉取 `snapshot`，拉完所有页后一次性应用，持久化快照 `watermark`。分页未完成时保留旧状态。
2. 不传游标拉取 `events`，回放全部仍保留的历史。序号不大于快照 watermark 的旧事件只写历史，不覆盖快照当前状态。快照到回放之间如果已经有事件过期，按参考客户端逻辑记录缺口并重新同步。
3. 事务内保存本页事件、按 event_id 去重、更新状态和 next_cursor；提交成功后才推进游标。
4. `has_more=true` 立即继续下一页；全部取完后再等下一轮，建议每 5 秒一轮、请求超时 3 秒、不重叠执行。
5. 对 `occurred/updated` 更新活动报警；对 `recovered` 将对应 alarm_id 标记恢复；对 `recorded` 保存历史，并根据严重程度决定是否通知。历史 P1 也需要关注，不能只订阅 active=true。
6. 空事件页仍更新 monitoring。查询失败不清空报警，不推进游标。连续超过 60 秒无有效响应，由业务后端产生通信异常提醒；这不证明电机已停。

工控机的历史事件默认保留 30 天，未恢复状态不随历史清理。快照最多有效 10 分钟，进程重启也会让未取完的快照失效；正常重启沿用原报警数据库时，事件流及活动报警保留，仅 boot_id 更新。

已有可运行的参考客户端：[tools/poll_alarms.py](../tools/poll_alarms.py)。完整同步约定见 [alarm-api.md](alarm-api.md)。

“消除报警”应区分三个动作：

| 动作 | 当前实现 |
| --- | --- |
| 排除设备故障 | 处理连接、空间、配置、设备状态等根因；后续对应能力验证成功产生 recovered |
| 人工确认/关闭通知 | 由业务后端保存确认人、时间和备注；可以停止提醒，但不修改设备 active 状态 |
| 删除已发生历史 | 不作为消警手段；历史按保留期管理，不能抹掉任务失败或产物损失事实 |

当前没有 `POST /api/alarms/clear`、ACK 或统一人工复位接口，轮询 GET 也不触发硬件复测。设备空闲时，部分报警会一直保留到下一次实际使用成功；不能理解成“维修后还显示报警，所以代码必须修改”。另一方面，某些不再访问的文件或缺少匹配验证路径的资源，可能无法自动消警，这是当前实现的边界。

## 6. 是否必须停服务、改代码、再重启

| 情况 | 通常的处理方式 | API 服务是否必须重启 |
| --- | --- | --- |
| 磁盘低容量 | 清理确认可删除的数据或扩容，等下一次采样 | 否 |
| 短暂相机/串口通信异常 | 处理连接/供电等原因，在设备允许的条件下验证对应采集或通信 | 通常不需要；若底层资源卡死可能需要 |
| 录像文件丢失或任务失败 | 保留历史、检查产物及原因，按业务重新发起采集/任务 | 不因历史记录本身要求重启 |
| 模型缺失、图像质量不合格 | 恢复模型/输入条件，执行实际推理验证 | 取决于配置加载和缓存，不能一概而论 |
| 运行配置错误 | 修正配置，再运行相同预检；是否需重启取决于配置的加载方式 | 启动时读取的配置通常需要 |
| 磁盘报警阈值环境变量错误 | 修正环境变量，重新启动服务加载 | 是，但无需为此改代码 |
| 任务线程或监测线程无法启动/已停止且未恢复 | 查线程/初始化日志；当前报警层不会自动重启它们 | 通常需运维恢复或重启 |
| P0：停止失败、位置不确定、硬限位/不安全位置 | 先按现场规程暂停相关生产动作并确认设备安全，再修复和验证 | 重启 API 不是安全复位，也不是充分恢复证据 |
| 确认是程序缺陷 | 定位、修复、验证后部署 | 按部署流程重启/更新 |

任务失败后，后续设备恢复不会使原任务自动续跑或变为成功。需要依据任务记录、已执行步骤和产物决定重做范围，再显式提交任务，避免重复生产动作。

本次改动还明确收紧了物镜切换成功判定：位置超出允许误差时会中止切换，不能仅视作新增一条提示。报警记录层本身没有新增全局停产开关，但业务检查失败仍会影响当前操作。

恢复生产应基于实际故障处置和设备验证；不以删除报警数据库、隐藏看板条目或反复重启作为恢复依据。

## 7. 示例文件说明与核验

[examples/alarm-polling.json](examples/alarm-polling.json) 是合成场景集合，外层包装不是 API 响应；每个场景的 `response` 才是对应响应正文。它使用当前报警存储实现和临时数据库生成，包含全量代码及生命周期示例，不代表真实设备曾同时出现这些故障。示例游标属于临时数据库，不能用于生产接口。

本次核对运行了报警 API、存储、服务和轮询客户端现有测试，共 56 项通过；未操作真实硬件。实现依据：`workflow/api_server.py`、`alarm_store.py`、`alarm_service.py`、`alarm_device_codes.py`、`alarm_task_codes.py` 及各模块报警调用点。

# 切站最小间隔：修改、部署和实机验证

2026-10-26 更新：当前源码的观察扫描入口在省略 `task.motion.slave_switch_gap_ms` 时默认使用 **2 ms**；显式填 `0` 可关闭，填其他合法数字可覆盖。仅修改扫描入口的缺省值，底层通用客户端、直接调用的运动函数和对焦流程仍保留原默认值。下面列出的 2026-10-23 ZIP 是历史发布包，**不包含这次默认值修改**；须部署更新后的 `workflow/scan_executor.py` 并重启服务后才生效。测试请求省略此字段后，应检查结果 `capture_result.slave_switch_gap_ms` 为 `2.0`。

现场后续检查仅发现 `tests/test_stage_diagnostics_summary.py` 基线不符；这不说明该文件一定缺失，也不影响运行模块的已通过校验。为避免部署依赖现场测试目录，已从原包派生 `rtu-switch-gap-2267472-20261023-runtime.zip`：只含6个运行/汇总代码文件，所有这6个文件的旧版哈希、新版哈希和目标内容与原包完全一致，保留完整基线检查。测试及说明文档不在此包的部署范围，现场对应文件不改动。此工控机后续请使用 `runtime` 包，并将下文 `PACKAGE` 文件名相应替换；同样先 `check` 成功才 `apply`，无需改部署工具或任务 JSON。原包的这次 `check` 未写入文件，不需要先回退。

本轮基线为逐点停稳拍照提交 `2267472e2644f781784854b59fe39f642d649a4a`。2026-10-22 的静止测试报告中，`modbus.py` 和 `communication_timing.py` 哈希与该基线相符；其他包内文件仍须由现场 `check` 逐个确认。包是明确选文件的未提交工作区快照，`target_commit=null`，使用 `target_snapshot` 和 ZIP SHA-256 标识内容。没有切换本地分支或提交之前的未提交工作。

## 改动与参数

在原请求的 `task.motion` 内添加数字字段，单位为毫秒：

```json
"motion": {
  "slave_switch_gap_ms": 2,
  "stop_shoot": {
    "enabled": true,
    "reuse_connection": true,
    "reuse_profile": true,
    "hold_enabled": false
  }
}
```

若原来已经有 `motion` 对象，把新字段合并进去，保留原串口、速度、加减速度等参数。不要创建第二个同名对象，也不要把该数字放进 `stop_shoot`。当前扫描入口省略时默认 `2`，显式填 `0` 时不增加切站等待；接受有限数字 `0..20`，拒绝字符串、布尔值、null、负数和非有限值。

- `devices/motion/modbus.py`：读和写共用的 `_transact` 入口按上一事务结束时间计算；仅从站改变时补足不足的间隔。同站、首次请求、已有足够等待时不补等。客户端内锁覆盖等待和事务，扫描会话仍独占串口。
- 原有50ms重试等待、业务层50ms/20ms等待及超时设置保留。新等待不依赖是否开启取证；同时保证通信异常引起的参数缓存失效也不依赖取证。
- `scan_executor` → `stage_executor` → `ScanMotionSession`/独立客户端均透传配置。它与 `stop_shoot.enabled` 独立，同一会话内不允许改变间隔。
- 观察扫描默认2ms，通过参数传给运动函数和客户端；通用客户端默认仍为0ms，对焦和物镜流程没有启用新间隔。保持逐点到位、停稳后拍照，未改速度、标定、到位判定或相机参数。
- `communication` 新增 `switch_guard_count`、`switch_guard_requested_ms`、`switch_guard_wait_ms` 和 `configured_slave_switch_gaps_ms`。事务与重试耗时不包含新补等待，`move_total`及各运动步骤的墙钟耗时包含它，不要重复相加。
- 结果中的 `capture_result.slave_switch_gap_ms`、各 `point_timings[].slave_switch_gap_ms`、各 `motion_result.motion_params.slave_switch_gap_ms` 可核对生效值。失败结果也保留扫描配置。

## 工控机部署

上传发布目录里的 `code_update.py`、`rtu-switch-gap-2267472-20261023.zip` 和需要测试的任务 JSON 到 `/root/colony-releases/`。新版部署工具仍支持此前包和备份记录。工控机无需 Git；包不包含现场配置、标定、图像、相机SDK或虚拟环境。

先设置路径并校验（以下都是 Linux Bash 命令）：

```bash
PROJECT=/opt/colony_system
SERVICE=colony-system-api
PYTHON="$PROJECT/.venv/bin/python"
UPDATER=/root/colony-releases/code_update.py
PACKAGE=/root/colony-releases/rtu-switch-gap-2267472-20261023.zip

sha256sum "$PACKAGE"
"$PYTHON" "$UPDATER" check --root "$PROJECT" --package "$PACKAGE"
```

SHA-256 应与发布目录 `SHA256SUMS.txt` 一致。只有 `"check": "passed"` 才继续。若基线冲突，保留报错并取回对应文件核对，不要删除文件或强制覆盖以绕过校验。

等设备空闲、所有观察/对焦任务结束后，逐步执行：

```bash
systemctl stop "$SERVICE"
fuser -v /dev/ttyUSB0
"$PYTHON" "$UPDATER" apply --root "$PROJECT" --package "$PACKAGE" --service "$SERVICE"
```

`fuser` 预期无输出；仍有占用时先处理占用。只有 `"status": "applied"` 才进入验证；记录其更新 ID。编译检查不执行运动：

```bash
cd "$PROJECT"
"$PYTHON" -m py_compile devices/motion/modbus.py devices/motion/communication_timing.py workflow/scan_executor.py workflow/scan_motion_session.py workflow/stage_executor.py tools/summarize_stage_diagnostics.py
"$PYTHON" -m workflow.config_validator
```

检查成功再启动：

```bash
systemctl start "$SERVICE"
systemctl --no-pager --full status "$SERVICE"
curl --fail http://127.0.0.1:8000/health
```

没有改 systemd 配置，不需要 `daemon-reload`。健康接口成功只证明 API 启动，仍需采集验收。

## 六孔板 A2 对照

发布目录提供 `capture_6A2_gap0_001.json`、`capture_6A2_gap2_001.json`、`capture_6A2_gap5_001.json`。三者均启用相同的逐点会话优化、关闭使能保持，仅间隔、任务ID和输出路径不同。请先核对它们与原任务的其他参数一致；已有自定义相机/对焦/运动设置时，以原请求为准只合并间隔字段。

首次运行2ms任务（会实际移动和拍照，确认设备已准备好再提交）：

```bash
curl --fail --show-error -X POST http://127.0.0.1:8000/api/tasks/execute \
  -H 'Content-Type: application/json' \
  --data-binary @/root/colony-releases/capture_6A2_gap2_001.json
```

任务完成后，取回 `/opt/colony_system/data/interface_tasks/mov_6A2_gap2_001/result.json`。不要同时运行多个任务，也不要把 HTTP 202 当作采集成功。需要重复测试时同步修改任务ID、保存目录和结果路径末尾的编号，避免混淆历史数据。

```bash
"$PYTHON" "$PROJECT/tools/summarize_stage_diagnostics.py" \
  "$PROJECT/data/interface_tasks/mov_6A2_gap2_001/result.json"
```

先确认生效值2、图像数量/清晰度/定位正常、没有新增失败，再做同配置0与2ms交错对照，各至少3次；若2ms仍频繁重试，再测5ms。保持同一孔、采样路线、对焦策略、速度、曝光和稳定等待。之前 `stop_shoot.enabled=false` 的结果不能直接作为“只改变间隔”的对照。

重点比较 `move_total_ms`、任务总耗时、`retry_count`、`retry_overhead_ms`、`failed_transaction_count`、`switch_guard_wait_ms` 和异常/缺图。汇总脚本对旧结果没有guard字段时报告未知，不会伪报为零。静止测试有效不等于真实写入/运动负载已经验证。

## 关闭与回退

仅关闭新等待：下一个任务填 `"slave_switch_gap_ms": 0`，无需重启；这不会撤销其他代码修改。

需要完整回退本次代码时，先结束任务并确认设备空闲，再执行：

```bash
systemctl stop "$SERVICE"
"$PYTHON" "$UPDATER" status --root "$PROJECT"
"$PYTHON" "$UPDATER" rollback --root "$PROJECT" --service "$SERVICE"
```

确认最近一条未回退记录是本次快照；`rollback` 每次只撤销最近一包，不要连续执行。输出 `"status": "rolled_back"` 后再启动并检查 `/health`。它恢复本次更新前现场文件，删除本次新增文件，保留之前的停稳拍照优化。不要删除 `.code-updates` 备份目录，也无需拿main覆盖。

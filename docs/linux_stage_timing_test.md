# Linux 工控机载台耗时取证

本地分支 `diag/linux-stage-timing`，基于 `main` 的 `d2982bf`。
本轮仅记录时间、计数和现有响应，不改变运动参数、重试策略、控制字、等待或寄存器访问顺序。
不新增跨从站间隔，不额外读取硬件状态。统计在内存累积，随原结果落盘；计时会有少量软件开销。
不要合并 Windows 调试分支或复制整个 config 目录到工控机。

## 同步前

工控机工程路径以下假设为 `/opt/colony_system`；服务名假设采用仓库提供的 `colony-system-api.service`。
先执行 `systemctl cat colony-system-api` 核对 WorkingDirectory 和 ExecStart，实际路径不同则替换。
如果是终端手动运行，使用原终端管理进程，不要另外启动第二套 API。
等待当前采集/录像/载台任务结束后再停止服务。

本地生成的 `outputs/linux-stage-timing/linux-stage-timing.patch` 仅包含以下 9 个文件：

```
devices/motion/communication_timing.py
devices/motion/modbus.py
workflow/stage_executor.py
workflow/scan_executor.py
tools/summarize_stage_diagnostics.py
tests/test_communication_timing.py
tests/test_observation_timing.py
tests/test_stage_diagnostics_summary.py
docs/linux_stage_timing_test.md
```

Windows PowerShell 上传补丁（把账号和地址换成实际值）：

```powershell
scp .\outputs\linux-stage-timing\linux-stage-timing.patch 用户名@工控机IP:/tmp/linux-stage-timing.patch
```

## Linux 应用补丁

逐条执行并检查结果，任何一条失败都先停止后续操作。

```bash
cd /opt/colony_system
git status --short
git rev-parse HEAD
systemctl cat colony-system-api
git switch -c diag/linux-stage-timing
git apply --stat /tmp/linux-stage-timing.patch
git apply --check /tmp/linux-stage-timing.patch
```

保留 `git rev-parse HEAD` 的输出，作为工控机原始版本记录。
如果上述文件已有现场修改，先单独备份并审查差异；配置文件有现场修改不需要恢复。
如果分支名已存在，换用带日期的新分支名。若补丁检查失败，不要强制覆盖或整分支合并；需要按现场代码适配。
确认设备空闲后：

```bash
sudo systemctl stop colony-system-api
git apply /tmp/linux-stage-timing.patch
git diff --check
.venv/bin/python -m py_compile devices/motion/communication_timing.py devices/motion/modbus.py workflow/stage_executor.py workflow/scan_executor.py tools/summarize_stage_diagnostics.py
.venv/bin/python -m workflow.config_validator
```

上面检查全部成功后启动原服务：

```bash
sudo systemctl start colony-system-api
sudo systemctl status colony-system-api --no-pager
curl --fail http://127.0.0.1:8000/health
```

启动失败时查看 `journalctl -u colony-system-api -n 80 --no-pager`。
此流程使用工控机现有 Python/MVS 环境，不升级依赖、不改变串口权限或现场配置。
只采用一个 worker，不加 `--reload`。

## 本地模拟验证

```powershell
& 'C:\miniforge3\envs\ca\python.exe' -m pytest -q tests/test_communication_timing.py tests/test_stage_diagnostics_summary.py tests/test_modbus_rtu.py tests/test_observation_timing.py tests/test_core_workflow.py tests/test_scan_camera_lifecycle.py tests/test_stage_reciprocation_accuracy.py tests/test_linux_deploy_defaults.py
```

测试模拟串口；验证诊断开关前后访问序列和重试等待相同、失败和取消时记录落盘、上下文复位及统计明细上限。

## 实机测试

沿用之前工控机成功的 48-well C4、4x、8 点任务请求。仅换 task_id 和对应输出路径，防止覆盖。
保持同一载台、起始位置、速度/加减速度、0.5 秒稳定等待、曝光、对焦策略以及 capture/detect 阶段。
不要将 Windows B2 的 15 点任务作为它的直接对照，也不要添加 Windows 分支的 slave_switch_gap_s 参数。
运行 3 次；如需测试正常使用时对焦的总耗时，另设一组明确触发对焦的任务。

本轮完成后取回每次 result.json，并记录工控机原始提交号和补丁 SHA256：

```bash
sha256sum /tmp/linux-stage-timing.patch
.venv/bin/python tools/summarize_stage_diagnostics.py /实际路径/result.json
```

多孔包装结果字段：`wells[].result.capture_result.point_timings[].communication`。
单孔结果字段：`capture_result.point_timings[].communication`。
扫描结果字段：`point_timings[].communication`。

- `mode` 应为 `passive_linux_baseline_v1`，且 `runtime.platform` 应为 `linux`。
- `transaction_count` / `attempt_count` / `retry_count` / `failed_transaction_count`：事务、尝试、重试、最终失败次数。
- `transaction_total_ms`：原 Modbus 调用及其重试处理总时间，包含下层库等待；不是纯线缆传输时间。
- `attempt_total_ms`：各次下层调用耗时之和，仍包含下层等待，不是驱动器独占响应时间。
- `retry_overhead_ms`：发生重试的事务，扣除最后一次成功尝试后的时间；最终失败则不扣除最后尝试。
- `groups`：按移动阶段、从站、功能和寄存器分组的次数、累计、最小和最大耗时。
- `slave_switch_count` 及三个 `*_retry_count`：切站与同站/首次调用的重试归类，反映相关性，不单独证明原因。
- `slow_or_retried`：每点最多 100 条慢（>=100ms）/重试/失败事务详情；超限只丢明细，计数完整。
- `lifecycle_ms.connect`：包含客户端构造、打开串口及原有连接后等待；`disconnect` 为关闭耗时。

通信时间包含在移动子步骤中，不得重复相加。连接/关闭是移动子步骤之外的另列统计。
统计不包含自动对焦、物镜切换或检测的通信。失败发生在首次事务前可能没有通信事务，但任务状态仍会显示失败。
旧结果没有 communication 字段表示未测量，不代表零重试。
本轮不能把机械运动和轮询通信完全分离，也不能量化 DLL/串口驱动内部的发送与接收时间。

## 回退

没有后续改动的前提下，设备空闲时用原补丁反向应用；不要使用 reset --hard 恢复现场配置。

```bash
cd /opt/colony_system
git apply -R --check /tmp/linux-stage-timing.patch
sudo systemctl stop colony-system-api
git apply -R /tmp/linux-stage-timing.patch
.venv/bin/python -m workflow.config_validator
sudo systemctl start colony-system-api
curl --fail http://127.0.0.1:8000/health
```

反向检查或配置校验失败时停止后续操作，先处理差异。测试结果保留在 data 下，不随回退删除。

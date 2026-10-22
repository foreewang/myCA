# 逐点停稳拍照优化（Linux）

本地分支 `perf/stop-shoot-scan`，保留前一轮取证，新增扫描会话优化。
未修改对焦算法、触发策略、搜索范围、相机曝光、运动速度、加减速度、标定、限位或稳定等待。
同一孔的扫描执行器独占会话；对焦前及末点移动结束后释放串口，不跨孔/任务共享。

## 默认启用的三项

1. 到位轮询每轮只读取一次两轴位置/状态；先检查状态读取失败及故障，再判断到位。其他独立移动调用保留原流程。
2. 同一段扫描复用串口；对焦仍在原首点时机运行，运行前串口已经关闭；对焦后重新连接。
3. 速度、加速度、减速度在同一连接内参数未变时不重复写入；目标位置每点必写。
   断开、异常、对焦交接、参数变更、模式写入、故障复位及取证范围内的通信失败会使相应缓存失效。

默认正常结束仍执行原停止流程。不会自动启用使能保持。
减少状态读取会改变轮询周期，所以不能把减少的通信累计时间直接等同于总任务节省量。

## 请求开关（放在原 task.motion 内）

同代码基线，使用原逐点开关串口和原参数写入流程：

```json
"stop_shoot": {"enabled": false}
```

第一轮优化组（也是省略 stop_shoot 时的默认）：

```json
"stop_shoot": {
  "enabled": true,
  "reuse_connection": true,
  "reuse_profile": true,
  "hold_enabled": false
}
```

所有开关必须是 JSON 布尔值，拒绝字符串、null 和未知键。
可分别把 reuse_connection 或 reuse_profile 设为 false 做分项对照。
连接未复用时，缓存不会跨连接生效。不要同时改稳定时间和速度。

## 实验性使能保持：单独一轮验证

仅在第一轮稳定且已经核对驱动器状态位含义后，再单独设置 `hold_enabled: true`。
此开关要求 enabled 和 reuse_connection 都为 true。

对于中间采样点，必须同时满足：

- 两轴位置在原到位容差内，状态读取成功且无故障；
- 两轴同时有运行使能位和目标到位位；
- 连续至少 3 组位置采样，覆盖至少 150 ms，各轴位置范围不超过 10 pulse；
- 保留原 settle_s，等待后再次检查状态和位置，相对稳定判定位置偏移不超过 10 pulse。

不满足则继续等待，到超时按原异常停止路径处理，不会降级为只看位置容差拍照。
该判据是可测试的保守门槛，不等同于经过实机验证的速度为零；若驱动器不提供适用的到位位，保持关闭。
换点前有故障/状态读取失败会中止。对焦前、末点、错误和取消时不保持使能。
软件停止仍调用原驱动器停止方法，本轮没有修改它的控制字语义或声称硬件急停能力。

## 实测应看到什么

沿用工控机 B2 15 点原任务，只改任务 ID/输出路径/上述开关。
保持对焦策略、初始位置、同一载台和孔板标定，基线/优化各至少 3 次。

- 无对焦的 15 点扫描，预期扫描串口连接/关闭各 1 次。
- 首点运行一次对焦时，扫描连接/关闭各 2 次；对焦自身连接不在这个统计里。
- 基线 write_targets 为每点 16 个单寄存器事务。
- 无对焦优化组：15 点 write_targets 共 72 次事务（首点 16 + 后续 14×4）。
- 首点对焦优化组：共 84 次（首点及第二点各 16 + 后续 13×4）；参数变更/缓存失效时会增多。
- 等待到位每轮 4 次读取，不再追加两次状态读取；实际轮数随运动/通信时序变化。
- 开启使能保持时，查看 captures[].motion_result.stop_shoot 的 held_enabled、standstill_verified、stable_samples。
- point_timings[].stop_shoot 和 capture_result.stop_shoot 记录实际配置，避免仅凭文件名分组。

验收：move_total 的中位数下降，没有新增失败、超时、定位异常、缺图或模糊；正常拍照前仍停稳。
对焦耗时单列，不将其变化算成载台优化收益。
失败/取消发生在移动外时，额外清理统计位于 capture_result.motion_session_cleanup，不计入某一点的 move_total。
使用 `tools/summarize_stage_diagnostics.py` 同时查看 lifecycle_counts、session_cleanup 和分阶段事务。

## 同步工控机

已应用上一轮取证补丁的工控机，使用 `outputs/stop-shoot-optimization/from-diagnostics.patch`。
未应用取证、仍是本地 main 基线的工控机，使用 `from-main.patch`。两个补丁二选一，不要依次应用。
都不包含现场配置和对焦模块。已有其他代码修改时必须先检查补丁，不强制覆盖。

PowerShell 上传（替换账号、IP）：

```powershell
scp .\outputs\stop-shoot-optimization\from-diagnostics.patch 用户名@工控机IP:/tmp/stop-shoot.patch
```

Linux（按实际服务名和工程路径替换；逐条执行，失败即停止后续操作）：

```bash
cd /opt/colony_system
git status --short
git rev-parse HEAD
systemctl cat colony-system-api
git switch -c perf/stop-shoot-scan
git apply --stat /tmp/stop-shoot.patch
git apply --check /tmp/stop-shoot.patch
```

设备空闲后停止原服务，再更新；手动启动的服务需在原终端停止，不要另开第二实例。

```bash
sudo systemctl stop colony-system-api
git apply /tmp/stop-shoot.patch
git diff --check
.venv/bin/python -m py_compile workflow/scan_motion_session.py workflow/stage_executor.py workflow/scan_executor.py devices/motion/modbus.py
.venv/bin/python -m workflow.config_validator
sudo systemctl start colony-system-api
curl --fail http://127.0.0.1:8000/health
```

保留原提交号及 `sha256sum /tmp/stop-shoot.patch`。本地模拟测试不能代替 Linux 实机验证。
本地回归命令：

```powershell
& 'C:\miniforge3\envs\ca\python.exe' -m pytest -q tests/test_stop_shoot_scan.py tests/test_communication_timing.py tests/test_stage_diagnostics_summary.py tests/test_modbus_rtu.py tests/test_observation_timing.py tests/test_core_workflow.py tests/test_scan_camera_lifecycle.py tests/test_stage_reciprocation_accuracy.py tests/test_linux_deploy_defaults.py tests/test_camera_autofocus_contract.py tests/test_plate_scan_geometry.py
```

## 回退

先用 `stop_shoot.enabled: false` 做同代码基线。
需要撤回补丁时，设备空闲，且没有后续交叠修改：

```bash
cd /opt/colony_system
git apply -R --check /tmp/stop-shoot.patch
sudo systemctl stop colony-system-api
git apply -R /tmp/stop-shoot.patch
.venv/bin/python -m workflow.config_validator
sudo systemctl start colony-system-api
```

反向检查失败则先处理差异，不能 reset --hard 覆盖现场配置。

# 补偿正常完成后保持使能

## 证据与修改范围

2026-10-31 10:44 的三次日志显示：X 在恢复使能后仍接近目标；发送停止
控制字 0x06 后第一次读取就负向偏离约 3.6～3.8 万脉冲，随后偏差继续增加。
目标寄存器始终为 4225601。第一轮停止前 X 位置为 4225602，且按当前代码
定义，状态字运行使能位、目标到达位均已置位。

这定位了软件触发环节：正常到位后进入停止状态。驱动器内部行为和实际机械
回退仍需实机对照确认，不能仅凭串口读数断言机械原因。

运行时只同步：

- `workflow/stage_executor.py`
- `workflow/compensate_executor.py`

当前改动：

1. 补偿路径调用 keep_enabled=True；普通扫描等其他调用保留原默认行为。
2. 停稳需要两轴在原容差内、状态字运行使能和到达目标位均有效；至少 3 次采样、
   持续至少 150ms，期间每轴位置范围不超过 10 脉冲。
3. 正常完成清除 PP 触发位、保留 0x0F，不再调用发送 0x06 的停止函数。
4. 保留原 settle_s，之后复核状态、目标误差及相对于停稳读数的变化（不超过 10 脉冲）。
5. 运动中的取消检查在开始、轮询、settle 后及成功返回前执行；异常仍停止两轴。
6. 固定方向接近的预移动和最终移动均使用保持策略；闭环补偿移动同样适用。

通用 quick_stop()、速度、加减速度、容差、限位均未调整。
补偿成功后电机保留运行使能；关闭串口连接不会主动发送停止命令。
这与扫描 session 退出时主动停止的生命周期不同，是本修复的预期行为。
本改动保证的是运动函数内的异常/取消处理，并未新增任务结束后的全局停机策略。

## 部署

设备空闲时，备份工控机现有两个文件，停止原服务，部署这两个文件。
不要用整个 Windows 工作区覆盖现场配置或其他模块。服务名不同时替换命令中的名称。

```bash
cd /opt/colony_system
.venv/bin/python -m py_compile workflow/stage_executor.py workflow/compensate_executor.py
sudo systemctl start colony-system-api
sudo systemctl status colony-system-api --no-pager
```

本地模拟验证：

```powershell
& 'C:\miniforge3\envs\ca\python.exe' -m pytest -q tests/test_compensate_finish_diagnostics.py tests/test_core_workflow.py tests/test_stop_shoot_scan.py tests/test_communication_timing.py
```

测试覆盖模拟停止后位移的回归、无停止写入的成功路径、稳定条件、恢复使能后
异常偏移、最终状态读取失败/故障、超时，以及运动中/恢复使能后的取消停止。
模拟测试不替代驱动器实机验收。

## 实机验收

沿用此前补偿目标和运动参数，保留诊断读取进行首次对照，并同步观察固定标记。

```bash
rg -n -C 3 'stage_finish_probe|stage_arrival_decision|stage_hold_complete|stage_after_finish|task_failed' logs/task.log
```

成功应满足：

- stage_arrival_decision 中 require_standstill=True。
- stage_finish_probe_start 中 keep_enabled=True。
- 正常路径有 after_restore_hold_enabled，没有 after_stop_return。
- after_settle 中控制字为 15（0x0F），状态字运行使能位仍有效。
- 最终位置在原容差内，有 stage_hold_complete，任务成功；图像/独立观察符合预期。
- 不能仅以“没有报错”为验收依据，应确认位置持续保持。

如果停稳或最终状态检查失败，保留完整任务日志；不要放宽阈值掩盖失败。
错误路径仍可能发送 0x06，应对照 task_failed 原因区分它与正常完成路径。

首次对照成功后，在实际服务进程环境中设置
COLONY_COMPENSATE_FINISH_DIAGNOSTICS=0 并重启，保持补偿修复启用，
再验证去掉额外读取后仍能成功。终端 export 不会自动改变 systemd 服务环境。

回退时设备空闲并停止服务，恢复本次部署前备份的两个文件，再编译、启动原服务。
不要全仓 reset 或替换现场 config。

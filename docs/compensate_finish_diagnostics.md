# 补偿结束阶段位置偏移取证

针对 2026-10-31 B2 补偿日志：X 在到位判断后读数减少约 80000 pulse。
初始版本只增加诊断。根据后续分阶段日志，当前补偿路径已改为停稳后保持使能，
正常完成不再发送 0x06；通用停止函数未改。修复验收见 `compensate_hold_fix.md`。

## 部署范围

运行时必须同步两个文件：

- `workflow/stage_executor.py`
- `workflow/compensate_executor.py`

先在工控机备份这两个现场文件，设备空闲时停止原服务，再部署。
保留现场其他代码和 config，不用整个工作区覆盖工控机。
服务若不是 `colony-system-api`，使用实际服务名。部署后执行：

```bash
cd /opt/colony_system
.venv/bin/python -m py_compile workflow/stage_executor.py workflow/compensate_executor.py
sudo systemctl start colony-system-api
sudo systemctl status colony-system-api --no-pager
```

补偿路径默认启用，包括开启固定方向接近时的预移动、最终移动，以及闭环补偿移动。
普通扫描不启用额外读取。已有两条到位/结束摘要日志保留。
若需关闭额外取证，在实际服务进程环境中设置
`COLONY_COMPENSATE_FINISH_DIAGNOSTICS=0` 并重启；仅在交互终端 export
不会自动影响 systemd 服务。恢复原有备份文件也可撤销此诊断版本。

## 取证内容

每次移动先写 `event=stage_finish_probe_start`，包含唯一 move_id、目标、
到位判断使用的原读数、速度、加减速度和 settle_s。
同一任务可能包含多次移动，用 task_id 与 move_id 共同关联。

原停止路径写 8 条 `event=stage_finish_probe data={...}`，顺序如下：

1. `arrival_before_restore`：等待函数返回后，依次读取 X、Y；尚未执行结束阶段的恢复使能。
2. X `after_restore_before_stop`：X 恢复使能之后、调用停止之前。
3. X `after_stop_return`：X 停止函数返回后。
4. Y `after_restore_before_stop`：Y 恢复使能之后、调用停止之前。
5. Y `after_stop_return`：Y 停止函数返回后。
6. `after_settle`：原稳定等待和原最终位置快照之后，依次读取 X、Y；在最终到位校验之前落日志。

当前补偿保持使能路径写 6 条：X/Y `arrival_before_restore`、
X/Y `after_restore_hold_enabled`、X/Y `after_settle`。
正常完成不再出现 `after_stop_return`，start 事件中的 keep_enabled 应为 True。

每条样本按顺序读取：

| 字段 | 寄存器 | 含义 |
| --- | --- | --- |
| current_pos | 968/969 | 当前实际位置 |
| statusword | 897 | 状态字，十进制 |
| cmd_pos | 966/967 | 指令位置，不等于下发目标寄存器 |
| target_register | 999/1000 | 驱动器目标位置寄存器 |
| controlword | 896 | 控制字读回值，十进制 |

`requested_target` 是软件目标。`start_ms`、`end_ms`、`read_windows_ms`
是相对本次 finish_probe_start 附近起点的单调时钟毫秒数。
它们用于区分样本内各字段的采样时间；各字段和两轴不是同时读取。
`arrival_before_restore` 的新读数也不是原到位判断读数，原值在 start 事件与旧摘要中。

## 时序影响和失败处理

- 原停止路径新增 40 次逻辑读取事务；保持使能路径新增 30 次（每个样本 5 次），底层可能重试。
- 诊断本身不增加控制写入或 sleep；修复后的补偿路径另外启用了停稳等待，保留原 settle_s。
- `after_stop_return` 是停止函数返回后，已经包含原函数的 50ms 等待，
  不是停止控制字发送瞬间。不能用它精确断言位移发生于某一毫秒。
- 读取/重试和日志会推迟后续控制，可能改变复现概率；本版本不是无扰动测量。
  若故障不再出现，也不能据此认为已修复。
- 单个样本读取失败会记录 errors、保留已读字段并跳过该样本剩余读取；
  不因诊断读失败抛出异常，也不阻止原停止流程。缺失字段表示未测得，不表示零。
- 到位校验仍使用原有 after 快照；不会使用新增样本代替或放宽校验。
- 原到位校验失败时，新增样本已经写入 task.log，原异常清理仍执行。

## 测试与判断

沿用原补偿目标、运动参数和起始条件做一次受控复现，观察固定标记或驱动器
独立位置监视。不要为了记录画面而同时启动另一个串口控制进程。

```bash
rg -n -C 3 'stage_finish_probe|stage_arrival_decision|stage_after_finish|arrival error is too large' logs/task.log
```

请保留整个任务的原始日志，包括可能出现的通信重试；不能只截最后一个异常。

- arrival_before_restore 已偏移：变化发生在原到位采样至新采样之间，优先检查未停稳/反馈。
- after_restore_before_stop 首次偏移：重点检查恢复使能步骤及该区间的运动/反馈。
- after_stop_return 首次偏移：重点检查停止步骤及该区间的运动/反馈。
- 只有 after_settle 偏移：重点检查停止后的延迟变化。
- target_register 与 requested_target 不同：进一步核查目标寄存器变化与写入来源。
- cmd_pos 随 current_pos 变化不等于软件重新写了目标，必须对照 target_register。

上述仅定位变化区间；真实机械位移仍需独立观察确认，不能仅凭同一串口读数证明。
验证保持使能修复后，关闭额外读取再复测，确认结果不依赖诊断带来的延时。

# 观察耗时优化：跨从站间隔对照测试

## 分支和本轮范围

从 `main` 的 `d2982bf` 创建分支 `perf/observation-modbus-gap`。
本地已执行 `git switch -c perf/observation-modbus-gap`，无需再次创建。
可用 `git branch --show-current` 和 `git diff` 查看分支及修改。

本轮仅优化扫描 XY 移动期间的 Modbus 事务：切换从站时，距离上一尝试完成时间不足
最小间隔则补足等待。同从站、首次请求及已有足够间隔的请求不增加等待。
默认 10 ms；保留已有的 20/50 ms 控制间隔、通信超时、重试次数、位置容差、
正常结束控制字及异常停止流程。补偿、独立往复测试、物镜切换和对焦不启用该策略。
控制范围是每次扫描点的 `move_to_absolute` 调用，默认不新增硬件读请求。

上一版详细状态取证模块不在当前 main 中。本轮新增的是通信汇总及慢/重试事务明细，
不额外读取结束状态快照。因此请用本分支的 0 ms 与 10 ms 作公平对照，不能直接把
9 月含额外快照的取证任务当作相同条件的基线。

## 自动化测试

在项目根目录，用已安装依赖的 Python 环境运行：

```powershell
C:\miniforge3\envs\ca\python.exe -m pytest -q tests/test_modbus_slave_gap.py tests/test_modbus_rtu.py tests/test_observation_timing.py tests/test_summarize_observation_timing.py tests/test_core_workflow.py tests/test_scan_camera_lifecycle.py tests/test_stage_reciprocation_accuracy.py tests/test_linux_deploy_defaults.py
```

测试使用模拟串口与时钟，不会驱动硬件。覆盖读写跨轴、同轴、已有等待、0/5/10 ms、
重试、失败后的时序、断开连接状态清理、上下文隔离、参数校验、异常结果落盘及汇总。

## 更新工控机

先检查并提交本分支变更，推送到你正常使用的远端，再在工控机获取并切换该分支。
这些 Git 远端和部署步骤没有由助手代为执行。提交前确认新文件也包含在提交中：

```text
devices/motion/modbus.py
devices/motion/transaction_metrics.py
workflow/platform_defaults.py
workflow/scan_executor.py
tools/summarize_observation_timing.py
tests/test_modbus_slave_gap.py
tests/test_observation_timing.py
tests/test_summarize_observation_timing.py
docs/observation_optimization_test.md
```

服务空闲时按现有部署方式重启 API。不要把本地载台标定配置覆盖到另一套硬件。
记录部署的 Git 提交号；未提交代码则保留差异副本，以确保知道测试了哪个版本。

## 实机 A/B 步骤

保持同一载台、驱动器、导程、孔位、起始位置、扫描点数、任务阶段、曝光、速度、
加减速度、`scan.settle_s` 和到位容差。不要同时改变重试、轮询周期或使能策略。
一次只执行一个硬件任务，不用其他程序同时访问串口。

在原请求的 `task.motion` 中加入下面一个字段，保留其他字段：

```json
"slave_switch_gap_s": 0
```

这是同版本基线。优化组改为：

```json
"slave_switch_gap_s": 0.01
```

若优化组效果稳定，再单独测试 `0.005`。参数为秒，允许有限数值 `[0, 0.1]`；
省略时扫描移动默认 `0.01`，布尔值、字符串、null、NaN 和无穷值会被拒绝。
无需更改相机或导程。0 ms 只关闭新增间隔，仍保留原来的命令等待和重试策略。

建议至少交替执行 A/B 各 3 次，每次使用唯一任务 ID 和输出目录，避免覆盖结果。
按同一定位流程恢复起始位置；若做不到，额外比较排除首点后的移动均值，但不能认为
这已消除负载、振动或温度等其他差异。

## 读取结果

每点新字段：`scan_result.json` 的 `point_timings[].communication`。
单孔总结果位于 `capture_result.point_timings[]`；多孔位于
`wells[].result.capture_result.point_timings[]`。失败时也保留已完成事务的汇总。

关键字段：

| 字段 | 含义 |
| --- | --- |
| `slave_switch_gap_s` | 本次实际使用的最小间隔 |
| `transaction_count` / `attempt_count` | 逻辑事务数 / 实际请求尝试数 |
| `retry_count` | 首次以外的尝试次数 |
| `failed_transaction_count` | 重试后仍失败或抛异常的事务数 |
| `slave_switch_count` | 同一连接内相邻事务切换从站的次数 |
| `gap_wait_count` / `gap_wait_ms` | 新增补足等待次数 / 实测累计毫秒 |
| `transaction_total_ms` | 所有事务耗时，包含补足等待、尝试及重试处理 |
| `retry_overhead_ms` | 有重试事务中，扣除新增补足等待及最终成功尝试后的时间；最终失败则不扣除最后尝试 |
| `slow_or_retried` | 超过 100 ms、有重试或失败的事务，含从站、寄存器、写入值和尝试耗时/错误 |
| `dropped_details` | 每点最多保存 200 条明细，超过后继续汇总并记录丢弃数量 |

这些耗时包含在 `move_total` 内，不要重复相加；运动中重试等待会与机械运动重叠，
不能将重试等待减少量直接视作任务节省量。记录只在内存累计，随原有结果写盘。
原步骤的计时包含其调用产生的补足等待。

运行只读汇总工具：

```text
python tools/summarize_observation_timing.py baseline_1.json optimized_1.json
```

支持单孔、多孔及扫描结果。旧文件没有本轮通信统计时，输出 unknown 对应的 null，
不会把未测量解释成零重试。对比 `move_total_ms`、`nonfirst_move_mean_ms`、重试次数、
新增等待、失败数、到位误差，以及实际图片清晰度和完整性。

## 验收与回退

- 同等条件下重试次数明显下降，并且总观察耗时/移动耗时中位数下降。
- 没有新增最终通信失败、定位异常、缺图或清晰度下降。
- 如果重试减少但总耗时未降低，检查补足等待与机械运动耗时，不据此直接增加速度。
- 如无改善或有回归，设置 `slave_switch_gap_s: 0`，保留数据后再排查总线和驱动器。

自动化测试只能验证控制流程和计时逻辑；是否真正提速须以这轮实机 A/B 数据为准。
通过后再另做使能保持和参数缓存，避免本轮混入其他行为改变。

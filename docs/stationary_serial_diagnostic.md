# 静止、只读串口对照测试

用途：区分单站通信异常与 X/Y 切站相关异常，测量增加切站间隔后，减少的重试耗时能否抵消新增等待。测试本身不判断转换器或驱动器已经损坏。

## 准备与运行

1. 等观察、对焦等任务正常结束，确认载台静止。保持原来的 USB、供电、线缆连接；测试期间不要操作载台、启动 API 或调试软件。
2. 将本地 `tools/diagnose_stage_serial.py` 上传到工控机 `/root/colony-releases/diagnose_stage_serial.py`。只需上传这个文件，不需要覆盖项目代码、升级依赖或使用更新包。
3. 工控机以 root 执行以下命令，先停止服务，再检查串口占用：

```bash
systemctl stop colony-system-api
systemctl is-active colony-system-api
fuser -v /dev/ttyUSB0
```

预期服务显示 `inactive`，`fuser` 无输出。`is-active` 在服务停止时返回非零是正常的。若有占用，先核对占用进程，不要直接强杀。

4. 执行测试：

```bash
REPORT="/root/colony-releases/serial_readonly_$(date +%Y%m%d_%H%M%S).json"

/opt/colony_system/.venv/bin/python \
  /root/colony-releases/diagnose_stage_serial.py \
  --root /opt/colony_system \
  --port /dev/ttyUSB0 \
  --baudrate 115200 \
  --x-slave 1 --y-slave 2 \
  --reads 200 --rounds 2 --case-seconds 20 \
  --output "$REPORT"

echo "$REPORT"
```

使用现场项目的 PyModbus 3.6.9 和现有通信封装；超时、重试次数、重试等待保持该项目代码的设置，并记入报告。若出现版本或服务检查错误，先保留报错，不要为运行测试升级依赖。

默认共 16 组，最多 3200 次读取事务（重试不计入这个次数）。每组到达 200 次读取或约 20 秒即结束；正在执行的事务会完成后退出，故 20 秒不是硬性截止时间。每组打印进度并保存 JSON；连续三次事务最终失败会提前停止。按一次 Ctrl+C 也会尝试关闭串口并保存部分结果。

5. 测试进程退出后恢复服务：

```bash
systemctl start colony-system-api
systemctl --no-pager --full status colony-system-api
```

脚本不会自动启动或停止服务，也不会修改服务配置。将 `REPORT` 指向的 JSON 下载并返回分析即可，不需要再跑观察任务才能获得本次诊断结果。

## 对照分组

| 第一轮组名 | 请求顺序 | 切站额外最小间隔 |
| --- | --- | --- |
| `x_only` | 连续读 X 位置 | 0 ms |
| `y_only` | 连续读 Y 位置 | 0 ms |
| `snapshot_xy_0ms` | X 位置、X 状态、Y 位置、Y 状态循环 | 0 ms |
| `alternate_0ms_before` | X 位置、Y 位置循环 | 0 ms |
| `alternate_2ms` | 同上 | 2 ms |
| `alternate_5ms` | 同上 | 5 ms |
| `alternate_10ms` | 同上 | 10 ms |
| `alternate_0ms_after` | 同上 | 0 ms |

第二轮交换单站组顺序，并反转间隔组为 10、5、2 ms。两轮都在间隔组前后安排 0 ms 复测，以检查错误是否只是随时间减少。每组重新建立一次连接，所有初始读数均保留，没有被排除的预热段。

仅允许站号 1、2（或明确指定的两个站号）的功能码 03：位置寄存器 `968`、数量 `2`；状态字 `897`、数量 `1`。脚本不发送使能、停机、复位或运动命令，不操作相机。停止服务本身不证明机械静止，因此第一步仍需确认设备空闲。

这里的间隔从上一笔应用层读取结束开始计算，仅在换站时补足。0 ms 表示脚本不加等待；PyModbus、串口驱动仍有自身时序。它不是物理线上两帧之间的精确间隔。

## 结果怎么看

| 结果 | 支持的判断与下一步 |
| --- | --- |
| 单站基本正常，交替组反复重试，增加间隔在两轮均改善，0 ms 复测仍差 | 支持切站时序相关；比较包含额外等待的总耗时，选择最小有效间隔，再在观察任务验证 |
| 初始 0 ms 差，后面的所有组包括末尾 0 ms 都好 | 时间顺序、初始状态或重连效应仍有混淆，不能把改善归功于间隔 |
| 单站也频繁失败 | 问题不限于换站；按站号排查转换器、驱动器响应、线缆与供电等，尚不能仅凭此定责 |
| 全部组无重试 | 本次静止条件下未复现；不能宣布问题解决，下一步才是运动负载下的对照 |

重点字段：

- `case_elapsed_ms`：包含连接、读取、重试、补等待、关闭与取证开销的该组总耗时。组大小不同或 `time_limit` 时，应同时看每次完成读取的平均耗时和实际读数，不能直接比较总量。
- `communication.retry_count`：额外尝试次数；`retried_transactions` 才是发生重试的事务数量。最终失败另见 `failed_transaction_count`。
- `retries_per_1000_switches` 与 `retried_transactions_per_1000_switches`：每千次切站的额外尝试数、发生重试的事务数；单站使用 `retry_attempts_per_1000_reads` 和 `retried_transactions_per_1000_reads`。
- `directions`：分别查看 `1->2`、`2->1`，以及同站读数。
- `transport.counts.serial_open/serial_close`：包括库内部自动关闭与重新打开串口；不是 USB 物理插拔次数。
- `samples`：每次读取的间隔、耗时、重试、寄存器值；`positions` 汇总位置范围，用于检查测试期间是否仍有位置变化。
- `communication.slow_or_retried`、`transport_failures`：错误与收发明细分别限量保存，超出数量通过 `dropped_details`、`transport_failure_details_dropped` 标明。完整样本计数仍保留。

不要把通信、重试、底层收发与组总耗时相加，它们有包含关系。发生中断时，最后一笔未完成读取可能只在通信明细里，读取归一化指标以完成调用为分母。取证代码也会改变调用间隔，因此阴性结果需要结合真实观察任务判断。

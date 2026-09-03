# 硬件与安全

软件停止不能替代合格的硬件急停。底层 `quick_stop()` 实际发的是 Shutdown 控制字。换驱动器必须按新手册核控制字、状态字、寄存器和停止行为。

当前坐标、限位和交接点基线见 [XY 标定测试方案](xy_stage_calibration_test_plan.md)。

## 上机前

运动使用绝对脉冲。仓库没有自动回零。正式任务前必须：

- 由驱动器或现场流程保证零点有效
- `data/objective_state.json` 与真实物镜一致

更换电机、编码器、电子齿轮或驱动器后，旧的 `pulses_per_mm`、A1、物镜/焦点位置、handoff 点和软件限位都不能直接复用。

X/Y 导程不同时，`plates.yaml` 用 `{x: ..., y: ...}`。旧的两轴共用单值仍能读。

## 联调顺序

1. 厂家工具：急停、正负限位、站号、低速点动。
2. 程序只读状态字、模式和当前位置，不发运动。
3. 一次一轴：低速、小距离正反向，并测量实际距离。
4. 标定方向、脉冲/mm、绝对零点、机械行程和安全边距。
5. 再测 XY 协同、物镜、细准焦、单孔采集、补偿、handoff。
6. 最后才测整板和往复。

新电机不要一上来就用 HTTP 往复接口。

## 限位与运行保护

`config/plates.yaml`：

- `stage_limits`：扫描前检查计划点是否超出带余量的 X/Y 范围
- `runtime_guard`：运行中查疑似卡死、实际位移过小、到位误差过大

`abort_on_motion_failure=true` 时，异常会中止任务，并在失败结果里写下已完成图片数和错误信息。

handoff：

- 复用当前 `plate_type` 的 `stage_limits`，发指令前查硬限位和余量
- `settle_s`：到位后再等，振动稳定才通知机器人
- `arrival_tolerance_pulse`：误差超阈值立即失败，不进入机器人交互

切镜：焦点低于 `hardware.focus_axis.objective_switch_collision_limit_pos` 时不转物镜、不写状态。自动对焦搜索下沿不得越过该限位，这样切换前不必额外退焦。

## 相机保护

- `open()` 中途失败会释放已创建的 MVS 句柄
- SDK 初始化用进程级引用计数，一个控制器关闭不影响另一个
- 公共 SDK 入口加锁，降低并发抢句柄
- 录像线程未退出时拒绝强行停 MVS 录像
- 拍照和录像都校验 Mono8；录像写帧前校验 PixelFormat 和帧长度

## 自动对焦

- `run_task.py` 读 `config/autofocus.yaml`，生成 `autofocus_decision`
- 真正执行在 `scan_executor.py`：第一个扫描点 XY 到位并稳定后、第一张拍照前
- 默认：物镜发生切换后对焦
- `always_before_capture` 只让决策成立；实际频率仍由 `trigger.scope` 控制，默认每孔第一个扫描点一次
- 任务读取前会机器校验该 YAML
- 录像期间复用正在录像的相机，避免第三方模块独占打开
- 复用录像相机时，临时采样图在 `data/autofocus_recording_tmp`

未执行时的检查项见 [常见问题](troubleshooting.md)。

## 任意两点精度测试

`tools/stage_reciprocation_accuracy.py` 测已标定 A/B 点。一个周期是 `A -> B -> A`。输出：

- `moves.csv`：每次目标、运动前后位置、误差、耗时
- `summary.json`：pulse / mm 统计（均值、标准差、极差、最大绝对误差、RMSE）

默认 dry-run，不连硬件。下面的坐标只演示格式，必须换成当前设备、且在软件安全限位内的点：

```bash
python tools/stage_reciprocation_accuracy.py \
  --axis x \
  --plate-type 24-well \
  --point-a-x 1000000 --point-a-y 1000000 \
  --point-b-x 1200000 --point-b-y 1000000 \
  --cycles 20 \
  --warmup-cycles 2
```

确认端点、行程、从站和安全范围后，同一命令加 `--execute`。

- 测 X：两点 Y 相同
- 测 Y：两点 X 相同
- `--axis xy` 允许两轴都变
- 默认软件 X/电机1 `slave=1`，软件 Y/电机2 `slave=2`

该脚本统计的是驱动器回报位置。丝杆间隙和真实重复定位精度还要同步记量表、光栅或标定板。

固定孔位后台往复（B2–C4）见 [HTTP API](http-api.md)。

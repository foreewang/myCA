# 硬件与安全

软件停止不能替代合格的硬件急停。当前两个 Modbus 实现的 `quick_stop()` 名称与实际行为并不完全一致：代码写入的是 `CMD_SHUTDOWN=0x0006`，等待 50 ms 后只按“控制字写入成功”返回，并未轮询驱动器状态字，也没有用速度或连续位置读数证明机构已经停止。驱动器手册中的停止控制字、目标状态以及运行时停稳确认尚未闭环，这是正式上线的硬性现场阻断项；换驱动器、固件或参数后必须重新验证。

当前坐标、限位和交接点基线见 [XY 标定测试方案](xy_stage_calibration_test_plan.md)。

## 上机前

运动使用绝对脉冲。仓库没有自动回零。正式任务前必须：

- 由驱动器或现场流程保证零点有效
- `data/objective_state.json` 与真实物镜一致；该文件只是事务记录，仍要读取物镜轴和调焦轴位置后才能信任

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

## 安全锁存与停止验收

XY、物镜或自动对焦取消/异常路径在停止控制字写失败或抛异常时，以及相机子进程经终止、强杀后仍无法确认退出时，会把不安全状态持久化到固定生产路径 `data/hardware_safety_latch.json`。API 启动硬件运行时前会先写入本代 `generation_active` 标记；进程异常退出、标记损坏或锁存未处置时，下一代继续保持禁止准入，而不是靠重启清错。此后 `/api/hardware/status` 返回 `safe=false`、`busy=true` 和 `safety_latch` 详情，新的硬件操作返回 503 `HARDWARE_SAFETY_LATCHED`。

没有用于远程清除锁存的 HTTP 接口，也不得手工删除 marker。先停止上位机流量并按需保持物理急停，保存日志和故障信息，完整停止 API 与全部相机子进程，确认每个机构已经物理停稳、驱动器故障已处置且串口/相机无残留占用。由有权限的现场人员在与服务相同的环境中执行唯一的离线恢复入口：

```powershell
python tools/clear_hardware_safety_latch.py `
  --operator "工号或姓名" `
  --reason "现场检查和故障处置记录" `
  --confirm "I CONFIRM ALL HARDWARE IS PHYSICALLY STOPPED"
```

工具只操作固定生产 marker，不接受路径参数；它必须按顺序同时独占固定的 `data/api_server.lock` 和 `data/camera_worker.lock`，否则不清除。成功时原 marker 会逐字节归档到 `data/hardware_safety_clearance_archive/`，同目录写入包含 operator、reason、时间、哈希和锁路径的 clearance receipt。命令成功后才可重新启动服务，并保存 receipt 作为上线/复机证据。普通重启、删除 `.lock` 文件、设置环境变量或修改进程内变量都不会形成合格的恢复记录。

锁存只证明软件检测到了一个明确的不安全条件；相机类条件是旧 worker 的死亡无法确认，运动类条件是停止命令写失败或抛异常。对于运动，即使未触发锁存，也不能据此证明写入成功后机构真的停止。当前驱动层尚缺以下闭环，软件单元测试也不能替代：

- 按现场驱动器型号/固件手册确认 `0x0006` 是否是期望的停止方式，还是必须使用 `CMD_QUICK_STOP=0x0002`
- 写停止命令后，在有限时限内轮询经厂家确认的状态字位，并同时确认速度为零或连续位置读数稳定
- 分别对 X、Y、物镜和调焦轴做低速取消、通信中断、驱动器故障和停止超时测试，记录控制字、状态字、位置、耗时与物理测量
- 任一轴无法确认停稳时保持物理急停，不允许只因 HTTP 已返回或软件任务显示 `canceled` 就恢复准入

上述映射、代码级状态确认和现场故障注入证据未完成前，不能判定运动控制达到无人值守上线标准。

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
- 生产基线将 `motor.move_timeout_s` 固定为 30 秒，并传给每一次调焦轴绝对移动；校验器要求该值存在、有限、`> 0` 且 `<= 120`，未经重新风险评估不得把现场值调高于 30 秒
- 录像期间复用正在录像的相机，避免第三方模块独占打开
- 复用录像相机时，临时采样图在 `data/autofocus_recording_tmp`

未执行时的检查项见 [常见问题](troubleshooting.md)。

## 任意两点精度测试

`tools/stage_reciprocation_accuracy.py` 测已标定 A/B 点。一个周期是 `A -> B -> A`。输出：

- `moves.csv`：每次目标、运动前后位置、误差、耗时
- `summary.json`：pulse / mm 统计（均值、标准差、极差、最大绝对误差、RMSE）

默认 dry-run，不连硬件。下面的坐标只演示格式，必须换成当前设备、且在软件安全限位内的点：

```powershell
python tools/stage_reciprocation_accuracy.py `
  --axis x `
  --plate-type 24-well `
  --point-a-x 1000000 --point-a-y 1000000 `
  --point-b-x 1200000 --point-b-y 1000000 `
  --cycles 20 `
  --warmup-cycles 2
```

确认端点、行程、从站和安全范围后，同一命令加 `--execute`。

- 测 X：两点 Y 相同
- 测 Y：两点 X 相同
- `--axis xy` 允许两轴都变
- 默认软件 X/电机1 `slave=1`，软件 Y/电机2 `slave=2`

该脚本统计的是驱动器回报位置。丝杆间隙和真实重复定位精度还要同步记量表、光栅或标定板。

固定孔位后台往复（B2–C4）见 [HTTP API](http-api.md)。

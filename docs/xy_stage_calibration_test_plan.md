# 电机 1/2（XY 位移台）标定与分支功能测试方案

## 1. 本次标定基线

| 项目 | 标定值 |
| --- | ---: |
| 电机1 / X 从站 | 1 |
| 电机2 / Y 从站 | 2 |
| X pulses/mm | 65536 |
| Y pulses/mm | 131072 |
| X 物理硬限位 | -1295041 ～ 6525977 |
| Y 物理硬限位 | -1095614 ～ 9241733 |
| 共用 safety_margin | 131072 pulse |
| X 可执行安全范围 | -1163969 ～ 6394905 |
| Y 可执行安全范围 | -964542 ～ 9110661 |
| 交接点 | (5000000, 0) |

`safety_margin=131072` 等于 Y 轴 1 mm、X 轴 2 mm。物理硬限位保持实测值，不把安全余量混入 `x_min/x_max/y_min/y_max`。

四种板型的 A1 起始观测点：

| 板型 | A1 |
| --- | ---: |
| 6-well | (6213780, 7156357) |
| 12-well | (5781368, 7957288) |
| 24-well | (6106452, 8342500) |
| 48-well | (5854763, 8843257) |

方向参数保持 `row_stage_sign=-1`、`col_stage_sign=-1`、`x_stage_sign_for_view_right=-1`、`y_stage_sign_for_view_down=-1`。

## 2. 自动化测试

在项目根目录使用 `ca` 环境执行：

```powershell
C:\miniforge3\envs\ca\python.exe -m workflow.config_validator --plates config/plates.yaml --handoff config/handoff.yaml --camera config/camera.yaml --autofocus config/autofocus.yaml --objectives config/objectives.yaml
C:\miniforge3\envs\ca\python.exe -m pytest tests/test_core_workflow.py -q
C:\miniforge3\envs\ca\python.exe -m pytest tests/test_vision_v2.py -q
```

覆盖点：X/Y 独立脉冲换算、换孔坐标、孔内扫描坐标、视觉补偿、跨视野坐标投影、物理限位与安全余量、A1/最远孔起始点、交接点及 handoff 限位透传。

## 3. 上电前检查

1. 清空运动区域，确认硬件急停可用，先用厂家软件低速点动。
2. 读取电机1/2当前位置，确认驱动器零点没有再次被重置；本项目没有自动 Homing。
3. 分别接近四个物理限位，但不要用程序反复撞限位；确认位置读数与本表一致后退回至少 2 mm。
4. 先装空载测试板，不使用有价值样品；物镜保持在不会碰撞的安全高度。

## 4. 方向和导程验收

以交接点 `(5000000, 0)` 为 A 点。首次执行时降低速度并让操作员手持急停。

X 轴 10 mm 往复：

```powershell
C:\miniforge3\envs\ca\python.exe tools/stage_reciprocation_accuracy.py --axis x --point-a-x 5000000 --point-a-y 0 --point-b-x 5655360 --point-b-y 0 --cycles 10 --warmup-cycles 1 --profile-vel 100000 --plate-type 24-well --execute
```

Y 轴 10 mm 往复：

```powershell
C:\miniforge3\envs\ca\python.exe tools/stage_reciprocation_accuracy.py --axis y --point-a-x 5000000 --point-a-y 0 --point-b-x 5000000 --point-b-y 1310720 --cycles 10 --warmup-cycles 1 --profile-vel 100000 --plate-type 24-well --execute
```

验收要求：

- X 正脉冲：培养板物理方向 A1→A2，画面特征向左。
- Y 正脉冲：培养板物理方向 A1→B1，画面特征向上。
- 实测 10 mm 行程与量具结果一致；误差阈值由现场精度要求确定，不能只用驱动器位置反馈代替量具结果。
- A→B→A 连续 10 次无撞限位、卡死、异常声响或丢步；CSV/JSON 结果保存在 `data/stage_accuracy`。

## 5. 板坐标验收

逐板低速验证 A1、A2、B1，再验证最远孔。软件按当前标定应计算为：

| 板型 | A1 | A2 | B1 | 最远孔 |
| --- | ---: | ---: | ---: | ---: |
| 6-well | (6213780,7156357) | (3920020,7156357) | (6213780,2568837) | B3=(1626260,2568837) |
| 12-well | (5781368,7957288) | (4267486,7957288) | (5781368,4929525) | C4=(1239723,1901762) |
| 24-well | (6106452,8342500) | (4979233,8342500) | (6106452,6088062) | D6=(470356,1579185) |
| 48-well | (5854763,8843257) | (5087992,8843257) | (5854763,7309715) | F8=(487365,1175545) |

每到一个点都确认画面位于对应孔的起始观测位置。若整组点出现相同偏差，重新标定该板 A1；若偏差随行/列累积，重新测量对应轴 pulses/mm 或孔距，不能只修改单个孔坐标。

## 6. 扫描覆盖与安全验收

使用当前物镜 FOV、`overlap=0.1` 对 A1 做离线规划的结果：

| 板型 | 4X | 10X |
| --- | --- | --- |
| 6-well | 通过 | 超过 Y 安全范围 |
| 12-well | 通过 | 超过 Y 安全范围 |
| 24-well | 通过 | 超过 Y 安全范围 |
| 48-well | 超过 Y 安全范围 | 超过 Y 物理硬限位 |

这些路径应在运动前被拒绝，不能通过减小安全余量或扩大硬限位放行。若业务要求 48 孔板 4X 全孔扫描，需沿 Y 负方向重新布置板位并重测 A1；当前离线结果至少需要增加 112443 pulse 的上边界余量，现场还应另留装夹与重复定位公差。若业务要求 10X 全孔扫描，也需要重新核对各板安装位置；如果 10X 仅用于克隆居中观察，则只验收实际局部移动范围。

## 7. handoff 与回归验收

1. 空载执行 `load_in`、`unload_out`，两者均应到 `(5000000,0)`。
2. 将测试配置中的交接点临时改到安全范围外，任务必须在发运动指令前失败；测试后恢复配置。
3. 验证到位误差超过 `arrival_tolerance_pulse=3000` 时不会返回机器人 ready 状态。
4. 依次执行：单孔 4X 采集、跨孔 A1→A2→B1、视觉补偿、自动调焦、4X/10X 物镜切换、handoff。
5. 最后执行目标业务板型的整板任务；任何 `stage_limit_precheck` 失败都应先调整机械安装或重新标定，不绕过保护。

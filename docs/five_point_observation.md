# 4× 五点固定观察与验收

> 2026-10-28: [Optional deduplication / schema migration](optional_deduplication.md). `detect.deduplication.enabled` defaults to `false`; calibration requirements below apply only when explicitly enabled.

`capture` / `pipeline` 的 4× 观察默认每孔拍摄五张，顺序为中心、左上、右上、左下、右下；10× 默认继续使用原全孔扫描。五点是抽样观察，去重后的计数也只代表采样区域，不能直接当作全孔总量。

## 调用方式与兼容性

| `task.objective_name` | `task.scan.mode` | 行为 |
| --- | --- | --- |
| `4x` | 省略或 `five_point` | 每孔五点 |
| `4x` | `full_coverage` | 原全孔扫描 |
| `10x` | 省略或 `full_coverage` | 原全孔扫描 |
| `10x` | `five_point` | 硬件动作前报错，不切镜、不扫描 |

模式参数放在原有 `task.scan` 对象中，不增加 HTTP 路由。若原调用方仍需全孔覆盖，应显式添加 `"scan": {"mode": "full_coverage", ...}`。`observe_scope=full_plate` 只表示选择全部孔：4× 默认仍然是每孔五点，不表示每孔全覆盖。

五点位置由板型几何参数与当前视野决定，`scan.overlap` 不控制五点排布。4× 五点可不传 `overlap`；沿用旧值也不会改变五点位置。当前视野默认来自 `objectives.yaml`；如使用 `scan.fov_override_mm`，完整视野安全校验按覆盖后的宽、高执行。模式、视野、孔位或限位校验失败时整个任务在硬件动作前失败，不会裁剪成少于五点继续拍摄。

## 配置与点位计算

四种板型均在原有 `a1_start` 中保存示教参考视野，并在板型下增加 `five_point`：

```yaml
a1_start:
  # 保留原有 x、y、meaning。
  reference_objective: "4x"
  reference_fov_mm:
    width: 3.22
    height: 3.22
five_point:
  radial_fraction: 0.5
  edge_margin_mm: 0.5
```

`reference_fov_mm` 是示教时的视野，不要随当前物镜、相机 ROI 或当前 FOV 改写。`radial_fraction` 表示非中心点距孔心占孔半径的比例，必须为有限数值且在 `(0, 1)`；边缘余量和参考视野宽、高必须为有限正数。新字段须成组提供。旧配置完全没有这组字段时仍可加载并用于显式全孔扫描；请求五点时会明确报缺少五点标定配置，不能自动猜测参考视野。

各孔起点仍由 `a1_start + 列序号 × well_step.col + 行序号 × well_step.row` 推算，不能用孔径和间隙代替示教步长。设起点为 `(Sx, Sy)`，孔半径为 `R`，当前配置中向右、向下均对应脉冲减小：

```text
Cx = Sx − (R − reference_fov_mm.width / 2) × 65536
Cy = Sy

d_nominal = R × radial_fraction / √2
d_limit   = (R − edge_margin_mm) / √2 − 1.61  # 当前 3.22 × 3.22 mm 视野
d         = min(d_nominal, d_limit)
```

这里 `d` 是每轴偏移，距孔心为 `√2 × d`。相对孔心的五点为 `(0, 0)`、`(-d, -d)`、`(d, -d)`、`(-d, d)`、`(d, d)`，右、下为正。实际规划额外预留半脉冲量化误差，最后一次取整后再次检查矩形四角、五点互异和限位。

| 板型 | 内径 mm | 起点至孔心 mm | 非中心点每轴偏移 d，mm | 最小孔壁余量 mm |
| --- | ---: | ---: | ---: | ---: |
| 6 孔 | 34.28 | 15.530 | 6.060 | 6.293 |
| 12 孔 | 22.00 | 9.390 | 3.889 | 3.223 |
| 24 孔 | 15.28 | 6.030 | 2.701 | 1.543 |
| 48 孔 | 10.43 | 3.605 | 1.724 | 0.500 |

只有 48 孔板需要内收，才能保留配置的 `0.5 mm` 余量。视野宽、高为 `w`、`h` 时，每点都必须满足：

```text
(|dx| + w/2)² + (|dy| + h/2)² ≤ (R − edge_margin_mm)²
```

直接移动到中心开始采集，不先访问原左侧示教点。自动对焦沿用既有触发策略，需要对焦时在中心首拍前执行。

## 结果与重叠去重

`scan_result.json` 的 `scan_config.mode` 记录实际模式；五点结果另有 `scan_config.has_overlapping_views`，记录实际视野是否重叠。五点的 `captures[].point_name` 依次为 `center`、`upper_left`、`upper_right`、`lower_left`、`lower_right`，`index` 从 1 开始；检测结果 `images[].point_name` 保留点位名。原索引和坐标字段保留，`view_right_mm/view_down_mm` 仍相对原示教起点，不改为相对孔心。文件名模板可使用 `{point_name}`；已有 `{index}`、`{row}`、`{col}` 模板仍可使用。

当前 24、48 孔板的中心图与四角图存在重叠。默认关闭跨图去重，不要求去重标定，仍导出全部照片及逐图可选候选。仅当 `detect.deduplication.enabled=true` 时，按实际重叠情况检查 `calibrated=true` 和显式正数 `registration_tolerance_mm`，未标定时在硬件动作前拒绝；此时把 `scan.overlap` 设成 0 或省略不能绕过检查。已有有效标定可复核后复用，不能仅为通过预检把 `calibrated` 改为 true。

新结果的实际重叠标记优先于请求中的 `overlap`；历史结果没有新字段时继续按原 `overlap` 判断，支持读取旧结果。

## 软件验收与现场验收

软件验收不连接硬件，覆盖以下项目：

- 四种板型全部 90 个孔、450 个五点目标：顺序、孔心、完整矩形余量、量化后安全和位移台限位。
- 4× 默认五点、显式全孔、10× 默认全孔及 10× 显式五点失败；旧配置全孔兼容和新配置非法值拒绝。
- 任一所选孔不安全或重叠检测缺少标定时，整项任务在切镜、对焦和 XY 运动前失败。
- 中心首拍、既有对焦触发策略、结果点位名称和坐标语义；24/48 孔重复目标去重及旧结果读取。

在项目根目录使用已安装项目依赖的 Python 环境运行：

```bash
python -m pytest -q tests/test_five_point_geometry.py tests/test_five_point_config.py tests/test_five_point_workflow.py tests/test_plate_scan_geometry.py
python tools/verify_five_point_observation.py --dump-json outputs/five_point_acceptance/geometry.json
```

第二条命令独立核对所有板型、所有孔的量化后视野四角、限位、点位顺序和实际重叠关系，同时检查 10× 回退与显式五点拒绝。它不打开相机或串口；JSON 中保存全部目标坐标及逐板统计，`hardware_acceptance=not_run` 明确表示未做实机验收。

### 本次软件验收记录（2026-10-27）

本次按约定完成软件和模拟采集验收，实机拍照由现场执行。环境为 Windows、Python 3.10.20；相机、XY、物镜和对焦设备均使用测试替身，CUDA 算法回归另在本机真实 GPU 上执行。

| 验收项 | 结果 |
| --- | --- |
| 完整 `tests` 回归，启用 `IPSC_TEST_CUDA=1` | 707 passed；96 个子测试通过；0 failed、0 skipped |
| 四种板型的完整五点几何 | 全部 90 孔、450 点通过四角留边、取整、限位、顺序及重叠检查 |
| 全孔路径与修改前版本比对 | 4×/10×、全部孔、overlap=0/0.1，共 360 份计划、27,366 个目标点一致；仅增加模式元数据 |
| 配置与静态检查 | plates/objectives/handoff 校验通过；修改的工作流和新增工具/测试编译通过 |
| 模拟观察与识别 | 中心首移首拍；每孔五张；按原策略对焦；晚孔非法时零硬件动作；重叠目标合并且相邻不同目标保留 |

本地证据位于 `outputs/five_point_acceptance/`：`pytest.xml` 为完整测试报告，`geometry.json` 为全部五点计划，`full_coverage_regression.json` 记录基线版本与比较数量。该目录属于运行产物，不纳入 Git。测试中有一条 CuPy CUDA 路径检测提示，真实 CUDA 用例仍通过。

完整回归期间另修正了串口诊断测试的日志等级恢复：诊断测试结束后恢复原等级，避免影响随后运行的日志验收；未修改实际串口诊断行为。

### 现场执行清单

现场验收须在受控设备上另行完成；软件用例通过不代表已实拍验收。更改扫描模式本身不要求重做整套孔位标定，前提是设备零点、板夹具、原示教位置关系和视野仍有效。

1. 每种板型检查 A1、行末孔、列末孔及对角孔，确认中心位置和五点顺序；保存图像、目标/实际脉冲、设备零点和配置版本。
2. 重点实拍 48 孔板四角视野，确认整张图及四角均不含孔壁。`0.5 mm` 是配置初值，需核验其能覆盖现场定位误差、孔径偏差和可见孔壁范围。
3. 若孔心整体偏移，修正该板型 A1 基准；若偏差随行列累积，重新示教行末端、列末端并计算 `well_step`。零点或夹具改变时重新核验相应标定。
4. 相机 ROI、光路或实际视野改变时重新测量当前 FOV，并核验原示教起点到孔心的关系；保留示教参考视野的历史含义。
5. 对 24、48 孔板含检测任务复核已有配准容差，或用真实重叠目标标定 `registration_tolerance_mm`，确认同一目标只计数一次。

上线同时发布代码和 `plates.yaml`。仍要求完整覆盖的 4× 调用方须先明确传入 `scan.mode=full_coverage`；运行结果应检查实际模式和每孔点位数。

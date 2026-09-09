# iPSC纹理规则实现与本机验收记录

日期：2026-09-08。基线commit：`509a4a42b29b6eca6d7f54fa5e3dbb0ffb604c25`。
本记录对应用户后续授权：按修订方案修改代码，在本机RTX 500 Ada及conda环境ca先测试。

**2026-09-09更新：用户要求默认使用GPU，当前默认后端已改为cuda。本文下方默认CPU的描述保留为9月8日历史决策，计时结果不变；当前配置见 [默认GPU使用说明](ipsc_gpu_default.md)。**

## 结论

代码改造及本地工程回归已完成；**真实iPSC识别准确率/轮廓金标准验收尚未完成**。B3图片没有人工实例轮廓及允许定位区标注，不能把算法输出自行当作金标准。B3_001在当前规则下仍没有候选；它是否包含应识别的真实团块、具体位置，需要人工参照。未将“B3_001必须出候选”当作无条件调参目标。

350项测试和32个subtests通过，包含实际CUDA执行测试。15张B3的CPU/GPU结果一致性通过；这里只证明两种计算后端的一致性，不证明它们共同输出了正确的生物目标。

**后续接口清理更新：下文保留旧参数、旧位置参数和null暗核字段的描述属于9月8日验收时的行为；当前已删除这些无效参数、暗核字段及旧诊断，并删除无人调用的辅助函数，详见 [参数清理说明](rule_vision_parameter_cleanup.md)。历史实测数字不变。**

## 已实现

- 保留 `vision.vision.detect_pipeline:process_image` 和旧位置参数；以局部标准差及其空间密度作为主信号，取消暗核资格门槛。
- 使用绝对噪声下限，另外通过背景纹理估计检验候选内部纹理覆盖。拒绝仅边缘高方差、内部平滑的黑圆；以内部覆盖、solidity和长宽比例过滤散纹理/细长结构。
- 粗检保留多个独立连通目标，以稳定质量/坐标顺序排序；携带各目标的私有纹理支持mask进入ROI，避免仅凭bbox重叠删除邻团。
- ROI采用连续纹理支持区的实际外轮廓，取消径向星形轮廓和强制椭圆裁剪。旧 `radial_mode/recenter_iterations` 参数保留兼容，但不再参与纹理分割，在返回诊断中明确列出。
- 基于最终输出多边形重新构建mask，结合未填洞的纹理支持区、距离变换选取实际像素；没有足够边界距离或细化后的实例支持不足时显式不可挑取。几何质心与操作中心分开。
- `center_pixel == safe_point` 保持原图 `[x,y]` 整数坐标；新增 `texture_center_pixel`。旧暗核统计字段保留为null，没有把纹理中心伪装成暗核中心。
- 点坐标使用像素中心约定，bbox使用半开端点，按实际X/Y尺寸比例换算，最后舍入；覆盖非整倍、非方图及原图边界。
- GrabCut默认none，显式启用仍可执行，最多2次；mask输出与多边形一致。
- 维持无out_dir不落盘、有目录默认05/06/07、save_debug才写01–04。所有输出模式结果保持一致。
- 新质量字段在成功/失败路径均传递；顶层 `texture_processing` 记录算法、粗检后端及回退原因，包括零候选图片。
- 新规则参数在扫描检测和闭环重新拍照检测中一致透传。归一化中心、图像偏移、补偿接口通过测试。未执行真实设备动作，未改变独立ONNX默认路由。

## GPU与参数

`texture_backend` 支持：

| 值 | 行为 |
|---|---|
| `cpu`（默认） | 直接使用OpenCV/NumPy，不导入CuPy；本次本机完整流水线最快 |
| `cuda` | 使用CuPy；失败直接报错，防止GPU验收实际跑了CPU |
| `auto` | 尝试GPU，失败后本进程记住原因并回退CPU；不是动态测速择优模式 |

CPU/GPU采用float32中心化矩计算、相同reflect边界及1/256强度量化；GPU连续计算局部均值、平方均值、标准差、密度后一次同步回传。CuPy惰性导入；`backend_status()`可检查实际后端、回退原因/次数和内存池保留量。

规则入口默认CPU是本次实测后的工程选择：GPU算子更快，但本机这组完整I/O流水线没有优于新版CPU，冷启动开销也更大。RTX 3080上需重新测量后决定部署后端。没有把GPU默默忽略：显式cuda验收的15张图全程运行GPU，回退次数为0。

其他新增可配置项：`texture_noise_floor=1.5`（原uint8强度单位），`texture_window=7`（工作图奇数窗口），`refine_work_max=1200`（可配置1600），`safe_margin_px=1.0`（原图像素）。`coarse_work_max=1024`继续可配置。窗口在工作图像素中定义，倍率变化需要重新核验，不宣称已自动适配所有倍率。

**默认1像素仅提供几何内部点约束，不代表挑取针/机械误差的安全距离已经标定。** 可通过任务配置设置已有的操作余量；尚无设备余量数据时，不宣称完成机械挑取验收。调整安全距离会改变可挑取状态，不改变下游字段含义。

任务配置示例（这里1只是代码默认值，不是设备推荐值）：

```json
{
  "detect": {
    "entrypoint": "vision.vision.detect_pipeline:process_image",
    "texture_backend": "cpu",
    "texture_noise_floor": 1.5,
    "texture_window": 7,
    "refine_work_max": 1200,
    "safe_margin_px": 1.0,
    "save_debug": false
  }
}
```

也可直接调用 `process_image(path, texture_backend="cuda", out_dir=...)`；CLI支持 `--backend legacy --texture-backend cuda --safe-margin-px ...`。独立模型的 `--provider` 不用于规则GPU配置。

## 本机实测

环境：`C:\miniforge3\envs\ca\python.exe`，Python3.10.20、OpenCV4.13.0、NumPy2.2.6、CuPy14.2.0（cupy-cuda12x），CUDA runtime12.9.79、NVRTC12.9.86。GPU：NVIDIA RTX 500 Ada Generation Laptop GPU，4094MiB。

输入：桌面 `pipeline_B3_legacy_20260907_002/B3/images` 中15张5120×5120 BMP。相同硬件、图片与05/06/07输出配置。每种变体在独立进程预热一张后，连续测5轮；统计读取/解码、处理和同步输出函数返回的墙钟时间，不含生成缩略图/输入散列。OS页缓存和磁盘负载未严格控制，不能把几百分之一秒差异当作稳定收益。

| 变体 | 15张耗时中位数 | 单图P95 | 首张冷调用（不落盘） |
|---|---:|---:|---:|
| 旧规则CPU | 12.960s | 1.530s | 0.522s |
| 新纹理CPU | 5.040s | 0.706s | 0.093s |
| 新纹理CUDA | 5.394s | 0.760s | 5.983s |

在本次样本/配置下，新CPU较旧规则耗时下降约61%，新CUDA下降约58%。**算法和候选数量同时改变，这不是纯GPU加速比，也不能以更少检出冒充准确率提高。** GPU初始化/编译较重，服务进程可显式预热；不要以忽略冷启动的数字描述单次脚本耗时。

独立算子测试（含上传和同步回传，随机uint8图，预热后10次中位数）：1024²的局部矩+密度CPU约24.43ms、GPU约5.64ms；1600²约65.01ms/15.18ms。这说明算子确实使用GPU且有收益，但无法推出整条链必然更快。

CUDA最终内存池保留120,820,224字节（约115MiB）；此值不是进程/设备总峰值显存。本次未发生OOM或回退，不代表并发与其他模型共用GPU时已验收。

## B3结果和验证边界

旧规则共输出14个目标；新规则9个粗候选中，2个通过细化，7个被拒绝并保留失败诊断。各图候选数为：`[0,0,0,0,2,2,0,0,0,0,0,0,3,2,0]`。成功细化分别位于B3_005和B3_014。

2个成功目标的CPU/GPU轮廓IoU为0.999956和0.999545，定位点差0像素，全部落在各自最终轮廓内；另外7个拒绝状态一致。B3_001仍为0候选、B3_004为0候选：不能仅从数量判断漏检已修复或误检已消除，需要人工指认实际克隆。

完整测试：`350 passed, 32 subtests passed`。包含亮/暗纹理团、非凸U形、内部大空洞、邻团、触边、亮度变化、平滑黑圆/划线/低噪声负例、原图坐标恢复、安全余量不足拒绝、真实CUDA一致性、失败回退、输出契约、扫描和闭环补偿配置/坐标，以及仓库现有回归。测试中的形状精度阈值仅适用于合成数据，不是生物准确率报告。

以下尚待真实标注验收：precision/recall、实例IoU/Dice及边界物理误差、分化区域排除、失焦/不同倍率泛化、粘连实例的正确拆分。当前连通区域没有足够分离证据时按一个实例处理，未加入未经标注验证的强制分水岭。这个限制必须在粘连样本验收中单列。

## 重现与工件

工件目录：`outputs_ipsc_texture_20260908/`（运行产物，不纳入Git）。包含baseline快照、三种正式变体的 `timing.json`、逐图JSON、contact_sheet.jpg、最终05/06/07，以及 `cpu_gpu_consistency.json`、`operator_timings.json` 和 `code_manifest.json`。

```powershell
# 本机NVRTC不能打开中文临时目录中的源文件；仅为当前进程设置可写ASCII目录。
$env:TEMP = 'C:\colony_system.worktrees\deploy-linux-20260902\outputs_ipsc_texture_20260908\cuda_temp'
$env:TMP = $env:TEMP
$env:CUPY_CACHE_DIR = "$env:TEMP\cache"
$env:IPSC_TEST_CUDA = '1'
& 'C:\miniforge3\envs\ca\python.exe' -m pytest tests -q
```

没有修改系统环境变量或安装/替换ca中的依赖。可选依赖文件 `requirements-vision-texture-gpu.txt` 仅锁定已实测的CuPy包，不覆盖现有ONNX/CUDA依赖锁。其他平台应采用其适用的CUDA运行时，不能直接假定本机组合等价于Linux部署。

用 `tools/benchmark_ipsc_texture.py --root ... --images ... --out <新目录> --backend cpu|cuda|baseline --repeats 5 --write-outputs` 重跑（out必须是新目录）。`tools/compare_texture_results.py` 比较CPU/CUDA结果；真实识别质量仍需另行提供人工参考标注，不能由该比较脚本推导。

# OpenCV 规则检测：输出与内存优化验收

本轮保持规则算法和模型后端结构不变。优化目标是识别结果、05/06 像素不变，默认少写 01–04，减少图像缓冲区和同步输出开销。没有修改阈值、粗检测尺寸、径向搜索、GrabCut 次数、评分、孔边界或挑取坐标规则。

## 输出与接口

| 入口/配置 | 默认行为 | 恢复完整调试输出 |
| --- | --- | --- |
| `detect_from_gray(gray, ...)` | `out_dir=None`，只返回字典 | 显式指定目录和 `save_debug=True` |
| `detect_from_path(image_path, ...)` | 原默认目录 `outputs_5120_contour_refined_opt`，写 05–07 | `save_debug=True` |
| 规则 `process_image(image_path, ...)` | 未传 `out_dir` 时只返回字典 | 指定目录和 `save_debug=True` |
| workflow 显式规则入口 | `detect.save_debug=false`，有规则输出目录时写 05–07 | `detect.save_debug=true` |
| `vision/run_detect.py --backend legacy` | 有输出目录时写 05–07 | 增加 `--save-debug` |

保留文件是 `05_contour_mask.bmp`、`06_overlay.bmp`、`07_result.json`；完整模式增加 `01_gray.bmp`、`02_coarse_flat.bmp`、`03_coarse_binary.bmp`、`04_refine_density.bmp`。复用旧目录时不会删除历史 01–04，核验文件集合或体积请使用新目录。

`save_debug` 是新增布尔选项，旧参数位置和算法默认值保持不变。直接调用低层 `detect_and_refine` 仍默认返回完整 debug 数组，直接调用 `save_outputs` 仍默认写完整文件并保护调用方 overlay。

workflow 仅向 `vision.vision.detect_pipeline:process_image` 和 `vision.detect_pipeline:process_image` 透传该选项。模型/第三方入口参数不增加，后端默认选择不变。`save_overlay=false` 或 `overlay_source=workflow` 时，沿用原先不向规则入口提供输出目录的行为；`save_debug=true` 不会强制落盘。无落盘模式原先的 `scale_bar=None` 返回语义也保留。

任务配置片段：

```json
{
  "detect": {
    "entrypoint": "vision.vision.detect_pipeline:process_image",
    "save_debug": false,
    "save_overlay": true,
    "overlay_source": "vision"
  }
}
```

## 实现范围

- 文件入口复用已归一化的灰度图，避免再次复制；公开内存入口仍复制/转换以保护调用方。
- 根据输出模式跳过全图调试密度图、调试图放大；无落盘时还跳过全图 mask、overlay 和绘制。
- mask/密度图合并用原地 `np.maximum`；独占 overlay 不再为保存而复制一次。
- 每个 ROI 结束即释放细化结果和 debug 缓冲区，避免上一 ROI 的大型数组跨越下一次 GrabCut。
- 仍同步编码、写入 BMP，并完成原子 JSON 写入后返回。没有新增后台队列，也没有将“入队”作为落盘完成。

## 本地跨版本实测

环境：Windows，Python 3.10.20、OpenCV 4.13.0、NumPy 2.2.6；OpenCV 原默认 22 线程。基线是修改前独立源码快照 `colony-rule-baseline-c8v9_vx7`，快照记录的提交为 `07fbe5f88556fc0b6ce0d6ac0f1faa4d79f063a4`。每个变体运行独立、串行子进程，并校验实际导入文件路径及 SHA256，避免两边误用同一份源码。

两张真实 BMP 均为 5120×5120，分别检测到 2 和 1 个目标。相同随机种子 `20260905`、相同原图路径和比例尺参数（`mm_per_pixel=0.001`、`length_mm=0.5`）：

- 新版完整模式对原版完整模式：返回 JSON 全字段一致，01–06 全部解码后像素一致。
- 新版默认模式对原版完整模式：返回 JSON 全字段一致，05/06 全部像素一致；保存的 07 与返回字典一致。
- 新版无落盘模式对原版无落盘模式：返回 JSON 全字段一致，没有忽略或剥离 `scale_bar` 等字段。

| 输入 | 原版完整输出 | 新版默认输出 | 减少 |
| --- | ---: | ---: | ---: |
| `Image_20260616114812600.bmp` | 209,735,422 字节 | 104,873,510 字节 | 104,861,912 字节，约 50% |
| `Image_20260317113509384.bmp` | 209,729,157 字节 | 104,867,245 字节 | 104,861,912 字节，约 50% |

下表记录从文件读取到同步输出函数返回的单次时间；每张图/变体仅测 1 次，无预热，用于如实记录此次对照，不能推导稳定的 P95 或工控机性能承诺。

| 输入 | 原版完整 | 新版完整 | 新版默认 | 原版不落盘 | 新版不落盘 |
| --- | ---: | ---: | ---: | ---: | ---: |
| `Image_20260616114812600.bmp` | 43.987 s | 40.582 s | 40.193 s | 40.703 s | 41.963 s |
| `Image_20260317113509384.bmp` | 106.490 s | 102.479 s | 107.852 s | 103.056 s | 100.399 s |

输出体积减半已证实；端到端速度在两张图上有波动，尚不能宣称稳定整体加速或“耗时减半”。原分辨率 ROI 的 GrabCut 仍是需要后续算法实验的重点，本轮保持它的行为不变。

补充测量实际 `save_outputs` 阶段：两份源码在独立串行进程中各执行一次真实双目标图检测，使用实际产生的结果缓冲区，输出预热 1 次后测量 5 次。每次在计时外恢复未绘制比例尺的 overlay，并在计时外校验 JSON/05/06，避免重复绘制或校验开销混入结果。原版完整输出中位数 **0.2935 s**，新版默认输出 **0.1552 s**，输出阶段耗时减少约 **47.1%**。这是约 0.138 s 的输出收益，不能将此比例套用到数十秒的整次检测。

该实验逐次 JSON/05/06 及跨版本检查全部通过，报告见 [output_stage/report.json](../outputs_rule_vision_acceptance/output_stage/report.json)，可复跑脚本为 [profile_output_stage.py](../outputs_rule_vision_acceptance/profile_output_stage.py)。该脚本属于本地验收产物，未加入生产部署代码。

原始报告在本工作区的 [real_two_images/report.json](../outputs_rule_vision_acceptance/real_two_images/report.json)，SHA256：`3420fd050e304ed170d2e2302222762a099cb72df34bfd1c6d00b87dc5603ac0`。该目录属于忽略的运行产物，不随源码自动同步。

## 内存与回归验证

内存验收结合数组生命周期测试和独立进程连续检测的原生 RSS 观测，避免把 Python 对象释放等同于全部原生内存释放，也避免把分配器缓存直接判定为泄漏。修正采样器后，得到以下结果（MiB = 1,048,576 字节）：

| 场景 | 预热 + 测量轮数 | 采样峰值 | 每轮释放后 RSS 范围 | 被观测数组残留 |
| --- | ---: | ---: | ---: | ---: |
| 原版完整，真实 5120 双目标图 | 1 + 3 | 1945.35 MiB | 52.99–56.44 MiB | 0 |
| 新版默认，同一真实图 | 1 + 5 | 1894.54 MiB | 52.25–53.55 MiB | 0 |
| 新版默认，512 双目标合成图 | 3 + 100 | 88.02 MiB | 54.16–71.23 MiB | 0 |
| 新版无落盘，同一合成图 | 3 + 100 | 89.18 MiB | 54.07–71.74 MiB | 0 |

真实大图峰值降低约 50.81 MiB；GrabCut 的原生内存仍占主要部分。合成图默认模式前/后 10 轮释放后 RSS 中位数为 54.60/55.60 MiB，无落盘模式为 54.65/55.16 MiB；部分轮次暂时升至约 71 MiB，后续又回落，没有持续累积增长。弱引用覆盖灰度图、ROI mask/density 和全图 debug；新版共观察 3610 次数组引用，返回后存活数全部为 0。真实图多轮 JSON/05/06 仍与基线一致。

另有回归测试保留 9 轮返回字典，确认它们没有携带大型数组引用，并在下一 ROI 进入 GrabCut 前确认上一 ROI 缓冲区已经释放。本次结果支持所测路径无累积图像缓冲区泄漏，有限样本不能替代工控机长期运行观测。

有效原始数据见 [memory_soak_fixed/summary.json](../outputs_rule_vision_acceptance/memory_soak_fixed/summary.json) 及其同级各 worker 的 `rss.json`。早期 `memory_soak` 目录已标为无效：Windows 采样器原先每次动态创建 ctypes 指针类型，导致工具自身缓存增长。修正版仅初始化一次，并增加连续 1000 次采样缓存不增长、采样线程正常/异常退出均释放的回归测试；本页不使用作废数据。

当前全套测试结果为 **312 passed、6 failed、32 subtests passed**。6 项失败涉及本轮之外更新的默认扫描重叠率与交接点配置：5 项仍期待 overlap=0.1，而配置已改成 0；1 项仍期待旧交接点 (5000000, 0)，而配置已改成 (1970000, 1630000)。在独立副本保留其他工作区状态、仅将本轮运行时文件全部恢复为原基线后，这 6 项仍以相同断言失败，已确认不由本轮视觉改动引入。没有修改这些并行任务的配置或测试。

本轮新增测试全部通过，覆盖输出集合/体积、真实算法像素、比例尺、中文路径、8/16 位/浮点/非连续输入、重叠 mask、细化失败、旧低层默认值、接口透传与内存所有权。关键分割/预处理/特征/评分/孔边界、加载器、模型运行时及补偿/API 模块也逐文件校验，内容与原基线一致。变更源文件语法与 `git diff --check` 检查通过。

完整 `tools/release_gate.py` 在本地环境预检未通过：未安装锁定的 ONNX Runtime CPU/GPU profile，且 packaging、Pygments、typing_extensions 与测试锁定版本不符。没有为本轮修改依赖锁或模型环境；这次验收不能代替 Linux 工控机发布门禁，也没有执行工控机同步部署。

## 工控机复验与同步

新增工具 [benchmark_rule_vision.py](../tools/benchmark_rule_vision.py) 不依赖 Git 或额外性能分析库。保留修改前完整项目目录作为 baseline，将当前代码同步到另一个 candidate 目录；两边用同一个工控机 Python 环境，读取同一张原图。报告目录必须是不存在的新目录，工具不会递归删除旧目录。

```bash
python tools/benchmark_rule_vision.py \
  --baseline-root /opt/colony_system_before_rule_io \
  --candidate-root /opt/colony_system \
  --image /data/acceptance/colony_1.bmp \
  --image /data/acceptance/colony_2.bmp \
  --report-dir /data/acceptance/rule_io_run_001 \
  --warmup 1 --repeat 5 --memory-repeat 20
```

可用 `--kwargs-file options.json` 提供两边完全相同的算法和比例尺参数；文件必须是 JSON 对象，不能包含 `image_path`、`out_dir`、`save_debug`。`--threads` 未设置时保留环境默认；如要比较线程数，应为每个线程数运行独立报告。`--skip-memory` 可仅执行结果/像素和时间对照。

工具分开运行 timing、RSS、tracemalloc 三阶段；计时不包含哈希校验、输出图重读或性能采样。RSS 每 10 ms 采样可能漏掉更短峰值；tracemalloc 不保证覆盖所有 OpenCV 原生分配。有限轮数可发现累积增长，不能证明所有未来输入永不泄漏。任何结果、像素、产物集合差异或被观测数组残留都会导致非零退出码；RSS 趋势另写入报告供检查。

“同步落盘完成”指文件 API 已返回，不新增设备缓存刷盘保证。报告不包含相机采集、任务队列、HTTP 往返和后续补偿移动时间。

运行时变更文件为 `vision/vision/detect_pipeline.py`、`vision/vision/postprocess.py`、`workflow/detect_executor.py`、`vision/run_detect.py`、`workflow/run_task.py`。后一项把重叠去重预检扩到规则和第三方入口，只同步前四个文件时工控机仍会先拍完再报未标定。同时同步本页、关联接口文档、新增验收工具及 `tests/test_rule_vision_api_contract.py`、`tests/test_rule_vision_outputs.py`、`tests/test_benchmark_rule_vision.py`、`tests/test_vision_v2.py` 中本轮预检用例；不要混入本轮之外的工作区变更。依赖、模型文件和分割模块无需因本轮更新。

需要原完整产物时设置 `save_debug=true` 或 CLI `--save-debug` 即可。需要撤销实现时，从同步前备份恢复上述五个运行时文件；同步和回退安排在没有活动检测任务时进行。工控机未安装 Git 不影响上述比较和恢复流程。

## 参考项目可借鉴的后续算法实验

实际检查的参考目录为桌面的 `ipsc_texture_classifier(1)`；用户给出的分层路径 `ipsc/_texture/_classifier(1)` 在本机不存在。参考目录也有独立更新，以下针对本次读取到的代码，未将其算法移入当前部署分支。

1. **局部标准差纹理可补充候选证据。** `colony_center.py:54` 使用邻域方差开方，随后平滑、Otsu 和形态学得到区域。当前 `segment.py` 已有低分辨率粗检测和 CLAHE/Sobel 纹理回退，不能把“先缩图”和“加入纹理”当成完全缺失的能力。值得实验的是在弱暗核、弱对比 ROI 中加入局部方差，与现有暗核/梯度证据结合，而非用纹理全局替换暗核。
2. **纹理统计应先在小图或候选 ROI 计算。** 参考 CPU 代码全图使用 float64 和较大圆盘开闭运算，并非天然更快。可用 OpenCV `boxFilter`、`sqrBoxFilter` 计算 `E[I²] - E[I]²`，保持窗口尺度、边界模式和精度控制；仍需验证数值及最终阈值差异。OpenCV 官方说明平方盒滤波可用于局部方差/标准差：[Image Filtering](https://docs.opencv.org/4.13.0/d4/d86/group__imgproc__filter.html)。
3. **不要照搬“只留最大连通域”。** 参考 `segment_colony_cpu` 仅留下全图最大区域，默认剔除小于全图 1% 的对象；这会丢掉多克隆图里的其他克隆和小克隆。参考 `describe` 的几何质心也不能直接替代现有挑取安全点，凹形区域的质心可能落到区域外。
4. **更大速度收益需要针对原分辨率 GrabCut 单独实验。** 当前 `segment.py:621` 的 GrabCut 接收原 ROI，径向工作图虽限制尺寸，后续精修仍有大图成本。可实验小图轮廓初始化、只处理不确定边界区域，以及可信区域跳过精修；这些都会改变结果，须用人工标注和困难样本校验召回率、假阳性、mask 边界、中心/安全点误差，不能直接纳入本轮像素不变承诺。
5. **参考 GPU 分支的成绩不能移作当前算法成绩。** 现有 `gpu_backend.py` 使用 CuPy、float32 和距离变换实现圆盘形态学。其 `bench_gpu_results.json` 报告同一 5120 图 CPU/GPU 计算约 96.62/0.48 s、IoU 0.9998、中心差 0.1 px；这是该目录已有记录，本轮没有复跑。它加速的是另一套单目标纹理算法，IoU 也未达到像素相同，不能据此宣称当前 GrabCut 流程可直接快 200 倍。工控机 GPU/驱动、传输、预热和结果一致性都需独立验证。

参考 `features.py` 的多尺度 LBP + `train.py` 的 SVM 可用于未来 ROI 误检分类，但会引入训练和数据维护；定位入口并未调用它，本次读取到的 `classify.py` 也基本为空。当前不引入此分类依赖。另需保留现有灰度归一化：参考加载器的 RGB 权重和位深处理与当前实现不同，直接替换会改变输入像素。

建议顺序是先完成本轮等价输出优化，再对独立开关下的 ROI 纹理与精修策略做 A/B 实验。以人工标注作为准确性依据；“和旧算法一致”只证明回归稳定，不能证明旧算法本身准确。

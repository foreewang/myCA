# 规则视觉无效参数清理

当前纹理规则接口已移除不参与计算的旧参数。算法、有效参数默认值、定位点和可挑取判定未因本次清理改变。

## 公开入口

`process_image`、`detect_from_path`、`detect_from_gray`、`detect_and_refine` 不再接受以下11个参数：

| 原用途 | 已移除参数 |
|---|---|
| 暗种子阈值 | `seed_thresh`、`seed_quantile`、`seed_hard_floor`、`seed_hard_ceil` |
| 暗核心密度 | `core_density_min` |
| 前景占比 | `min_foreground_ratio`、`max_foreground_ratio` |
| 暗核心面积占比 | `min_dark_core_area_ratio`、`max_dark_core_area_ratio` |
| 径向轮廓及重定位 | `radial_mode`、`recenter_iterations` |

直接调用时传入上述参数会报 `TypeError`；应从调用代码中删除，不需要用其他参数替代。删除前这些值已经不影响纹理分割。`radial_mode` 的旧取值校验也已移除。

## 底层函数

除对应的公开旧参数外，以下底层参数也已移除：

| 函数 | 已移除参数 |
|---|---|
| `segment.detect_coarse_rois` | `flat_sigma`、`density_sigma`、`close_kernel`、`open_kernel`、`pad_ratio`、`nms_iou_thr` |
| `segment.refine_contour_in_roi` | `dark_percentile`、`density_sigma`、`radial_target_alpha`、`n_angles`、`recenter_min_shift_px` |
| `preprocess.roi_density_signal` | 已删除整个无人调用的旧辅助函数，包含其全部参数 |

函数签名已缩短，旧的位置参数调用也需要按新签名调整；建议对调参项使用关键字参数，避免位置含义错位。

## 工作流与结果

扫描检测和闭环补拍继续只透传白名单内的有效参数：`texture_backend`、`texture_noise_floor`、`texture_window`、`coarse_work_max`、`refine_work_max`、`safe_margin_px`。任务配置中的旧参数原本就不在白名单中，仍不会传入规则入口；维护配置时应删除这些无效项。

结果中的 `texture_processing.inactive_legacy_parameters` 已删除；`algorithm`、`coarse_backend`、`fallback_reason` 继续保留。粗检中固定为 -1 的旧 `seed_thresh` 诊断及对应的顶层 `coarse_seed_thresh` JSON字段也已删除，包括内存返回和落盘结果。当前仍有实际意义的 `coarse_density_thresh` 保留。无人调用的旧 `auto_seed_threshold` 函数已删除。

后续残留清理还移除了固定为 null 的 `dark_core_center_pixel`、`dark_core_area_small`、`dark_core_area_ratio`、`foreground_ratio`，以及暗核心中心回退逻辑。成功和失败目标均不再输出这些字段；工作流中心解析也不再读取暗核心中心。

规则函数中的独立 `mm_per_pixel` 参数及透传已删除，直接传入会报 `TypeError`。需要比例尺时使用 `scale_bar={"mm_per_pixel": ...}`；CLI的 `--mm-per-pixel` 仍负责构建该配置。工作流通用检测适配层在调用规则模块前过滤其通用 `mm_per_pixel` 元数据，包含规则入口的别名；模型调用及工作流自身坐标换算保留该字段。

仍有效的缩放、扩框、数量限制、边界过滤、轮廓裁剪和 GrabCut 参数继续保留。例如底层旧 `pad_ratio` 已删除，而流水线的 `refine_pad_ratio` 仍控制 ROI 扩框。

调用示例：

```python
from vision.vision.detect_pipeline import process_image

result = process_image("sample.bmp", texture_backend="cuda", safe_margin_px=1.0)
```

`safe_margin_px=1.0` 是现有代码默认值，设备所需余量仍需另行标定。历史方案和验收文档保留当时记录，以本文描述当前接口。

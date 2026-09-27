# 验收绿色候选接入正式规则入口

用户要求以当前绿色正常纹理候选效果为准修改视觉代码。本次将已验收的单图算法设为规则入口默认实现，没有重新调整识别阈值；未处理跨图克隆，未改变默认模型路由，也没有重启服务或向工控机部署。

## 入口与结果

- `vision.vision.detect_pipeline:process_image` 及 `vision.detect_pipeline:process_image` 默认运行 `single_image_rules_v2`。
- `detect_from_gray`、`detect_from_path` 和兼容规则 CLI 使用相同实现。
- 绿色 `normal_candidate` 进入正式 `components`，`is_pickable=true`、`is_valid_for_compensation=true`；中心与轮廓沿用验收结果。
- 红色、橙色纹理和 D 类暗区证据进入 `review_candidates`，两项有效性均为 `false`，不进入正式计数或挑取导出。
- 默认保留验收中的图像触边绿色候选，并输出截断与尺寸下界信息。没有新增跨图判断。
- 扫描和补拍传入相同 `mm_per_pixel` 标定；适配层不再删除规则入口的物理尺度。
- 保持原有 05–07 / 01–07 输出模式；正式 overlay 与 mask 仅绘制正式绿色目标，mask 保留内部排除区。

输入允许明确额外限制 `safe_margin_px`、`reject_border_touch`、`max_keep`。没有设置时与验收一致；显式限制导致不满足时转诊断候选，不会绕过约束。

## 实现与兼容

`single_image_segment.py` 是生产几何/分类实现，来自被冻结的实验算法；实验模块保持独立以供对照。
`legacy_detect_pipeline.py` 保存旧算法。旧测试明确检查此旧算法，新增正式算法测试覆盖新入口及工作流。

当前保持验收时的 CPU 算法。`texture_backend=cuda/auto` 作为兼容请求记录在结果中，实际后端和原因明确标记为 CPU；没有把当前版本宣称为 GPU 实现。
旧 ROI 调参对新版不生效，在 `texture_processing.ignored_legacy_options` 中列出；新参数由 `single_image_options` 映射提供。

任务配置示例：

```json
{
  "detect": {
    "entrypoint": "vision.vision.detect_pipeline:process_image",
    "rule_algorithm": "single_image_rules_v2",
    "save_overlay": true,
    "save_debug": false
  }
}
```

如需旧规则对照，在同一入口设置 `rule_algorithm="connected_texture_v1"`，可显式指定其 `texture_backend`。
命令行：`python vision/run_detect.py IMAGE --backend legacy --rule-algorithm connected_texture_v1 --texture-backend cpu`。
直接调用新版时建议传 `mm_per_pixel` 或 `fov_mm`；都未传时按完整 4x 3.22×3.22 mm 视野处理并记录这一默认来源，不能用于未经标定的裁剪图。

## 原图逐项验证

使用固定验收快照 `tests/fixtures/ipscell_green_acceptance.json` 和原始输入 SHA-256 验证，比较的不只是数量，还包括 ID、中心、bbox、面积与全部轮廓点哈希。

| 图像 | 验收绿色数量 | 正式结果数量 | 几何完全一致 |
|---|---:|---:|---|
| A1_013 | 2 | 2 | 是 |
| A2_011 | 0 | 0 | 是 |
| A5_012 | 3 | 3 | 是 |
| C1_001 | 1 | 1 | 是 |
| C6_011 | 1 | 1 | 是 |
| D1_012 | 0 | 0 | 是 |
| D6_012 | 2 | 2 | 是 |
| 合计 | 9 | 9 | 是 |

诊断记录不参与上述数量统计。此结果证明正式接入没有改变用户认可的效果，不代表对所有培养批次都已获得准确率保证。

验证命令：

```powershell
C:\miniforge3\envs\ca\python.exe tools/verify_ipscell_green_acceptance.py --out outputs_ipscell_promoted_next
```

输出目录必须不存在。本次结果位于 [verification.json](../outputs_ipscell_promoted_20260924/verification.json)，同目录保存七图正式 JSON 和仅绿色轮廓预览。

## 回归覆盖

新增测试覆盖绿色几何提升、红/橙排除、标定透传及两个入口别名、扫描/补拍一致、可挑取导出、输出模式一致与 mask 内部排除、额外安全余量和旧算法显式回退。旧规则测试继续验证被冻结的旧实现。

本次相关回归测试结果：**204 passed, 1 skipped**（38.34 秒；GPU 条件测试跳过）。七张原图的独立逐项验证全部通过；`git diff --check` 无空白错误。

仍保留验收时已知的限制：孔边阴影没有完整几何/空孔标定，边缘不确定区域转复核；轮廓来自统一工作网格而不是原生 5120 像素贴边；当前 CPU 每张大图约数秒。后续改善需再与本次验收快照比较。

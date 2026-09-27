# V4 单图规则正式接入（2026-09-27）

用户已确认 V4 可以上线。本次把已验收的算法接入当前工作支线，保留先前未提交的其他改动。没有修改独立 V2/V3/V4 试验目录，没有自动提交或执行设备运动。

## 接口行为

规则入口 `vision.vision.detect_pipeline:process_image` 及兼容别名 `vision.detect_pipeline:process_image` 默认运行 `single_image_rules_v4`。观察识别、扫描检测和补拍继续使用现有标定与 schema v2，不新增必填参数。

```json
{
  "detect": {
    "entrypoint": "vision.vision.detect_pipeline:process_image",
    "rule_algorithm": "single_image_rules_v4",
    "save_overlay": true,
    "save_debug": false
  }
}
```

已有规则任务不设置 `rule_algorithm` 时直接走 V4；显式锁定 `single_image_rules_v2` 的任务继续走上一版，要升级应改成 V4。配置仍选择模型入口的任务继续走模型，更新文件不会切换其路由。CLI 的 `--backend legacy` 表示规则路由：

```bash
python vision/run_detect.py /path/to/image.bmp --backend legacy --rule-algorithm single_image_rules_v4 --out-dir /path/to/new_result
```

绿色正常纹理目标进入 `components`，通过最终纹理掩膜中心点及余量检查后标记为可挑取、可用于补偿。橙色纹理和红色暗区进入 `review_candidates`，两项有效性为 false，D6 合并区域仍为橙色。原有 `safe_margin_px`、`reject_border_touch` 和 `max_keep` 限制继续生效；限制不满足时转入复核，不更改验收轮廓来绕过限制。

正式 mask 保留暗区排除和纹理空洞。中心点余量在最终可用纹理掩膜上重新计算，单位为原图像素。新版本补齐规则启发式 confidence，仍不应把它解释为统计准确概率。

不新增依赖，使用现有 NumPy/OpenCV CPU；传 `texture_backend=cuda/auto` 仍记录请求，但执行 CPU。默认模型路由、物理标定、跨图处理和硬件参数均未修改。1.5 mm 不是整块克隆的最大直径过滤阈值。

## 实现和验收来源

`single_image_segment.py` 继续产生基础分割证据。新增 `single_image_refine_v2.py`、`single_image_refine_v3.py`、`single_image_refine_v4.py` 是独立试验的冻结算法副本，仅改成包内相对导入。正式入口不依赖 `vision/experiments` 或本机试验目录；各阶段仍只输出图像证据，由 `detect_pipeline.py` 做最终可挑取映射。

`tests/fixtures/ipscell_v4_acceptance.json` 从已经验收的 V4 报告冻结，包含原图 SHA256、绿色及复核记录的几何摘要、各阶段配置及工作掩膜哈希。它不是用本次正式代码重新生成的期望结果。

逐图正式接口验证：A1=3、A2=1、A5=3、C1=1、C6=1、D1=1、D6=2，共 12 个绿色候选。全部目标的编号、轮廓、面积、中心点及复核分类与验收版一致；D6 原 C02 合入 C01，其他编号保留。复现：

```bash
python tools/verify_ipscell_green_acceptance.py --out /path/to/new_acceptance
python -m pytest tests/test_accepted_single_image_rules.py tests/test_v4_production_rules.py tests/test_rule_vision_api_contract.py -q
```

旧 2026-09-24 验收快照仍保留，可加 `--fixture tests/fixtures/ipscell_green_acceptance.json` 验证显式 V2 回退。完整本机测试和门禁状态见部署包内 `validation/`，不能将单图验收通过等同于部署环境门禁通过。

## 工控机同步

增量包不包含现场 config、图像、模型权重、SDK 或 Python 环境。包含这次正式接入所需的视觉运行模块和工作流适配层，兼容当前支线已有的第一版未提交改动。以包内 `manifest.json` 为完整清单。

1. 将 ZIP 解压至独立目录；使用现场解释器运行 `python verify_sync.py --target /实际/项目目录 --mode before`。如果有 MISMATCH，先比较现场改动，不直接覆盖。
2. 在无活动任务时停止现场 API 服务，并完整备份项目。按原部署方式将 `payload/` 内容合并进项目根目录，不覆盖现场配置。
3. 运行 `python verify_sync.py --target /实际/项目目录 --mode after`。检查任务选择规则入口，未设置算法参数或显式为 V4。
4. 在现场已有运行环境中运行 `python -m workflow.deployment_preflight`；有完整测试环境时按原流程运行 `python tools/release_gate.py`。随后用已有图像离线核对 JSON 的 `texture_processing.algorithm=single_image_rules_v4`，绿色可挑取、红/橙不可挑取。
5. 预检通过后按现场原方式重启 API 并验收观察识别。仓库示例服务名是 `colony-system-api.service`，实际名称以现场为准。

本次开发环境可以运行规则算法，但完整 release gate 因缺少 ONNX runtime 以及测试锁版本偏差而未通过。没有修改开发环境来掩盖该结果；工控机需在原有完整运行环境中通过预检。包内保留门禁原始日志。

## 回退

快速算法回退：保持规则入口，显式设置 `rule_algorithm=single_image_rules_v2`（上一版绿色方案），或者 `connected_texture_v1`（旧连通纹理算法）。部署文件回退：停止服务，恢复同步前备份；新文件按包 manifest 核对移除，再按原方式启动。

本次只接受了既有 7 张图的效果，没有证明新批次的泛化准确率，V4 已知的 D6 右侧/下侧边界偏差仍存在。不要把圈画对照的 IoU 当作独立准确率。

# 可选跨视野去重与逐图候选

`task.detect.deduplication.enabled` 为严格 JSON 布尔值，省略时为 `false`。
适用于所有板型、物镜和检测入口，不按板型自动启用，也不因 `calibrated=true` 自动开启。
扫描点位、拍照、识别、标注图、候选选择和补偿坐标计算保持原逻辑。

## 默认逐图识别

```json
"detect": {
  "entrypoint": "vision.vision.detect_pipeline:process_image",
  "save_overlay": true,
  "overlay_source": "vision",
  "deduplication": {"enabled": false}
}
```

仅跳过去重标定检查及跨图合并，其他检测预检仍执行。相同克隆可能重复出现。
识别、候选文件写入失败仍按原任务失败规则处理。

完整检测结果 `schema_version=3`，`total_image_clone_count` 为全部逐图正式观测之和；
`unique_clone_count`、`total_clone_count`、`unique_clones` 均为 `null`，表示未计算唯一结果。
`deduplication.enabled=false`、`status="not_performed"`，每条观测的
`source_detection_id` 为 `I<图号>:<局部克隆编号>`，`global_clone_id=null`。
待复核候选不因此提升为正式可选克隆。

始终生成 `pickable_detect_result.json`（既有纯内存调用无输出路径的情况除外），包括零候选情况。
默认分支候选文件为 **schema_version=4**：

- 保留全部照片的原 `index`、图片/标注图/预览图路径、台位坐标和 `mm_per_pixel`；保留 `point_name`。
- 每张图导出 `is_pickable` 严格为 `true` 的候选，保留原 `clone_id`、`center_px`、`offset_from_image_center_px`。
- `global_clone_id=null`，`source_detection_id` 只标识观测，不保证物理目标唯一。
- `candidate_count` 和 `total_clone_count` 都表示导出的可选条目数，可能重复；
  `count_semantics="per_image_candidates"`、`unique_clone_count=null`。
- `deduplication={"enabled":false,"status":"not_performed"}`。
- 图片级 `is_pickable` 表示该图片是否有候选，无候选时仍保留所有图片和空 `clones`。

后端需要接受候选版本 4 及可空的 `global_clone_id`。显示“候选条目数”，不能显示为唯一克隆数。
读取文件路径和现有下载接口不变。选择动作使用 `image_index + clone_id`：

```json
"selector": {"mode":"image_and_clone","purpose":"pick","image_index":3,"clone_id":"C01"}
```

单次补偿使用所选照片的原台位、像素比例和目标偏移，不依赖全局 ID。
不要将未去重候选直接用于自动遍历所有物理克隆；不同图可能包含同一目标。

## 显式开启

```json
"deduplication": {
  "enabled": true,
  "calibrated": true,
  "registration_tolerance_mm": 0.10,
  "intersection_over_min_threshold": 0.50
}
```

`0.10` 仅为字段示例，必须替换为经过验证的容差。
开启后按原规则检查实际重叠与标定，执行原跨图合并和代表选择。
完整检测结果保持版本 2，候选文件保持版本 3，字段含义与原去重结果一致。
历史已去重文件仍可用离线导出器处理；缺少去重分组且没有明确关闭标记的文件仍拒绝导出，
不会把缺失数据猜作逐图模式。

升级时所有原先需要去重的请求都必须补 `enabled:true`，即使已有 `calibrated:true`。
仅加入请求字段不能改变旧工控机代码，必须同步更新工作流。

## 软件验收（2026-10-28）

- 全套测试通过：720 项及 96 项子测试。随后补充未去重模式的文件/内联补偿输入、空候选图片保留、候选下载接口验证，候选文件测试共 25 项通过。
- 使用现场 `48B3_TEST_1028` 的五张原始 BMP 和对应采集结果重新执行规则识别，保留五张照片，标注图及预览图全部存在，导出三条候选。
- 三条候选通过原图号和局部 ID 选择后的补偿坐标，与完整检测结果计算值逐项相同。另以模拟运动验证每次请求仅执行一次补偿。
- 本次为本地软件验收，未连接设备、未部署工控机，也未验证外部后端页面。后端若严格限制版本或全局 ID 类型，需要接受版本 4 和 `global_clone_id=null`。
- 三条候选是规则算法的输出，不构成真实克隆身份或识别准确率的验收。

本地复验产物在 `outputs/optional_deduplication/`：全套测试报告 `pytest.xml`，补充测试报告 `pickable-final.xml`，实拍图验证结果 `real_B3/acceptance.json` 及候选文件 `real_B3/pickable_detect_result.json`。这些产物不随源码提交。

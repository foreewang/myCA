# 规则视觉默认GPU：配置与实现

2026-09-09按用户要求，将规则视觉默认后端改为 `cuda`，使用本地ca环境和RTX 500 Ada验证。入口仍为 `vision.vision.detect_pipeline:process_image`。

## 默认调用

```python
from vision.vision.detect_pipeline import process_image

result = process_image("sample.bmp", out_dir="outputs/sample")
assert result["texture_processing"]["coarse_backend"] == "cuda"
```

命令行使用 `python -m vision.run_detect sample.bmp --backend legacy` 即执行规则GPU路径。这里legacy表示既有规则入口；CLI整体默认model路由仍沿用原配置。

工作流中保留规则入口即可默认GPU；如果已有任务显式配置 `texture_backend: cpu`，应改成cuda或移除该字段：

```json
{
  "detect": {
    "entrypoint": "vision.vision.detect_pipeline:process_image",
    "texture_backend": "cuda"
  }
}
```

## 本次代码调整

- `gpu_ops.py` 定义唯一默认常量 `DEFAULT_TEXTURE_BACKEND = "cuda"`。
- `detect_pipeline.py`、`segment.py`、`texture_segment.py`、`preprocess.py`的默认参数均引用该常量，保证整图粗检与ROI细化一致。
- GPU计算已有的局部均值、平方均值、标准差与密度均值滤波，连续运行后一次同步回传。没有修改分割阈值、形状条件、坐标与有效性规则。
- CLI帮助信息同步更新。已有工作流规则参数透传继续生效，模型provider字段不会冒充规则后端设置。
- Windows默认临时目录含非ASCII字符时，在CuPy编译前为当前进程设置 `CUPY_CACHE_IN_MEMORY=1`，规避本机NVRTC无法打开中文路径源文件的问题。不修改TEMP/TMP或系统环境，尊重用户已有CUPY_CACHE_IN_MEMORY设置。内存缓存可在进程内复用，跨进程会重新初始化/编译。

读取/解码、缩图、Otsu、形态学、连通域、轮廓、距离变换和输出仍在CPU运行。本次是默认启用已有GPU纹理计算，不是全流水线GPU化。

## 后端与故障行为

| texture_backend | 行为 |
|---|---|
| cuda（默认） | 必须成功执行GPU；缺少CuPy、CUDA设备或编译失败时明确报错 |
| auto | 尝试GPU，失败则记录原因并回退CPU；同进程后续不重复尝试已失败后端 |
| cpu | 显式使用CPU |

诊断入口：`vision.vision.gpu_ops.backend_status()`。JSON顶层texture_processing记录粗检后端，component记录各目标处理后端；零候选时仍可检查顶层后端。

## 环境和性能

ca中已有CuPy14.2.0及可用CUDA运行时，本次无需安装或升级。其他环境可参考 `requirements-vision-texture-gpu.txt` 安装对应CuPy包，并先确认CUDA运行时兼容。现有ONNX依赖锁未调整。

前次本机15张B3、预热后含05/06/07输出的中位耗时：新版CPU5.04秒，新版CUDA5.39秒；GPU纹理算子更快，但这组完整流水线没有优于新版CPU。因此默认GPU表示按用户偏好选择计算后端，不意味着端到端已经获得额外加速。后续加速需要分阶段测量，评估更多算子驻留GPU的收益，避免增加传输与同步成本。

普通输出/算法契约测试显式使用CPU，以兼容无GPU的测试机；设置 `IPSC_TEST_CUDA=1` 时另执行真实GPU一致性测试，并验证省略后端参数仍使用cuda。默认参数一致性、GPU故障不静默回退、中文临时路径兼容均有回归测试。

本次ca完整回归：353项测试与32个subtests通过。未修改TEMP/TMP、不传texture_backend直接处理B3_005，顶层和目标后端均为cuda，设备为RTX 500 Ada，回退次数0。实测记录保存在 `outputs_ipsc_texture_20260908/default_gpu_smoke_20260909.json`。

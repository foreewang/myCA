# 工控机无 Git 同步说明

本次交付为增量包，适用于已有完整项目的工控机，不是独立安装包。包内 `payload/` 保留项目相对目录，包含本次已修改和新增文件；`manifest.json` 记录基线版本及新旧文件 SHA256；`verify_sync.py` 无第三方依赖，工控机无需 Git。

## 本次变更

- 规则视觉改为连通纹理检测，新增 CuPy GPU纹理算子，默认后端cuda；显式auto可回退CPU。
- 纹理支持区域细化轮廓，依据最终轮廓及边界余量选点；扫描、闭环补拍透传纹理设置。
- 删除无效暗核、径向参数和旧辅助函数；删除旧暗核空字段、seed_thresh诊断链及无效规则mm_per_pixel传递。比例尺和工作流坐标换算继续有效。
- 增加纹理/接口/输出测试，性能基准、CPU/GPU结果比较和分阶段计时工具，以及相关文档。

本机最近一次完整回归：372项测试、32个子测试通过，包含真实CUDA；CuPy有一条CUDA路径探测警告但执行成功。该结果不替代工控机环境、识别准确率和真实设备验收。

已发现但本包未处理的4个其他模块无用参数：模型输出 `_save_outputs.src_path`、补偿辅助函数 `_image_center_distance2.image_item`、自动对焦 `execute_autofocus_for_task.ctx`、物镜切换 `ensure_objective_for_task.extra_context`。`dark_fraction` 仍参与模型图像质量检查。

## 同步步骤（Linux示例）

以下路径为示例，按现场实际目录调整。先将ZIP解压到独立目录，例如 `/tmp/colony_sync`，不要直接解压覆盖运行目录。

1. 检查包内文件完整性及工控机待替换文件的基线：

   ```bash
   python3 /tmp/colony_sync/verify_sync.py --target /opt/colony_system --mode before
   ```

   `PASS` 表示包文件完整，目标对应文件是基线版本、待新增文件不存在，或已是包内版本。`MISMATCH` 表示有现场修改或版本不同，需先核对差异，不能直接覆盖。此检查仅覆盖增量包文件，不能证明其余项目文件完全相同。

2. 保留当前项目完整备份，建立独立测试副本，将 `payload/` 的内容合并进测试副本的项目根目录。例如：

   ```bash
   cp -a /tmp/colony_sync/payload/. /opt/colony_system_candidate/
   python3 /tmp/colony_sync/verify_sync.py --target /opt/colony_system_candidate --mode after
   ```

   测试副本必须预先包含完整项目。Python虚拟环境含绝对路径，复制目录后不要假定副本中的 `.venv` 可用；明确选择测试解释器，或新建测试环境。

3. 在实际测试Python环境检查CuPy/CUDA。规则纹理依赖见 `requirements-vision-texture-gpu.txt`，现有ONNX GPU依赖文件不包含CuPy。该文件仅锁定本机实测CuPy版本，工控机的驱动、CUDA运行时/NVRTC仍需验证；不要用开发依赖文件重装现场完整环境。若需安装依赖，先在独立测试环境验证并保存原环境依赖记录。

4. 从测试项目根目录运行已有图片测试，不启动任务执行或设备运动：

   ```bash
   python -m vision.run_detect /path/to/sample.bmp --backend legacy --texture-backend cuda --out-dir /path/to/new_result
   IPSC_TEST_CUDA=1 python -m pytest tests -q
   ```

   `python` 必须是已确认的测试解释器；测试需要pytest等测试依赖。检查JSON中 `texture_processing.coarse_backend` 为cuda，核对轮廓、定位点、失败记录及耗时。模型provider字段不控制规则后端。

5. 软件发布门禁按原部署流程运行：`IPSC_TEST_CUDA=1 python tools/release_gate.py`。完整pytest通过不等同于发布门禁通过；后者还检查依赖锁和部署环境。工控机门禁结果需现场记录。

6. 离线验证通过后，在无活动任务时停止API服务，将相同payload合并到实际项目，再执行after校验及原部署预检，按原启动方式重启服务并验证后端接口。随后再安排设备联调。仓库提供的服务文件名为 `colony-system-api.service`，现场服务名以实际安装为准。

## 同步范围与回退

以包内manifest为完整清单。包不包含 `.venv`、现场config、data、logs、模型权重、相机SDK或本机图像测试产物，不会通过payload覆盖这些内容。不要直接照旧的性能文档只同步5个运行文件，本次还需要新增GPU和纹理分割模块。

本包没有删除文件操作。回退时停止服务，从同步前完整备份恢复被替换文件，并移走manifest中标记为新增的文件；如修改过依赖环境，也须恢复原环境。不要将增量包本身当作备份。

# 当前规则算法逐图、逐步骤计时

`tools/profile_rule_vision.py`串行调用真正的 `vision.vision.detect_pipeline:process_image`，仅在本测试进程中临时包装计时函数，结束后恢复；不修改生产算法或检测JSON。独立测试已验证计时前后JSON、05/06图像一致，以及嵌套时间不重复累加。

## 命令

```powershell
& 'C:\miniforge3\envs\ca\python.exe' `
  'C:\colony_system.worktrees\deploy-linux-20260902\tools\profile_rule_vision.py' `
  --images 'C:\Users\王海成\Desktop\pipeline_B3_legacy_20260907_002\B3\images' `
  --out 'C:\Users\王海成\Desktop\pipeline_B3_legacy_20260907_002\B3' `
  --repeats 2 --warmup 0
```

未指定backend时测试当前算法默认值（本次为cuda）。`--backend cpu|cuda|auto`可显式选择后端。`--repeats 2 --warmup 0`表示不做额外预热，运行两轮：第一轮包含进程首次CUDA初始化/编译，第二轮复用同一进程，均包含实际输出。只测试一轮用 `--repeats 1`；仅考察预热后行为可用 `--warmup 1 --repeats 5`。第一次运行遇到不同ROI形状也可能产生新内核编译，因此预热首图不保证所有ROI内核都已编译。

输入目录必须存在；支持bmp/png/jpg/jpeg/tif/tiff，只处理目录直接包含的图片。输出父目录下自动创建独立时间戳子目录，不覆盖既有测试。正式图片均写05/06/07，传 `--save-debug`时额外写01–04；第二轮复用本次新建的逐图输出目录，图片代表最后一轮，计时保留所有轮次。

## 输出和计时定义

- `stage_timing.md`：中文步骤定义及各轮汇总。
- `per_image_timing.csv`：每轮每张图片的全部步骤及总时间，UTF-8 BOM，适合Excel打开。
- `stage_timing.json`：环境、源码SHA256、实际GPU后端、每张图及每次被包装函数的计时。
- `images/<图片名_扩展名>/`：该图片的05/06/07。

各步骤使用排除子步骤后的墙钟时间，所有步骤可相加还原每张图的总时间。多个ROI的同一阶段累计记录。GPU纹理阶段包含数据上传、计算、同步回传，首调用还包含CuPy导入、CUDA上下文及编译；不是纯CUDA kernel时间。

“流水线其他”包括未单独包装的扩框裁ROI、全图缓冲区分配/合并和计时开销，不能解读为单独ROI裁剪时间。“ROI轮廓”包括候选支持区域关联、连通域和内部质量检查、轮廓提取及掩膜还原。默认GrabCut和调试输出关闭，耗时0；无粗候选图片的ROI相关步骤为0。

`image_total_s`为全部图片process_image调用之和。`script_before_report_s`为进入main后到最终报告写出前的时间，还包含算法导入、准备、预热和循环日志；不含解释器启动、工具顶层标准库导入及报告自身写出。文件写盘时间指编码/文件操作返回，不表示操作系统已将所有缓存物理落盘。操作系统页缓存未清空。

## 2026-09-09本次结果

实际目录为桌面 `pipeline_B3_legacy_20260907_002/B3/images`，不是将消息中 `\_` 当成路径分隔后的多层目录。共15张5120×5120 BMP，在ca和RTX 500 Ada上运行2轮，30次调用均cuda，无报错、无回退。

首轮图片调用合计13.044847秒；第二轮6.164394秒；两轮19.209240秒；main流程到报告写出前19.453602秒。每轮9个候选记录、2个成功细化/可挑取、7个拒绝记录，仍不构成人工标注准确率验收。

工件目录：`C:\Users\王海成\Desktop\pipeline_B3_legacy_20260907_002\B3\vision_stage_timing_20260909_105636_240321`。该目录还附 `test_command.ps1` 方便再次执行。

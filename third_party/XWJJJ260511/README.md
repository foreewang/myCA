# XWJJJ260511

这是一个完整的新版本目录，真实测试需要用到的自动对焦、相机、电机、Modbus 文件都在这里。以后主要改 `config.yaml`，不用再打一长串命令，也不用依赖外层旧版本的业务代码。

## 安装依赖

先进入这个目录，再安装依赖：

```powershell
cd XWJJJ260511
pip install -r requirements.txt
```

## 直接运行

如果已经 `cd` 到 `XWJJJ260511` 目录，默认读取 `config.yaml`：

```powershell
python run.py
```

也可以用批处理入口：

```powershell
.\run.bat
```

也可以指定配置文件：

```powershell
python run.py -c config_real_example.yaml
```

外部程序只知道当前倍镜时，把倍镜传进来即可，调焦范围由 YAML 里的 `motor.objective_ranges` 维护：

```powershell
python run.py --objective 4x
python run.py --objective 10x
```

如果用 `run.bat`，可以这样指定：

```powershell
.\run.bat -c config_real_example.yaml
```

临时只预览相机，不移动电机：

```powershell
python run.py --preview
```

如果你站在 `XWJJJ260511` 的上一级目录，也可以运行：

```powershell
python -m XWJJJ260511
```

## 给别的 Python 调用

把整个 `XWJJJ260511` 文件夹发给对方后，推荐让对方把它当成一个 Python 包来调用。对方只传当前倍镜，调焦范围仍然由你维护在 `config.yaml` 或指定的 YAML 里：

```python
from XWJJJ260511 import run_autofocus

result = run_autofocus(
    "config.yaml",
    objective="10x",
)

print(result.best_pos)
print(result.best_value)
print(result.output_path)
```

如果用真实电机示例配置文件，就把配置文件名换成 `config_real_example.yaml`：

```python
from XWJJJ260511 import run_autofocus

result = run_autofocus(
    "config_real_example.yaml",
    objective="4x",
)
```

不推荐让对方用 `sys.argv + main()` 当函数接口；那种写法只是模拟命令行。如果确实要这么调用，也必须把倍镜参数一起放进去：

```python
import sys

from XWJJJ260511.run import main

sys.argv = [
    "run.py",
    "-c",
    "config_real_example.yaml",
    "--objective",
    "10x",
]
main()
```

也可以直接调用 `autofocus_api.py`。这种方式不读 YAML，所以需要把倍镜范围映射一起传进去：

```python
from XWJJJ260511.autofocus_api import run_realtime_autofocus

result = run_realtime_autofocus(
    use_modbus_motor=True,
    motor_port="COM3",
    motor_baudrate=115200,
    focus_slave=3,
    objective="10x",
    focus_ranges={
        "4x": {"min_pos": -2063120, "max_pos": -1769500},
        "10x": {"min_pos": -2095551, "max_pos": -2028750},
    },
    profile_vel=50000,
    profile_acc=50000,
    profile_dec=50000,
    tol=100,
    max_iter=10,
    settle_ms=300,
    center_roi=0.6,
    downsample=0.5,
    output_path="XWJJJ260511/output/from_python.png",
    use_mvs=False,
    camera_index=0,
)

print(result.best_pos)
print(result.best_value)
print(result.output_path)
```

返回值 `result` 里常用字段：

- `result.best_pos`：最佳焦距位置，电机最后会停在这里。
- `result.best_value`：最佳清晰度分数。
- `result.frame`：最佳位置重新采集的一帧 BGR 图像。
- `result.focus_log`：搜索过程列表，每项是 `(位置, 清晰度)`。
- `result.output_path`：保存的图片路径。
- `result.elapsed_sec`：本次自动对焦耗时。

## 输出位置

输出位置在 YAML 里改：

```yaml
output:
  timestamp_folder: true
  image_path: output/sharpest.png
  log_path: output/focus_log.csv
```

开启 `timestamp_folder` 后，每次运行会按时间创建一个文件夹，例如：

- `XWJJJ260511\output\20260512_101530\sharpest.png`
- `XWJJJ260511\output\20260512_101530\focus_log.csv`

## 切到真实电机

把 `config.yaml` 里的电机类型改成：

```yaml
motor:
  type: modbus
```

然后按实际设备修改：

```yaml
port: COM3
baudrate: 115200
focus_slave: 3
objective: 4x
objective_ranges:
  4x:
    min_pos: -2063120
    max_pos: -1769500
  10x:
    min_pos: -2095551
    max_pos: -2028750
profile_vel: 50000
profile_acc: 50000
profile_dec: 50000
```

第一次真实测试先用小范围、低速度。每个倍镜下面的 `min_pos` 和 `max_pos` 是自动对焦搜索范围，也是软件限位。旧写法仍然兼容：如果没有配置 `objective_ranges`，程序会继续读取 `motor.min_pos/max_pos`。

## 相机配置

OpenCV 摄像头：

```yaml
camera:
  backend: opencv
  opencv_index: 0
```

海康 MVS：

```yaml
camera:
  backend: mvs
  ip: 192.168.1.253
  net_export_ip: 192.168.1.168
  mvs_sdk_path: D:/app/mvs/MVS/Development/Samples/Python/MvImport
```

`ip` 是相机 IP，`net_export_ip` 是连接相机的电脑有线网卡 IP。只调相机时建议先运行 `python run.py --preview`，这样不会进入电机自动对焦流程。

曝光控制：

```yaml
camera:
  backend: mvs
  exposure_auto: false
  exposure_time_us: 8000
```

`exposure_time_us` 的单位是微秒。只要设置了 `exposure_time_us`，程序会自动关闭海康相机的自动曝光并写入手动曝光时间；不设置时沿用相机当前状态。四倍镜和十倍镜亮度不一样时，推荐按倍镜分别配置：

```yaml
camera:
  backend: mvs
  objective_settings:
    4x:
      exposure_auto: false
      exposure_time_us: 8000
    10x:
      exposure_auto: false
      exposure_time_us: 12000
```

具体数值需要用 `python run.py --preview --objective 4x` 和 `python run.py --preview --objective 10x` 现场看画面亮度与清晰度曲线来定。原则是不过曝、不太暗，并且调焦过程中曝光保持固定。

## 新版本文件

- `run.py` / `__main__.py`：YAML 运行入口。
- `config.yaml`：默认安全配置，虚拟电机。
- `config_real_example.yaml`：真实 Modbus 电机示例配置。
- `autofocus_api.py` / `focus.py`：自动对焦流程和清晰度算法。
- `hardware/`：相机、电机、Modbus 电机适配器。
- `modbus.py` / `MotorManager.py`：真实电机底层 Modbus 控制。
- `requirements.txt`：这个版本需要安装的依赖。

"""从 YAML 配置文件启动实时自动对焦流程。"""

# 允许类型标注延迟解析，避免脚本直接运行时出现包名解析问题。
from __future__ import annotations

# argparse 负责解析命令行参数，例如 --preview、--camera-test。
import argparse
# csv 用来保存自动对焦过程日志。
import csv
# sys 用来调整模块搜索路径，使 python run.py 也能运行包内相对导入。
import sys
# datetime 用来给每次输出创建时间命名的文件夹。
from datetime import datetime
# Path 用来安全处理 Windows/Unix 路径。
from pathlib import Path
from collections.abc import Mapping
# Any 用于配置字典和硬件对象的类型标注。
from typing import Any


# 当前包目录，也就是 XWJJJ260511 目录。
PACKAGE_DIR = Path(__file__).resolve().parent
# 项目上一级目录，用于让 python run.py 能找到 XWJJJ260511 包。
PROJECT_ROOT = PACKAGE_DIR.parent
# 默认配置文件。
DEFAULT_CONFIG = PACKAGE_DIR / "config.yaml"

# 把项目上一级目录加入 sys.path，支持从包目录里直接 python run.py。
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

# 直接运行 run.py 时 __package__ 为空；这里补成包名，支持相对导入。
if __package__ in (None, ""):
    __package__ = PACKAGE_DIR.name


def main() -> None:
    # 创建命令行解析器。
    parser = argparse.ArgumentParser(description="XWJJJ260511 YAML 实时自动对焦入口")
    # -c/--config 允许临时指定配置文件。
    parser.add_argument(
        "-c",
        "--config",
        default=str(DEFAULT_CONFIG),
        help="YAML 配置文件路径，默认 config.yaml",
    )
    # 外部程序只知道当前是几倍镜时，可以通过这个参数选择对应的调焦范围。
    parser.add_argument(
        "--objective",
        "--magnification",
        dest="objective",
        help="当前倍镜，例如 4x、10x、4、10。会从 motor.objective_ranges 里选择调焦范围。",
    )
    # --preview 只显示相机画面和清晰度，不进入自动对焦。
    parser.add_argument(
        "--preview",
        action="store_true",
        help="临时覆盖 YAML：只预览相机，不移动电机",
    )
    # --camera-test 只抓一张相机测试图，不移动电机。
    parser.add_argument(
        "--camera-test",
        action="store_true",
        help="只连接相机抓一张测试图，不移动电机",
    )
    # --motor-status 只读电机当前位置，不移动电机。
    parser.add_argument(
        "--motor-status",
        action="store_true",
        help="只读取电机当前位置和配置范围，不移动电机",
    )
    # 解析用户在命令行输入的参数。
    args = parser.parse_args()

    # 把配置路径解析成绝对路径或包内路径。
    config_path = _resolve_config_path(args.config)
    # 读取 YAML 配置，得到 Python 字典。
    cfg = _load_yaml_config(config_path)
    # 命令行指定倍镜时覆盖 YAML 里的默认倍镜。
    if args.objective:
        motor_cfg = _section(cfg, "motor")
        cfg["motor"] = motor_cfg
        motor_cfg["objective"] = args.objective
    # 命令行 --preview 优先级高于 YAML，临时覆盖 preview。
    if args.preview:
        cfg.setdefault("mode", {})["preview"] = True

    # 在真正连接硬件前打印本次配置摘要，方便确认是否会动电机。
    _print_config_summary(config_path, cfg)

    # 只读电机状态时，不创建相机，也不进入自动对焦。
    if args.motor_status:
        _run_motor_status(cfg)
        return

    try:
        # 根据 camera 配置创建相机对象。
        camera = _create_camera(cfg)
        try:
            # 相机测试模式只抓图保存，不移动电机。
            if args.camera_test:
                _run_camera_test(camera, cfg)
                return

            # 预览模式会循环显示画面，直到按 q。
            if _get_bool(cfg, ("mode", "preview"), False):
                _run_preview(camera, cfg)
                return

            # 正常模式：移动电机、采图、计算清晰度并搜索最佳焦点。
            result = _run_autofocus(camera, cfg)
            # 把搜索过程写成 CSV，便于分析曲线。
            _save_focus_log(result.focus_log, _get_output_path(cfg, "log_path"), cfg)

            # 打印本次自动对焦结果。
            run_info = _get_run_log_info(cfg)
            print("\n自动对焦完成")
            print(f"当前倍镜: {run_info['objective']}")
            print(f"曝光时间: {run_info['exposure']}")
            print(
                f"调焦范围: {run_info['min_pos']:.0f} 到 {run_info['max_pos']:.0f} "
                f"({run_info['range_label']})"
            )
            print(f"最佳焦距位置: {result.best_pos:.2f}")
            print(f"最佳清晰度分数: {result.best_value:.2f}")
            print(f"耗时: {result.elapsed_sec:.2f}s")
            print(f"最清晰图片: {_get_output_path(cfg, 'image_path')}")
            print(f"搜索记录: {_get_output_path(cfg, 'log_path')}")
        finally:
            # 无论成功还是异常，都关闭相机资源。
            camera.close()
    except ModuleNotFoundError as exc:
        # 某些依赖缺失时，给出更明确的安装提示。
        missing = exc.name or "依赖库"
        raise SystemExit(
            f"缺少依赖 {missing}。请在项目根目录运行：pip install -r requirements.txt"
        ) from exc


def run_autofocus_from_config(
    config_path: str | Path | None = None,
    *,
    objective: Any = None,
):
    """给外部 Python 调用的 YAML 入口；只需额外传当前倍镜即可。"""

    path = _resolve_config_path(config_path or DEFAULT_CONFIG)
    cfg = _load_yaml_config(path)
    if objective is not None:
        motor_cfg = _section(cfg, "motor")
        cfg["motor"] = motor_cfg
        motor_cfg["objective"] = objective

    camera = _create_camera(cfg)
    try:
        result = _run_autofocus(camera, cfg)
        _save_focus_log(result.focus_log, _get_output_path(cfg, "log_path"), cfg)
        return result
    finally:
        camera.close()


def _run_autofocus(camera: Any, cfg: dict[str, Any]):
    # 延迟导入，避免只读配置或帮助信息时加载全部硬件依赖。
    from .autofocus_api import run_realtime_autofocus
    from .hardware import VirtualMotor

    # 取出 motor 和 focus 两段配置。
    motor_cfg = _section(cfg, "motor")
    focus_cfg = _section(cfg, "focus")
    # 电机类型可以是 virtual 或 modbus。
    motor_type = str(motor_cfg.get("type", "virtual")).lower()
    # 根据当前倍镜选择自动对焦搜索范围；没有倍镜映射时兼容旧的 min_pos/max_pos。
    min_pos, max_pos, range_label = _resolve_focus_range(motor_cfg)

    # 下限必须小于上限，否则搜索区间无效。
    if min_pos >= max_pos:
        raise ValueError(f"配置错误：{range_label}.min_pos 必须小于 {range_label}.max_pos")

    # virtual 模式不控制真实硬件。
    if motor_type == "virtual":
        motor = VirtualMotor(min_pos=min_pos, max_pos=max_pos)
        use_modbus_motor = False
        print("电机模式: virtual，不会控制真实硬件。")
    # modbus 模式由 autofocus_api 内部创建真实电机对象。
    elif motor_type == "modbus":
        motor = None
        use_modbus_motor = True
        print("电机模式: modbus，将控制真实聚焦电机。")
    else:
        raise ValueError("配置错误：motor.type 只能是 virtual 或 modbus")

    # 计算本次最清晰图片输出路径。
    image_path = _get_output_path(cfg, "image_path")
    # 提前创建输出目录。
    image_path.parent.mkdir(parents=True, exist_ok=True)

    # 把 YAML 配置展开为自动对焦 API 参数。
    return run_realtime_autofocus(
        motor=motor,
        camera=camera,
        use_modbus_motor=use_modbus_motor,
        motor_port=str(motor_cfg.get("port", "COM3")),
        motor_baudrate=int(motor_cfg.get("baudrate", 115200)),
        focus_slave=int(motor_cfg.get("focus_slave", 3)),
        min_pos=min_pos,
        max_pos=max_pos,
        profile_vel=int(motor_cfg.get("profile_vel", 50000)),
        profile_acc=int(motor_cfg.get("profile_acc", 50000)),
        profile_dec=int(motor_cfg.get("profile_dec", 50000)),
        tol=float(focus_cfg.get("tol", 100)),
        max_iter=int(focus_cfg.get("max_iter", 10)),
        settle_ms=float(focus_cfg.get("settle_ms", 300)),
        center_roi=float(focus_cfg.get("center_roi", 0.6)),
        downsample=float(focus_cfg.get("downsample", 0.5)),
        output_path=str(image_path),
        use_mvs=_camera_uses_mvs(cfg),
        camera_index=int(_section(cfg, "camera").get("opencv_index", 0)),
    )


def _create_camera(cfg: Mapping[str, Any]) -> Any:
    # 延迟导入，只有需要相机时才加载 MVS/OpenCV 相关代码。
    from .hardware.hikrobot_camera import HikrobotCamera

    # 取出 camera 配置段。
    camera_cfg = _section(cfg, "camera")
    motor_cfg = _section(cfg, "motor")
    camera_settings, _ = _resolve_camera_settings(camera_cfg, motor_cfg)
    # backend 决定使用 OpenCV 还是海康 MVS。
    backend = str(camera_settings.get("backend", "opencv")).lower()
    # 目前只支持这两种相机后端。
    if backend not in {"opencv", "mvs"}:
        raise ValueError("配置错误：camera.backend 只能是 opencv 或 mvs")

    # 创建统一相机对象，后续代码只调用 capture()。
    return HikrobotCamera(
        device=camera_settings.get("ip"),
        use_mvs=backend == "mvs",
        opencv_index=int(camera_settings.get("opencv_index", 0)),
        net_export_ip=camera_settings.get("net_export_ip"),
        mvs_sdk_path=camera_settings.get("mvs_python_dir") or camera_settings.get("mvs_sdk_path"),
        exposure_auto=_optional_bool(camera_settings, "exposure_auto", "camera.exposure_auto"),
        exposure_time_us=_optional_float(
            camera_settings, "exposure_time_us", "camera.exposure_time_us"
        ),
    )


def _run_preview(camera: Any, cfg: Mapping[str, Any]) -> None:
    # 预览模式需要 OpenCV 窗口显示。
    import cv2

    # 清晰度指标显示在预览窗口上。
    from .focus import compute_focus_metric

    # 读取清晰度算法参数。
    focus_cfg = _section(cfg, "focus")
    center_roi = float(focus_cfg.get("center_roi", 0.6))
    roi = None if center_roi <= 0 else min(1.0, center_roi)
    downsample = float(focus_cfg.get("downsample", 0.5))

    print("预览模式。按 q 退出。")
    while True:
        # 取当前帧。
        frame = camera.capture()
        # 计算当前帧清晰度。
        value = compute_focus_metric(frame, center_roi=roi, downsample=downsample)
        # 把清晰度文字画到图像左上角。
        cv2.putText(
            frame,
            f"Laplacian var: {value:.1f}",
            (20, 40),
            cv2.FONT_HERSHEY_SIMPLEX,
            1,
            (0, 255, 0),
            2,
        )
        # 显示预览窗口。
        cv2.imshow("XWJJJ260511 Preview (q to quit)", frame)
        # 用户按 q 时退出预览。
        if cv2.waitKey(1) & 0xFF == ord("q"):
            break
    # 关闭所有 OpenCV 窗口。
    cv2.destroyAllWindows()


def _run_camera_test(camera: Any, cfg: Mapping[str, Any]) -> None:
    # 相机测试模式只需要保存一张测试图。
    import cv2

    # 测试图和正式输出一样放入本次时间目录。
    output_path = _get_output_path(cfg, "image_path").with_name("camera_test.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # 从相机取一帧。
    frame = camera.capture()
    # 防御性检查，避免保存空图。
    if frame is None or frame.size == 0:
        raise RuntimeError("相机测试失败：没有取到有效图像")

    # 保存测试图到磁盘。
    ok = cv2.imwrite(str(output_path), frame)
    if not ok:
        raise RuntimeError(f"相机测试失败：保存图片失败 {output_path}")

    print("相机测试完成")
    print(f"图像尺寸: {frame.shape[1]}x{frame.shape[0]}")
    print(f"测试图片: {output_path}")


def _run_motor_status(cfg: Mapping[str, Any]) -> None:
    # 只读电机状态时，需要真实 Modbus 电机对象。
    from .hardware.modbus_motor import ModbusFocusMotor

    # 读取电机配置。
    motor_cfg = _section(cfg, "motor")
    min_pos, max_pos, range_label = _resolve_focus_range(motor_cfg)

    # 创建电机对象；后面只读当前位置，不发送移动命令。
    motor = ModbusFocusMotor(
        port=str(motor_cfg.get("port", "COM3")),
        baudrate=int(motor_cfg.get("baudrate", 115200)),
        slave=int(motor_cfg.get("focus_slave", 3)),
        min_pos=min_pos,
        max_pos=max_pos,
        profile_vel=int(motor_cfg.get("profile_vel", 50000)),
        profile_acc=int(motor_cfg.get("profile_acc", 50000)),
        profile_dec=int(motor_cfg.get("profile_dec", 50000)),
    )
    try:
        # 读取驱动器反馈的当前位置。
        current_pos = motor.get_position()
        # 读取当前配置里的软件搜索范围。
        safe_min, safe_max = motor.get_range()
        print("电机状态读取完成")
        print(f"当前位置: {current_pos:.0f}")
        print(f"当前配置搜索范围: {safe_min:.0f} 到 {safe_max:.0f} ({range_label})")
        print("注意：这不是机械全行程，只是 config.yaml 里设置的软件搜索范围。")
    finally:
        # 读完状态后关闭串口连接。
        motor.close()


def _load_yaml_config(path: Path) -> dict[str, Any]:
    try:
        # PyYAML 不是标准库，缺失时给出安装提示。
        import yaml
    except ImportError as exc:
        raise RuntimeError(
            "缺少 PyYAML。请先运行：pip install -r requirements.txt"
        ) from exc

    # 配置文件不存在时直接报错。
    if not path.exists():
        raise FileNotFoundError(f"配置文件不存在: {path}")

    # 按 UTF-8 读取 YAML。
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    # 顶层必须是字典，方便按 section 读取。
    if not isinstance(data, dict):
        raise ValueError(f"配置文件格式错误，顶层必须是 YAML 字典: {path}")
    return data


def _save_focus_log(focus_log, path: Path, cfg: Mapping[str, Any] | None = None) -> None:
    # 确保日志目录存在。
    path.parent.mkdir(parents=True, exist_ok=True)
    # newline="" 避免 Windows 下 CSV 出现空行。
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if cfg is not None:
            run_info = _get_run_log_info(cfg)
            writer.writerow(["当前倍镜", run_info["objective"]])
            writer.writerow(["曝光时间", run_info["exposure"]])
            writer.writerow(["最小调焦范围", f"{run_info['min_pos']:.0f}"])
            writer.writerow(["最大调焦范围", f"{run_info['max_pos']:.0f}"])
            writer.writerow(["范围来源", run_info["range_label"]])
            writer.writerow([])
        # 写表头。
        writer.writerow(["序号", "位置", "清晰度"])
        # 逐行写入每个采样位置和对应清晰度。
        for idx, (pos, value) in enumerate(focus_log, 1):
            writer.writerow([idx, f"{pos:.4f}", f"{value:.4f}"])


def _get_output_path(cfg: Mapping[str, Any], key: str) -> Path:
    # 读取 output 配置段。
    output_cfg = _section(cfg, "output")
    # 如果配置里没写路径，就使用默认文件名。
    default_name = "sharpest.png" if key == "image_path" else "focus_log.csv"
    # 取配置路径或默认路径。
    value = str(output_cfg.get(key, PACKAGE_DIR / "output" / default_name))
    # 把相对路径解析到包目录下。
    path = _resolve_output_path(value)
    # 如果关闭时间文件夹，就直接返回配置路径。
    if not _output_uses_timestamp_folder(output_cfg):
        return path
    # 默认开启时间文件夹，同一次运行复用同一个目录。
    return _get_output_run_dir(output_cfg, path.parent) / path.name


def _resolve_config_path(value: str | Path) -> Path:
    # 先把字符串变成 Path。
    path = Path(value)
    # 绝对路径直接使用。
    if path.is_absolute():
        return path
    # 相对路径优先按当前工作目录查找。
    cwd_path = Path.cwd() / path
    if cwd_path.exists():
        return cwd_path
    # 找不到时再按包目录查找。
    return PACKAGE_DIR / path


def _resolve_output_path(value: str | Path) -> Path:
    # 先把字符串变成 Path。
    path = Path(value)
    # 绝对路径直接使用。
    if path.is_absolute():
        return path
    # 相对输出路径固定放在 XWJJJ260511 目录内。
    return PACKAGE_DIR / path


def _output_uses_timestamp_folder(output_cfg: Mapping[str, Any]) -> bool:
    # 默认开启按时间创建输出目录。
    value = output_cfg.get("timestamp_folder", True)
    # 布尔值直接返回。
    if isinstance(value, bool):
        return value
    # 字符串配置支持 true/yes/on 等常见写法。
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    # 其它类型按 Python 布尔规则转换。
    return bool(value)


def _get_output_run_dir(output_cfg: dict[str, Any], base_dir: Path) -> Path:
    # 同一次运行中，第一次生成的目录会缓存在配置字典里。
    run_dir = output_cfg.get("_run_dir")
    if run_dir:
        return Path(str(run_dir))

    # 用年月日_时分秒作为目录名。
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = base_dir / timestamp
    # 如果同一秒内已经存在目录，就追加 _02、_03 避免覆盖。
    suffix = 1
    while path.exists():
        suffix += 1
        path = base_dir / f"{timestamp}_{suffix:02d}"
    # 缓存本次运行目录，保证图片和日志在同一个文件夹。
    output_cfg["_run_dir"] = str(path)
    return path


def _camera_uses_mvs(cfg: Mapping[str, Any]) -> bool:
    # 判断当前相机后端是不是海康 MVS。
    return str(_section(cfg, "camera").get("backend", "opencv")).lower() == "mvs"


def _resolve_camera_settings(
    camera_cfg: Mapping[str, Any], motor_cfg: Mapping[str, Any]
) -> tuple[dict[str, Any], str]:
    """返回当前倍镜对应的相机配置；倍镜配置会覆盖 camera 顶层配置。"""

    settings = dict(camera_cfg)
    objective_settings = camera_cfg.get("objective_settings")
    objective = motor_cfg.get("objective")

    if objective_settings is None or objective is None:
        return settings, "camera"

    if not isinstance(objective_settings, Mapping):
        raise ValueError("配置错误：camera.objective_settings 必须是 YAML 字典")

    objective_cfg, matched_key = _find_objective_camera_settings(objective_settings, objective)
    settings.update(objective_cfg)
    return settings, f"camera.objective_settings.{matched_key}"


def _find_objective_camera_settings(
    objective_settings: Mapping[Any, Any], objective: Any
) -> tuple[Mapping[str, Any], str]:
    target = _normalize_objective_key(objective)
    available: list[str] = []

    for key, value in objective_settings.items():
        available.append(str(key))
        if _normalize_objective_key(key) == target:
            if not isinstance(value, Mapping):
                raise ValueError(f"配置错误：camera.objective_settings.{key} 必须是 YAML 字典")
            return value, str(key)

    choices = "、".join(available) if available else "空"
    raise ValueError(f"配置错误：未知相机倍镜配置 {objective!r}，可选倍镜：{choices}")


def _optional_bool(cfg: Mapping[str, Any], key: str, label: str) -> bool | None:
    if key not in cfg or cfg.get(key) is None:
        return None

    value = cfg.get(key)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        text = value.strip().lower()
        if text in {"1", "true", "yes", "on"}:
            return True
        if text in {"0", "false", "no", "off"}:
            return False
    raise ValueError(f"配置错误：{label} 必须是 true/false")


def _optional_float(cfg: Mapping[str, Any], key: str, label: str) -> float | None:
    if key not in cfg or cfg.get(key) is None:
        return None

    try:
        return float(cfg.get(key))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"配置错误：{label} 必须是数字") from exc


def _section(cfg: Mapping[str, Any], key: str) -> dict[str, Any]:
    # 从顶层配置里取某个 section；不存在时用空字典。
    value = cfg.get(key, {})
    # 每个 section 必须是字典，不能是字符串或列表。
    if not isinstance(value, dict):
        raise ValueError(f"配置错误：{key} 必须是 YAML 字典")
    return value


def _resolve_focus_range(motor_cfg: Mapping[str, Any]) -> tuple[float, float, str]:
    """按当前倍镜返回调焦范围，并兼容旧版 motor.min_pos/max_pos 写法。"""

    ranges = motor_cfg.get("objective_ranges")
    objective = motor_cfg.get("objective")

    if ranges is not None:
        if not isinstance(ranges, Mapping):
            raise ValueError("配置错误：motor.objective_ranges 必须是 YAML 字典")

        if objective is not None:
            range_cfg, matched_key = _find_objective_range(ranges, objective)
            return (
                float(range_cfg["min_pos"]),
                float(range_cfg["max_pos"]),
                f"motor.objective_ranges.{matched_key}",
            )

    return (
        float(motor_cfg.get("min_pos", 0)),
        float(motor_cfg.get("max_pos", 10000)),
        "motor",
    )


def _find_objective_range(
    ranges: Mapping[Any, Any], objective: Any
) -> tuple[Mapping[str, Any], str]:
    target = _normalize_objective_key(objective)
    available: list[str] = []

    for key, value in ranges.items():
        available.append(str(key))
        if _normalize_objective_key(key) == target:
            if not isinstance(value, Mapping):
                raise ValueError(f"配置错误：motor.objective_ranges.{key} 必须是 YAML 字典")
            if "min_pos" not in value or "max_pos" not in value:
                raise ValueError(
                    f"配置错误：motor.objective_ranges.{key} 必须包含 min_pos 和 max_pos"
                )
            return value, str(key)

    choices = "、".join(available) if available else "空"
    raise ValueError(f"配置错误：未知倍镜 {objective!r}，可选倍镜：{choices}")


def _normalize_objective_key(value: Any) -> str:
    text = str(value).strip().lower()
    for suffix in ("倍镜", "倍", "x", "镜"):
        if text.endswith(suffix):
            text = text[: -len(suffix)].strip()
    chinese_numbers = {
        "一": "1",
        "二": "2",
        "三": "3",
        "四": "4",
        "五": "5",
        "六": "6",
        "七": "7",
        "八": "8",
        "九": "9",
        "十": "10",
    }
    if text in chinese_numbers:
        return chinese_numbers[text]
    try:
        number = float(text)
    except ValueError:
        return text
    if number.is_integer():
        return str(int(number))
    return str(number)


def _get_bool(cfg: Mapping[str, Any], path: tuple[str, str], default: bool) -> bool:
    # path 形如 ("mode", "preview")，先取 section。
    section = _section(cfg, path[0])
    # 再取具体字段。
    value = section.get(path[1], default)
    # 布尔值直接返回。
    if isinstance(value, bool):
        return value
    # 字符串支持 true/yes/on 等写法。
    if isinstance(value, str):
        return value.lower() in {"1", "true", "yes", "on"}
    # 其它类型按 Python 布尔规则转换。
    return bool(value)


def _get_run_log_info(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """返回控制台和 CSV 共同使用的本次运行信息。"""

    camera_cfg = _section(cfg, "camera")
    motor_cfg = _section(cfg, "motor")
    camera_settings, camera_label = _resolve_camera_settings(camera_cfg, motor_cfg)
    min_pos, max_pos, range_label = _resolve_focus_range(motor_cfg)
    objective = motor_cfg.get("objective", "未配置")
    exposure_auto = _optional_bool(camera_settings, "exposure_auto", "camera.exposure_auto")
    exposure_time_us = _optional_float(
        camera_settings, "exposure_time_us", "camera.exposure_time_us"
    )
    return {
        "objective": objective,
        "exposure": _format_exposure(exposure_auto, exposure_time_us, camera_label),
        "min_pos": min_pos,
        "max_pos": max_pos,
        "range_label": range_label,
    }


def _format_exposure(
    exposure_auto: bool | None, exposure_time_us: float | None, camera_label: str
) -> str:
    if exposure_auto is None and exposure_time_us is None:
        return "沿用相机当前设置"
    if exposure_auto:
        return f"自动曝光 ({camera_label})"
    if exposure_time_us is None:
        return f"手动，沿用相机当前曝光时间 ({camera_label})"
    return f"{exposure_time_us:.1f} us ({camera_label})"


def _print_config_summary(config_path: Path, cfg: Mapping[str, Any]) -> None:
    # 取出主要配置段用于打印。
    camera_cfg = _section(cfg, "camera")
    motor_cfg = _section(cfg, "motor")
    output_cfg = _section(cfg, "output")
    camera_settings, camera_label = _resolve_camera_settings(camera_cfg, motor_cfg)
    print(f"配置文件: {config_path}")
    print(f"相机: {camera_settings.get('backend', 'opencv')}")
    if str(camera_settings.get("backend", "opencv")).lower() == "mvs":
        print(f"相机 IP: {camera_settings.get('ip', '自动枚举')}")
        print(f"电脑网卡 IP: {camera_settings.get('net_export_ip', '自动选择')}")
    exposure_auto = _optional_bool(camera_settings, "exposure_auto", "camera.exposure_auto")
    exposure_time_us = _optional_float(
        camera_settings, "exposure_time_us", "camera.exposure_time_us"
    )
    print(f"曝光: {_format_exposure(exposure_auto, exposure_time_us, camera_label)}")
    print(f"电机: {motor_cfg.get('type', 'virtual')}")
    min_pos, max_pos, range_label = _resolve_focus_range(motor_cfg)
    objective = motor_cfg.get("objective")
    if objective is not None:
        print(f"当前倍镜: {objective}")
    print(f"搜索范围: {min_pos:.0f} 到 {max_pos:.0f} ({range_label})")
    print(f"输出时间文件夹: {'开启' if _output_uses_timestamp_folder(output_cfg) else '关闭'}")
    print(f"图片保存: {_get_output_path(cfg, 'image_path')}")
    print(f"日志保存: {_get_output_path(cfg, 'log_path')}")


if __name__ == "__main__":
    main()

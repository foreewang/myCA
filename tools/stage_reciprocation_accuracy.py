"""XY 位移台两点往复精度测试工具。

默认只做参数和限位预检查，不连接硬件；只有显式传入 ``--execute`` 才会运动。
一个测试周期定义为 A -> B -> A，测量阶段分别统计 A/B 两个端点的控制器
位置误差和重复性。结果写入 CSV 明细与 JSON 汇总。

注意：这里统计的是驱动器位置反馈精度，不等同于载物台的物理定位精度。
丝杆间隙、联轴器、结构变形等必须配合量表、光栅尺或视觉标定板测量。
"""
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import signal
import statistics
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Sequence

import yaml


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from workflow.platform_defaults import DEFAULT_MODBUS_PORT  # noqa: E402

DEFAULT_PLATES_PATH = PROJECT_ROOT / "config" / "plates.yaml"
DEFAULT_OUTPUT_ROOT = PROJECT_ROOT / "data" / "stage_accuracy"

CSV_FIELDS = (
    "timestamp",
    "phase",
    "cycle",
    "endpoint",
    "status",
    "target_x",
    "target_y",
    "before_x",
    "before_y",
    "after_x",
    "after_y",
    "error_x_pulse",
    "error_y_pulse",
    "duration_s",
    "message",
)


class TestDefinitionError(ValueError):
    """测试点、轴定义或安全限位无效。"""


class StopRequested(RuntimeError):
    """用户请求停止测试。"""


def _load_plate_config(path: Path, plate_type: str) -> Dict[str, Any]:
    try:
        root = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError as exc:
        raise TestDefinitionError(f"培养板配置不存在: {path}") from exc
    except yaml.YAMLError as exc:
        raise TestDefinitionError(f"培养板配置 YAML 无法解析: {path}: {exc}") from exc

    plate = (root.get("plates") or {}).get(plate_type)
    if not isinstance(plate, dict):
        choices = ", ".join(str(x) for x in (root.get("plates") or {}).keys()) or "无"
        raise TestDefinitionError(f"不存在板型 {plate_type!r}，可选值: {choices}")
    return plate


def _axis_pulses_per_mm(plate: Mapping[str, Any]) -> tuple[float, float]:
    value = plate.get("pulses_per_mm")
    if isinstance(value, Mapping):
        x_ppm = float(value.get("x"))
        y_ppm = float(value.get("y"))
    else:
        x_ppm = y_ppm = float(value)
    if x_ppm <= 0 or y_ppm <= 0:
        raise TestDefinitionError("pulses_per_mm 的 X/Y 值必须大于 0")
    return x_ppm, y_ppm


def _stage_limits(plate: Mapping[str, Any]) -> Dict[str, Any]:
    cfg = plate.get("stage_limits")
    if not isinstance(cfg, Mapping) or not bool(cfg.get("enabled", False)):
        raise TestDefinitionError("精度测试要求 plates.yaml.stage_limits.enabled=true")

    required = ("x_min", "x_max", "y_min", "y_max")
    missing = [key for key in required if cfg.get(key) is None]
    if missing:
        raise TestDefinitionError(f"stage_limits 缺少字段: {', '.join(missing)}")

    limits = {
        "enabled": True,
        "x_min": int(cfg["x_min"]),
        "x_max": int(cfg["x_max"]),
        "y_min": int(cfg["y_min"]),
        "y_max": int(cfg["y_max"]),
        "safety_margin": int(cfg.get("safety_margin", 0)),
    }
    if limits["x_min"] >= limits["x_max"] or limits["y_min"] >= limits["y_max"]:
        raise TestDefinitionError("stage_limits 的 min 必须小于 max")
    if limits["safety_margin"] < 0:
        raise TestDefinitionError("stage_limits.safety_margin 不能为负数")
    return limits


def _safe_bounds(limits: Mapping[str, Any]) -> Dict[str, int]:
    margin = int(limits["safety_margin"])
    bounds = {
        "x_min": int(limits["x_min"]) + margin,
        "x_max": int(limits["x_max"]) - margin,
        "y_min": int(limits["y_min"]) + margin,
        "y_max": int(limits["y_max"]) - margin,
    }
    if bounds["x_min"] > bounds["x_max"] or bounds["y_min"] > bounds["y_max"]:
        raise TestDefinitionError("stage_limits.safety_margin 导致没有可用安全行程")
    return bounds


def _validate_test_definition(
    *,
    axis: str,
    point_a: Mapping[str, int],
    point_b: Mapping[str, int],
    limits: Mapping[str, Any],
) -> Dict[str, int]:
    if point_a == point_b:
        raise TestDefinitionError("A/B 两点不能完全相同")
    if axis == "x" and int(point_a["y"]) != int(point_b["y"]):
        raise TestDefinitionError("axis=x 时，point-a-y 必须等于 point-b-y")
    if axis == "y" and int(point_a["x"]) != int(point_b["x"]):
        raise TestDefinitionError("axis=y 时，point-a-x 必须等于 point-b-x")

    bounds = _safe_bounds(limits)
    for name, point in (("A", point_a), ("B", point_b)):
        x = int(point["x"])
        y = int(point["y"])
        if not bounds["x_min"] <= x <= bounds["x_max"]:
            raise TestDefinitionError(
                f"点{name}.x={x} 超出安全范围 [{bounds['x_min']}, {bounds['x_max']}]"
            )
        if not bounds["y_min"] <= y <= bounds["y_max"]:
            raise TestDefinitionError(
                f"点{name}.y={y} 超出安全范围 [{bounds['y_min']}, {bounds['y_max']}]"
            )
    return bounds


def _metric(values: Sequence[float], *, ppm: float) -> Dict[str, Any]:
    if not values:
        return {"count": 0}
    mean = statistics.fmean(values)
    stddev = statistics.pstdev(values) if len(values) > 1 else 0.0
    value_range = max(values) - min(values)
    max_abs = max(abs(value) for value in values)
    rmse = math.sqrt(statistics.fmean(value * value for value in values))
    return {
        "count": len(values),
        "mean_error_pulse": mean,
        "stddev_pulse": stddev,
        "range_pulse": value_range,
        "max_abs_error_pulse": max_abs,
        "rmse_pulse": rmse,
        "mean_error_mm": mean / ppm,
        "stddev_mm": stddev / ppm,
        "range_mm": value_range / ppm,
        "max_abs_error_mm": max_abs / ppm,
        "rmse_mm": rmse / ppm,
    }


def summarize_records(
    records: Iterable[Mapping[str, Any]],
    *,
    x_ppm: float,
    y_ppm: float,
) -> Dict[str, Any]:
    measured = [
        record
        for record in records
        if record.get("phase") == "measurement" and record.get("status") == "success"
    ]
    endpoint_summary: Dict[str, Any] = {}
    for endpoint in ("A", "B"):
        endpoint_records = [record for record in measured if record.get("endpoint") == endpoint]
        endpoint_summary[endpoint] = {
            "x": _metric(
                [float(record["error_x_pulse"]) for record in endpoint_records],
                ppm=x_ppm,
            ),
            "y": _metric(
                [float(record["error_y_pulse"]) for record in endpoint_records],
                ppm=y_ppm,
            ),
        }

    completed_cycles = len([record for record in measured if record.get("endpoint") == "A"])
    return {
        "measurement_move_count": len(measured),
        "completed_cycles": completed_cycles,
        "endpoints": endpoint_summary,
        "overall": {
            "x": _metric([float(record["error_x_pulse"]) for record in measured], ppm=x_ppm),
            "y": _metric([float(record["error_y_pulse"]) for record in measured], ppm=y_ppm),
        },
    }


def _write_csv(path: Path, records: Sequence[Mapping[str, Any]]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    with temp_path.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(records)
    os.replace(temp_path, path)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    temp_path = path.with_suffix(path.suffix + ".tmp")
    temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temp_path, path)


def _write_outputs(
    *,
    output_dir: Path,
    status: str,
    message: str | None,
    config: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
    x_ppm: float,
    y_ppm: float,
) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    _write_csv(output_dir / "moves.csv", records)
    _write_json(
        output_dir / "summary.json",
        {
            "status": status,
            "message": message,
            "updated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "config": dict(config),
            "summary": summarize_records(records, x_ppm=x_ppm, y_ppm=y_ppm),
        },
    )


def _position(result: Mapping[str, Any], phase: str, axis: str) -> int:
    return int(((result.get(phase) or {}).get(axis) or {})["current_pos"])


def _record_move(
    *,
    move_to_absolute: Any,
    point: Mapping[str, int],
    endpoint: str,
    phase: str,
    cycle: int,
    motion: Mapping[str, Any],
    stage_limits: Mapping[str, Any],
    stop_event: threading.Event,
) -> Dict[str, Any]:
    timestamp = datetime.now().astimezone().isoformat(timespec="milliseconds")
    started = time.perf_counter()
    result = move_to_absolute(
        port=motion["port"],
        x_target=int(point["x"]),
        y_target=int(point["y"]),
        profile_vel=int(motion["profile_vel"]),
        profile_acc=int(motion["profile_acc"]),
        profile_dec=int(motion["profile_dec"]),
        x_slave=int(motion["x_slave"]),
        y_slave=int(motion["y_slave"]),
        baudrate=int(motion["baudrate"]),
        settle_s=float(motion["settle_s"]),
        timeout_s=float(motion["timeout_s"]),
        poll_s=float(motion["poll_s"]),
        arrival_tolerance_pulse=int(motion["arrival_tolerance_pulse"]),
        stage_limits=stage_limits,
        stop_event=stop_event,
    )
    duration = time.perf_counter() - started
    before_x = _position(result, "before", "x")
    before_y = _position(result, "before", "y")
    after_x = _position(result, "after", "x")
    after_y = _position(result, "after", "y")
    stopped = bool(result.get("stopped_by_request", False))
    return {
        "timestamp": timestamp,
        "phase": phase,
        "cycle": cycle,
        "endpoint": endpoint,
        "status": "stopped" if stopped else "success",
        "target_x": int(point["x"]),
        "target_y": int(point["y"]),
        "before_x": before_x,
        "before_y": before_y,
        "after_x": after_x,
        "after_y": after_y,
        "error_x_pulse": after_x - int(point["x"]),
        "error_y_pulse": after_y - int(point["y"]),
        "duration_s": duration,
        "message": "stop requested" if stopped else "",
    }


def _execute(
    *,
    point_a: Mapping[str, int],
    point_b: Mapping[str, int],
    cycles: int,
    warmup_cycles: int,
    motion: Mapping[str, Any],
    limits: Mapping[str, Any],
    output_dir: Path,
    config: Mapping[str, Any],
    x_ppm: float,
    y_ppm: float,
) -> int:
    try:
        from workflow.stage_executor import move_to_absolute
    except ModuleNotFoundError as exc:
        raise RuntimeError(
            f"缺少运动依赖 {exc.name!r}；请先执行 pip install -r requirements.txt"
        ) from exc

    stop_event = threading.Event()

    def request_stop(signum: int, _frame: Any) -> None:
        if not stop_event.is_set():
            print(f"\n收到停止信号 {signum}，正在请求位移台快速停止……", file=sys.stderr)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    if hasattr(signal, "SIGTERM"):
        signal.signal(signal.SIGTERM, request_stop)

    records: list[Dict[str, Any]] = []
    status = "running"
    message: str | None = None

    def run_move(point: Mapping[str, int], endpoint: str, phase: str, cycle: int) -> None:
        nonlocal status, message
        if stop_event.is_set():
            raise StopRequested("测试已由用户停止")
        print(
            f"[{phase} cycle={cycle}] -> {endpoint} "
            f"target=({int(point['x'])}, {int(point['y'])})"
        )
        try:
            record = _record_move(
                move_to_absolute=move_to_absolute,
                point=point,
                endpoint=endpoint,
                phase=phase,
                cycle=cycle,
                motion=motion,
                stage_limits=limits,
                stop_event=stop_event,
            )
        except Exception as exc:
            records.append(
                {
                    "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                    "phase": phase,
                    "cycle": cycle,
                    "endpoint": endpoint,
                    "status": "failed",
                    "target_x": int(point["x"]),
                    "target_y": int(point["y"]),
                    "duration_s": "",
                    "message": str(exc),
                }
            )
            status = "failed"
            message = str(exc)
            _write_outputs(
                output_dir=output_dir,
                status=status,
                message=message,
                config=config,
                records=records,
                x_ppm=x_ppm,
                y_ppm=y_ppm,
            )
            raise

        records.append(record)
        print(
            f"    actual=({record['after_x']}, {record['after_y']}), "
            f"error=({record['error_x_pulse']}, {record['error_y_pulse']}) pulse, "
            f"duration={float(record['duration_s']):.3f}s"
        )
        _write_outputs(
            output_dir=output_dir,
            status="running",
            message=None,
            config=config,
            records=records,
            x_ppm=x_ppm,
            y_ppm=y_ppm,
        )
        if record["status"] == "stopped":
            raise StopRequested("测试已由用户停止")

    try:
        run_move(point_a, "A", "preposition", 0)
        for cycle in range(1, warmup_cycles + 1):
            run_move(point_b, "B", "warmup", cycle)
            run_move(point_a, "A", "warmup", cycle)
        for cycle in range(1, cycles + 1):
            run_move(point_b, "B", "measurement", cycle)
            run_move(point_a, "A", "measurement", cycle)
        status = "success"
    except StopRequested as exc:
        status = "stopped"
        message = str(exc)
    except Exception as exc:
        status = "failed"
        message = str(exc)

    _write_outputs(
        output_dir=output_dir,
        status=status,
        message=message,
        config=config,
        records=records,
        x_ppm=x_ppm,
        y_ppm=y_ppm,
    )
    print(f"测试状态: {status}")
    print(f"明细: {output_dir / 'moves.csv'}")
    print(f"汇总: {output_dir / 'summary.json'}")
    if message:
        print(f"消息: {message}", file=sys.stderr)
    return 0 if status == "success" else 1


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="XY 位移台两点往复精度测试")
    parser.add_argument("--axis", choices=("x", "y", "xy"), required=True, help="测试 X、Y 或 XY 联动")
    parser.add_argument("--point-a-x", type=int, required=True, help="A 点 X 绝对坐标，单位 pulse")
    parser.add_argument("--point-a-y", type=int, required=True, help="A 点 Y 绝对坐标，单位 pulse")
    parser.add_argument("--point-b-x", type=int, required=True, help="B 点 X 绝对坐标，单位 pulse")
    parser.add_argument("--point-b-y", type=int, required=True, help="B 点 Y 绝对坐标，单位 pulse")
    parser.add_argument("--cycles", type=int, default=10, help="测量周期数，A->B->A 为一周期")
    parser.add_argument("--warmup-cycles", type=int, default=1, help="不计入统计的预热周期数")
    parser.add_argument("--port", default=DEFAULT_MODBUS_PORT)
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--x-slave", type=int, default=1, help="软件 X/电机 1 从站号")
    parser.add_argument("--y-slave", type=int, default=2, help="软件 Y/电机 2 从站号")
    parser.add_argument("--profile-vel", type=int, default=100000)
    parser.add_argument("--profile-acc", type=int, default=100000)
    parser.add_argument("--profile-dec", type=int, default=100000)
    parser.add_argument("--arrival-tolerance-pulse", type=int, default=3000)
    parser.add_argument("--settle-s", type=float, default=1.0)
    parser.add_argument("--timeout-s", type=float, default=120.0)
    parser.add_argument("--poll-s", type=float, default=0.05)
    parser.add_argument("--plates", type=Path, default=DEFAULT_PLATES_PATH)
    parser.add_argument("--plate-type", default="24-well")
    parser.add_argument("--output-dir", type=Path, default=None)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="确认参数和现场安全后实际驱动电机；不提供时仅做 dry-run",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    positive_ints = {
        "cycles": args.cycles,
        "baudrate": args.baudrate,
        "x_slave": args.x_slave,
        "y_slave": args.y_slave,
        "profile_vel": args.profile_vel,
        "profile_acc": args.profile_acc,
        "profile_dec": args.profile_dec,
    }
    for name, value in positive_ints.items():
        if int(value) <= 0:
            parser.error(f"--{name.replace('_', '-')} 必须大于 0")
    if args.warmup_cycles < 0:
        parser.error("--warmup-cycles 不能为负数")
    if args.arrival_tolerance_pulse < 0:
        parser.error("--arrival-tolerance-pulse 不能为负数")
    if args.settle_s < 0 or args.timeout_s <= 0 or args.poll_s <= 0:
        parser.error("settle-s 必须非负，timeout-s/poll-s 必须大于 0")
    if args.x_slave == args.y_slave:
        parser.error("x-slave 和 y-slave 不能相同")

    point_a = {"x": int(args.point_a_x), "y": int(args.point_a_y)}
    point_b = {"x": int(args.point_b_x), "y": int(args.point_b_y)}
    try:
        plates_path = args.plates.resolve()
        plate = _load_plate_config(plates_path, args.plate_type)
        limits = _stage_limits(plate)
        bounds = _validate_test_definition(
            axis=args.axis,
            point_a=point_a,
            point_b=point_b,
            limits=limits,
        )
        x_ppm, y_ppm = _axis_pulses_per_mm(plate)
    except (TestDefinitionError, TypeError, ValueError) as exc:
        parser.error(str(exc))

    dx = int(point_b["x"]) - int(point_a["x"])
    dy = int(point_b["y"]) - int(point_a["y"])
    output_dir = (
        args.output_dir.resolve()
        if args.output_dir is not None
        else DEFAULT_OUTPUT_ROOT / datetime.now().strftime("%Y%m%d_%H%M%S")
    )
    motion = {
        "port": str(args.port),
        "baudrate": int(args.baudrate),
        "x_slave": int(args.x_slave),
        "y_slave": int(args.y_slave),
        "profile_vel": int(args.profile_vel),
        "profile_acc": int(args.profile_acc),
        "profile_dec": int(args.profile_dec),
        "arrival_tolerance_pulse": int(args.arrival_tolerance_pulse),
        "settle_s": float(args.settle_s),
        "timeout_s": float(args.timeout_s),
        "poll_s": float(args.poll_s),
    }
    config = {
        "axis": args.axis,
        "point_a": point_a,
        "point_b": point_b,
        "cycles": int(args.cycles),
        "warmup_cycles": int(args.warmup_cycles),
        "motion": motion,
        "plates_path": str(plates_path),
        "plate_type": str(args.plate_type),
        "stage_limits": limits,
        "safe_bounds": bounds,
        "pulses_per_mm": {"x": x_ppm, "y": y_ppm},
        "travel": {
            "x_pulse": dx,
            "y_pulse": dy,
            "x_mm": dx / x_ppm,
            "y_mm": dy / y_ppm,
        },
        "output_dir": str(output_dir),
    }

    print(json.dumps(config, ensure_ascii=False, indent=2))
    print(f"总运动次数: {1 + 2 * args.warmup_cycles + 2 * args.cycles}（含首次移动到 A）")
    if not args.execute:
        print("DRY-RUN：未连接硬件、未执行运动。确认点位和现场安全后追加 --execute。")
        return 0

    print("即将连接真实硬件执行往复测试。Ctrl+C 会请求快速停止，现场仍须准备物理急停。")
    try:
        return _execute(
            point_a=point_a,
            point_b=point_b,
            cycles=int(args.cycles),
            warmup_cycles=int(args.warmup_cycles),
            motion=motion,
            limits=limits,
            output_dir=output_dir,
            config=config,
            x_ppm=x_ppm,
            y_ppm=y_ppm,
        )
    except RuntimeError as exc:
        print(f"无法启动测试: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

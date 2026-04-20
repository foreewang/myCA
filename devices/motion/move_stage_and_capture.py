from __future__ import annotations

import argparse
import json
import logging
import time
from typing import Any, Dict, Optional

import sys
from pathlib import Path

CURRENT_DIR = Path(__file__).resolve().parent
PARENT_DIR = CURRENT_DIR.parent

if str(PARENT_DIR) not in sys.path:
    sys.path.insert(0, str(PARENT_DIR))
from modbus import ModbusRTUClient
from MotorManager import MotorManager
from camera_controller import HikCameraController


def read_axis_state(client: ModbusRTUClient, axis_name: str, slave: int) -> Dict[str, Any]:
    pos = client._read_32bit(slave, ModbusRTUClient.REG_CURRENT_POS)
    sw = client._read_statusword(slave)
    return {
        "axis": axis_name,
        "slave": slave,
        "current_pos": pos,
        "statusword": sw,
    }


def move_axis_absolute(
    motor: MotorManager,
    target_pos: Optional[int],
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    timeout: float,
) -> Optional[int]:
    if target_pos is None:
        return None
    return motor.pp_absolute_move(
        target_pos=target_pos,
        profile_vel=profile_vel,
        profile_acc=profile_acc,
        profile_dec=profile_dec,
        timeout=timeout,
    )


def move_axis_relative(
    motor: MotorManager,
    offset: Optional[int],
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    timeout: float,
) -> Optional[int]:
    if offset is None:
        return None
    return motor.pp_relative_move(
        offset=offset,
        profile_vel=profile_vel,
        profile_acc=profile_acc,
        profile_dec=profile_dec,
        timeout=timeout,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Move XY stage, then capture one image")

    # Motion
    parser.add_argument("--port", default="COM3", help="Modbus serial port, e.g. COM3")
    parser.add_argument("--baudrate", type=int, default=115200)
    parser.add_argument("--x-slave", type=int, default=1)
    parser.add_argument("--y-slave", type=int, default=2)
    parser.add_argument("--mode", choices=["abs", "rel"], default="abs", help="Stage move mode")
    parser.add_argument("--x", type=int, default=None, help="Absolute target of X axis (pulse)")
    parser.add_argument("--y", type=int, default=None, help="Absolute target of Y axis (pulse)")
    parser.add_argument("--dx", type=int, default=None, help="Relative offset of X axis (pulse)")
    parser.add_argument("--dy", type=int, default=None, help="Relative offset of Y axis (pulse)")
    parser.add_argument("--y-first", action="store_true", help="Move Y first, then X")
    parser.add_argument("--profile-vel", type=int, default=100000)
    parser.add_argument("--profile-acc", type=int, default=30000)
    parser.add_argument("--profile-dec", type=int, default=30000)
    parser.add_argument("--timeout", type=float, default=120.0)
    parser.add_argument("--settle-s", type=float, default=0.8, help="Extra wait time after motion before capture")

    # Camera
    parser.add_argument("--mvs-python-dir", default=None, help="MvImport path; omit to use camera_controller defaults")
    parser.add_argument("--device-index", type=int, default=0)
    parser.add_argument("--serial-number", default=None)
    parser.add_argument("--exposure-us", type=float, default=None)
    parser.add_argument("--gain", type=float, default=None)
    parser.add_argument("--save-path", required=True, help="Image save path, e.g. C:\\colony_system\\data\\images\\capture.bmp")

    # Output
    parser.add_argument("--output-json", default=None, help="Optional JSON report path")

    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s %(name)s: %(message)s")
    logger = logging.getLogger("move_stage_and_capture")

    if args.mode == "abs" and args.x is None and args.y is None:
        raise ValueError("abs mode requires at least one of --x or --y")
    if args.mode == "rel" and args.dx is None and args.dy is None:
        raise ValueError("rel mode requires at least one of --dx or --dy")

    save_path = Path(args.save_path)
    save_path.parent.mkdir(parents=True, exist_ok=True)

    result: Dict[str, Any] = {
        "port": args.port,
        "baudrate": args.baudrate,
        "mode": args.mode,
        "command": {
            "x": args.x,
            "y": args.y,
            "dx": args.dx,
            "dy": args.dy,
            "profile_vel": args.profile_vel,
            "profile_acc": args.profile_acc,
            "profile_dec": args.profile_dec,
            "timeout": args.timeout,
            "settle_s": args.settle_s,
            "y_first": args.y_first,
        },
        "camera": {
            "device_index": args.device_index,
            "serial_number": args.serial_number,
            "save_path": str(save_path),
            "exposure_us": args.exposure_us,
            "gain": args.gain,
        },
    }

    cam = HikCameraController(
        mvs_python_dir=args.mvs_python_dir,
        device_index=args.device_index,
        serial_number=args.serial_number,
        default_exposure_us=args.exposure_us,
        default_gain=args.gain,
    )

    try:
        logger.info("Open camera")
        cam.open()

        with ModbusRTUClient(port=args.port, baudrate=args.baudrate) as client:
            x_motor = MotorManager(client, slave=args.x_slave)
            y_motor = MotorManager(client, slave=args.y_slave)

            result["before"] = {
                "x": read_axis_state(client, "x", args.x_slave),
                "y": read_axis_state(client, "y", args.y_slave),
            }

            move_result: Dict[str, Any] = {}

            if args.mode == "abs":
                def move_x():
                    move_result["x_diff"] = move_axis_absolute(
                        x_motor, args.x, args.profile_vel, args.profile_acc, args.profile_dec, args.timeout
                    )

                def move_y():
                    move_result["y_diff"] = move_axis_absolute(
                        y_motor, args.y, args.profile_vel, args.profile_acc, args.profile_dec, args.timeout
                    )
            else:
                def move_x():
                    move_result["x_diff"] = move_axis_relative(
                        x_motor, args.dx, args.profile_vel, args.profile_acc, args.profile_dec, args.timeout
                    )

                def move_y():
                    move_result["y_diff"] = move_axis_relative(
                        y_motor, args.dy, args.profile_vel, args.profile_acc, args.profile_dec, args.timeout
                    )

            logger.info("Start stage motion")
            if args.y_first:
                move_y()
                move_x()
            else:
                move_x()
                move_y()

            result["move_result"] = move_result
            result["after"] = {
                "x": read_axis_state(client, "x", args.x_slave),
                "y": read_axis_state(client, "y", args.y_slave),
            }

            if args.settle_s > 0:
                logger.info("Wait %.3f s for stage settling", args.settle_s)
                time.sleep(args.settle_s)

            logger.info("Capture image -> %s", save_path)
            frame = cam.capture_bmp(str(save_path))
            result["capture"] = {
                "width": frame.width,
                "height": frame.height,
                "frame_num": frame.frame_num,
                "pixel_type": frame.pixel_type,
                "frame_len": frame.frame_len,
                "saved_path": frame.saved_path,
                "timestamp": frame.timestamp,
            }

    finally:
        try:
            cam.close()
        except Exception:
            pass

    if args.output_json:
        output_json = Path(args.output_json)
        output_json.parent.mkdir(parents=True, exist_ok=True)
        with open(output_json, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

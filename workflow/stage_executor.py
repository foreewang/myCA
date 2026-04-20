from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Dict, Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MOTION_DIR = PROJECT_ROOT / "devices" / "motion"

if str(MOTION_DIR) not in sys.path:
    sys.path.insert(0, str(MOTION_DIR))

from modbus import ModbusRTUClient  # type: ignore
from MotorManager import MotorManager  # type: ignore


def snapshot_axis(motor: MotorManager, axis_name: str) -> Dict[str, Any]:
    pos = motor.client._read_32bit(motor.slave, motor.client.REG_CURRENT_POS)
    sw = motor.client._read_statusword(motor.slave)
    return {
        "axis": axis_name,
        "slave": motor.slave,
        "current_pos": pos,
        "statusword": sw,
    }


def snapshot_xy(x_motor: MotorManager, y_motor: MotorManager) -> Dict[str, Any]:
    return {
        "x": snapshot_axis(x_motor, "x"),
        "y": snapshot_axis(y_motor, "y"),
    }


def move_to_absolute(
    *,
    port: str,
    x_target: int,
    y_target: int,
    profile_vel: int,
    profile_acc: int,
    profile_dec: int,
    x_slave: int = 1,
    y_slave: int = 2,
    baudrate: int = 115200,
    settle_s: float = 0.8,
) -> Dict[str, Any]:
    with ModbusRTUClient(port=port, baudrate=baudrate) as client:
        x_motor = MotorManager(client, slave=x_slave)
        y_motor = MotorManager(client, slave=y_slave)

        before = snapshot_xy(x_motor, y_motor)

        x_diff = x_motor.pp_absolute_move(
            target_pos=int(x_target),
            profile_vel=int(profile_vel),
            profile_acc=int(profile_acc),
            profile_dec=int(profile_dec),
        )
        y_diff = y_motor.pp_absolute_move(
            target_pos=int(y_target),
            profile_vel=int(profile_vel),
            profile_acc=int(profile_acc),
            profile_dec=int(profile_dec),
        )

        time.sleep(settle_s)
        after = snapshot_xy(x_motor, y_motor)

        err_to_target = {
            "x": int(after["x"]["current_pos"]) - int(x_target),
            "y": int(after["y"]["current_pos"]) - int(y_target),
        }

        return {
            "target": {"x": int(x_target), "y": int(y_target)},
            "before": before,
            "move_result": {"x_diff": x_diff, "y_diff": y_diff},
            "after": after,
            "err_to_target": err_to_target,
        }
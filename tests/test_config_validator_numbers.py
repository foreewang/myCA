"""Numeric safety checks shared by hardware configuration validators."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

from workflow.config_validator import (
    ConfigIssue,
    _require_number,
    _validate_autofocus_focus,
    _validate_autofocus_motor,
)


class ConfigValidatorNumberTests(unittest.TestCase):
    def test_non_finite_hardware_values_are_rejected(self) -> None:
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                issues: list[ConfigIssue] = []
                result = _require_number(
                    {"value": value},
                    "value",
                    "camera.exposure_us",
                    issues,
                    minimum=0,
                    exclusive_min=True,
                )
                self.assertIsNone(result)
                self.assertEqual(len(issues), 1)
                self.assertEqual(issues[0].message, "must be a finite number")

    def test_finite_hardware_value_is_preserved(self) -> None:
        issues: list[ConfigIssue] = []
        result = _require_number(
            {"value": 5000.0},
            "value",
            "camera.exposure_us",
            issues,
            minimum=0,
            exclusive_min=True,
        )
        self.assertEqual(result, 5000.0)
        self.assertEqual(issues, [])

    def test_autofocus_motor_requires_a_bounded_move_timeout(self) -> None:
        base = {
            "type": "modbus",
            "port": "COM3",
            "baudrate": 115200,
            "focus_slave": 3,
            "objective": "4x",
            "objective_ranges": {"4x": {"min_pos": 100, "max_pos": 200}},
            "profile_vel": 50000,
            "profile_acc": 50000,
            "profile_dec": 50000,
        }
        for timeout, expected in ((None, "must be a number"), (121, "must be <= 120")):
            with self.subTest(timeout=timeout):
                cfg = dict(base)
                if timeout is not None:
                    cfg["move_timeout_s"] = timeout
                issues: list[ConfigIssue] = []
                _validate_autofocus_motor("autofocus.motor", cfg, issues, None, None)
                messages = [issue.message for issue in issues if issue.path.endswith("move_timeout_s")]
                self.assertIn(expected, messages)

    def test_autofocus_work_bounds_iteration_and_settle_time(self) -> None:
        issues: list[ConfigIssue] = []
        _validate_autofocus_focus(
            "autofocus.focus",
            {
                "tol": 1,
                "max_iter": 51,
                "settle_ms": 5001,
                "center_roi": 0.6,
                "downsample": 0.5,
            },
            issues,
        )
        messages = {(issue.path, issue.message) for issue in issues}
        self.assertIn(("autofocus.focus.max_iter", "must be <= 50"), messages)
        self.assertIn(("autofocus.focus.settle_ms", "must be <= 5000"), messages)

    def test_autofocus_runtime_forwards_configured_motor_timeout(self) -> None:
        from third_party.XWJJJ260511 import run as autofocus_run

        sentinel = object()
        with TemporaryDirectory() as temp_dir, mock.patch(
            "third_party.XWJJJ260511.autofocus_api.run_realtime_autofocus",
            return_value=sentinel,
        ) as run_mock:
            result = autofocus_run._run_autofocus(
                object(),
                {
                    "motor": {
                        "type": "virtual",
                        "min_pos": 100,
                        "max_pos": 200,
                        "move_timeout_s": 17,
                    },
                    "focus": {},
                    "camera": {"backend": "mvs"},
                    "output": {
                        "timestamp_folder": False,
                        "image_path": str(Path(temp_dir) / "sharpest.png"),
                    },
                },
            )

        self.assertIs(result, sentinel)
        self.assertEqual(run_mock.call_args.kwargs["motor_move_timeout_s"], 17.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)

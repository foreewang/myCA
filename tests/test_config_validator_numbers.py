"""Numeric safety checks shared by hardware configuration validators."""
from __future__ import annotations

import unittest

from workflow.config_validator import ConfigIssue, _require_number


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


if __name__ == "__main__":
    unittest.main(verbosity=2)

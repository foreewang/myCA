"""Production-schema checks for config/objectives.yaml."""
from __future__ import annotations

import copy
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest import mock

from workflow import config_validator
from workflow.config_validator import (
    ConfigValidationError,
    load_yaml_unique,
    validate_objectives_config,
    validate_objectives_file,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
OBJECTIVES_PATH = PROJECT_ROOT / "config" / "objectives.yaml"


class ObjectiveConfigValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.valid = load_yaml_unique(OBJECTIVES_PATH)

    def _assert_rejected(self, config: dict, expected_path: str, expected_message: str | None = None) -> None:
        with self.assertRaises(ConfigValidationError) as raised:
            validate_objectives_config(config)
        matching = [issue for issue in raised.exception.issues if issue.path == expected_path]
        self.assertTrue(matching, f"missing issue for {expected_path}: {raised.exception}")
        if expected_message is not None:
            self.assertTrue(
                any(expected_message in issue.message for issue in matching),
                f"missing {expected_message!r} in issues for {expected_path}: {matching}",
            )

    def test_repository_objectives_file_passes_complete_validation(self) -> None:
        loaded = validate_objectives_file(OBJECTIVES_PATH)
        self.assertEqual(set(loaded["objectives"]), {"4x", "10x"})

    def test_magnification_and_fov_must_be_finite_positive_numbers(self) -> None:
        cases = (
            (("magnification",), float("nan"), "objectives.4x.magnification", "finite"),
            (("magnification",), 0, "objectives.4x.magnification", "> 0"),
            (("fov_mm", "width"), float("inf"), "objectives.4x.fov_mm.width", "finite"),
            (("fov_mm", "height"), -1, "objectives.4x.fov_mm.height", "> 0"),
        )
        for keys, value, expected_path, expected_message in cases:
            with self.subTest(path=expected_path, value=value):
                config = copy.deepcopy(self.valid)
                node = config["objectives"]["4x"]
                for key in keys[:-1]:
                    node = node[key]
                node[keys[-1]] = value
                self._assert_rejected(config, expected_path, expected_message)

    def test_named_magnification_must_match_objective_name(self) -> None:
        config = copy.deepcopy(self.valid)
        config["objectives"]["4x"]["magnification"] = 10
        self._assert_rejected(
            config,
            "objectives.4x.magnification",
            "must match objective name",
        )

    def test_switch_mode_boolean_targets_and_profiles_use_runtime_types(self) -> None:
        cases = (
            ("enabled", 1, "must be a boolean"),
            ("mode", "manual", "must be one of"),
            ("objective_target_pos", 1.5, "must be an integer"),
            ("focus_target_pos", "-3002685", "must be an integer"),
            ("focus_collision_limit_pos", -3436433.0, "must be an integer"),
            ("objective_profile_vel", 0, "must be >= 1"),
            ("objective_profile_acc", -1, "must be >= 1"),
            ("objective_profile_dec", 100000.0, "must be an integer"),
            ("focus_profile_vel", 0, "must be >= 1"),
            ("focus_profile_acc", -1, "must be >= 1"),
            ("focus_profile_dec", 100000.0, "must be an integer"),
        )
        for key, value, expected_message in cases:
            with self.subTest(key=key, value=value):
                config = copy.deepcopy(self.valid)
                config["objectives"]["4x"]["switch"][key] = value
                self._assert_rejected(
                    config,
                    f"objectives.4x.switch.{key}",
                    expected_message,
                )

    def test_focus_target_must_respect_objective_and_switch_collision_floors(self) -> None:
        config = copy.deepcopy(self.valid)
        objective_switch = config["objectives"]["4x"]["switch"]
        objective_switch["focus_target_pos"] = objective_switch["focus_collision_limit_pos"] - 1
        self._assert_rejected(
            config,
            "objectives.4x.switch.focus_target_pos",
            "focus_collision_limit_pos",
        )

        config = copy.deepcopy(self.valid)
        global_limit = config["hardware"]["focus_axis"]["objective_switch_collision_limit_pos"]
        config["objectives"]["4x"]["switch"]["focus_target_pos"] = global_limit - 1
        self._assert_rejected(
            config,
            "objectives.4x.switch.focus_target_pos",
            "hardware.focus_axis.objective_switch_collision_limit_pos",
        )

    def test_modbus_and_axis_fields_are_explicit_positive_integers(self) -> None:
        cases = (
            (("modbus", "port"), "", "hardware.modbus.port", "non-empty string"),
            (("modbus", "baudrate"), 115200.0, "hardware.modbus.baudrate", "integer"),
            (("modbus", "baudrate"), 0, "hardware.modbus.baudrate", ">= 1"),
            (("objective_axis", "slave"), "4", "hardware.objective_axis.slave", "integer"),
            (("focus_axis", "slave"), 0, "hardware.focus_axis.slave", ">= 1"),
            (
                ("focus_axis", "objective_switch_collision_limit_pos"),
                -3168285.0,
                "hardware.focus_axis.objective_switch_collision_limit_pos",
                "integer",
            ),
        )
        for keys, value, expected_path, expected_message in cases:
            with self.subTest(path=expected_path, value=value):
                config = copy.deepcopy(self.valid)
                config["hardware"][keys[0]][keys[1]] = value
                self._assert_rejected(config, expected_path, expected_message)

        config = copy.deepcopy(self.valid)
        config["hardware"]["focus_axis"]["slave"] = config["hardware"]["objective_axis"]["slave"]
        self._assert_rejected(config, "hardware.focus_axis.slave", "must differ")

    def test_state_and_objective_names_cannot_reference_ambiguous_values(self) -> None:
        config = copy.deepcopy(self.valid)
        config["state"]["assume_initial"] = "20x"
        self._assert_rejected(config, "state.assume_initial", "undefined objective")

        config = copy.deepcopy(self.valid)
        config["objectives"][" 4x"] = config["objectives"].pop("4x")
        self._assert_rejected(config, "objectives. 4x", "whitespace")

        config = copy.deepcopy(self.valid)
        config["objectives"]["4X"] = copy.deepcopy(config["objectives"]["4x"])
        self._assert_rejected(config, "objectives.4X", "duplicates")

    def test_default_cli_includes_objectives_and_objectives_only_is_independent(self) -> None:
        validator_names = (
            "validate_objectives_file",
            "validate_camera_file",
            "validate_plates_file",
            "validate_autofocus_file",
            "validate_handoff_file",
        )
        patches = {name: mock.patch.object(config_validator, name) for name in validator_names}
        mocks = {name: patcher.start() for name, patcher in patches.items()}
        self.addCleanup(lambda: [patcher.stop() for patcher in patches.values()])

        with mock.patch.object(sys, "argv", ["config_validator"]), contextlib.redirect_stdout(io.StringIO()):
            config_validator.main()
        for validator in mocks.values():
            validator.assert_called_once()

        for validator in mocks.values():
            validator.reset_mock()
        with mock.patch.object(
            sys,
            "argv",
            ["config_validator", "--objectives", str(OBJECTIVES_PATH)],
        ), contextlib.redirect_stdout(io.StringIO()):
            config_validator.main()
        mocks["validate_objectives_file"].assert_called_once_with(OBJECTIVES_PATH)
        for name in validator_names[1:]:
            mocks[name].assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)

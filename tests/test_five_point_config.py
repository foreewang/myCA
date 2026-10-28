"""Five-point calibration schema validation without connecting to hardware."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import unittest

from workflow.config_validator import (
    ConfigValidationError,
    load_yaml_unique,
    validate_plates_config,
)


PLATES_PATH = Path(__file__).resolve().parents[1] / "config" / "plates.yaml"


class FivePointConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = load_yaml_unique(PLATES_PATH)
        self.plate = self.config["plates"]["24-well"]

    def assert_invalid(self, suffix: str) -> None:
        with self.assertRaises(ConfigValidationError) as raised:
            validate_plates_config(self.config)
        self.assertIn(f"plates.24-well.{suffix}", str(raised.exception))

    def test_shipped_calibration_is_valid_for_all_plate_types(self) -> None:
        validate_plates_config(self.config)
        for plate in self.config["plates"].values():
            self.assertEqual(plate["a1_start"]["reference_objective"], "4x")
            self.assertEqual(plate["a1_start"]["reference_fov_mm"], {"width": 3.22, "height": 3.22})
            self.assertEqual(plate["five_point"], {"radial_fraction": 0.5, "edge_margin_mm": 0.5})

    def test_legacy_full_coverage_plate_configuration_remains_loadable(self) -> None:
        for plate in self.config["plates"].values():
            plate.pop("five_point")
            plate["a1_start"].pop("reference_objective")
            plate["a1_start"].pop("reference_fov_mm")
        validate_plates_config(self.config)

    def test_five_point_fields_must_be_supplied_together(self) -> None:
        original = deepcopy(self.plate)
        fields = ("reference_objective", "reference_fov_mm", "five_point")
        for mask in range(1, 7):
            with self.subTest(mask=mask):
                self.plate.clear()
                self.plate.update(deepcopy(original))
                removed = []
                for index, key in enumerate(fields):
                    if not mask & (1 << index):
                        container = self.plate if key == "five_point" else self.plate["a1_start"]
                        container.pop(key)
                        removed.append(key if key == "five_point" else f"a1_start.{key}")
                self.assert_invalid(removed[0])

    def test_reference_objective_is_explicit_four_times(self) -> None:
        for value in (None, True, 4, "10x", "4X", " 4x ", ""):
            with self.subTest(value=value):
                self.plate["a1_start"]["reference_objective"] = value
                self.assert_invalid("a1_start.reference_objective")

    def test_geometry_fields_reject_non_finite_non_positive_and_non_numeric_values(self) -> None:
        cases = (
            (self.plate["a1_start"]["reference_fov_mm"], "width", "a1_start.reference_fov_mm.width"),
            (self.plate["a1_start"]["reference_fov_mm"], "height", "a1_start.reference_fov_mm.height"),
            (self.plate["five_point"], "radial_fraction", "five_point.radial_fraction"),
            (self.plate["five_point"], "edge_margin_mm", "five_point.edge_margin_mm"),
        )
        for container, key, path in cases:
            original = container[key]
            for value in (None, True, False, "0.5", 0, -0.5, float("nan"), float("inf"), float("-inf")):
                with self.subTest(field=path, value=value):
                    container[key] = value
                    self.assert_invalid(path)
            container[key] = original

    def test_fraction_must_be_less_than_one(self) -> None:
        for value in (1, 1.001, 2):
            with self.subTest(value=value):
                self.plate["five_point"]["radial_fraction"] = value
                self.assert_invalid("five_point.radial_fraction")

    def test_required_nested_values_cannot_be_omitted(self) -> None:
        cases = (
            (self.plate["a1_start"]["reference_fov_mm"], "width", "a1_start.reference_fov_mm.width"),
            (self.plate["a1_start"]["reference_fov_mm"], "height", "a1_start.reference_fov_mm.height"),
            (self.plate["five_point"], "radial_fraction", "five_point.radial_fraction"),
            (self.plate["five_point"], "edge_margin_mm", "five_point.edge_margin_mm"),
        )
        for container, key, path in cases:
            with self.subTest(field=path):
                original = container.pop(key)
                self.assert_invalid(path)
                container[key] = original

    def test_nested_geometry_requires_mappings(self) -> None:
        for value in (None, True, 3.22, [3.22, 3.22]):
            with self.subTest(field="reference_fov_mm", value=value):
                self.plate["a1_start"]["reference_fov_mm"] = value
                self.assert_invalid("a1_start.reference_fov_mm")
        self.plate["a1_start"]["reference_fov_mm"] = {"width": 3.22, "height": 3.22}
        for value in (None, True, 0.5, [0.5, 0.5]):
            with self.subTest(field="five_point", value=value):
                self.plate["five_point"] = value
                self.assert_invalid("five_point")

    def test_unknown_nested_fields_do_not_silently_hide_typographical_errors(self) -> None:
        self.plate["five_point"]["edge_margn_mm"] = 0.5
        self.assert_invalid("five_point.edge_margn_mm")
        self.plate["five_point"].pop("edge_margn_mm")
        self.plate["a1_start"]["reference_fov_mm"]["widht"] = 3.22
        self.assert_invalid("a1_start.reference_fov_mm.widht")


if __name__ == "__main__":
    unittest.main(verbosity=2)

"""Tests for fail-closed production runtime validation."""
from __future__ import annotations

import importlib.metadata
import unittest

from workflow.deployment_preflight import (
    LOCK_FILES,
    parse_exact_lock,
    select_runtime_profile,
    validate_locked_distributions,
)


class DeploymentPreflightTests(unittest.TestCase):
    def test_release_candidate_locks_are_exact_and_profile_specific(self) -> None:
        cpu = parse_exact_lock(LOCK_FILES["cpu"])
        gpu = parse_exact_lock(LOCK_FILES["gpu"])
        self.assertEqual(cpu["onnxruntime"], ("onnxruntime", "1.23.2"))
        self.assertNotIn("onnxruntime-gpu", cpu)
        self.assertEqual(gpu["onnxruntime-gpu"], ("onnxruntime-gpu", "1.23.2"))
        self.assertNotIn("onnxruntime", gpu)
        for required in ("fastapi", "starlette", "pydantic", "uvicorn", "numpy", "opencv-python"):
            self.assertIn(required, cpu)
            self.assertIn(required, gpu)

    def test_runtime_profile_requires_exactly_one_onnx_distribution(self) -> None:
        def getter(installed: set[str]):
            def version(name: str) -> str:
                if name not in installed:
                    raise importlib.metadata.PackageNotFoundError(name)
                return "1.23.2"

            return version

        self.assertEqual(select_runtime_profile(getter({"onnxruntime"})), ("cpu", []))
        self.assertEqual(select_runtime_profile(getter({"onnxruntime-gpu"})), ("gpu", []))
        self.assertIsNone(select_runtime_profile(getter(set()))[0])
        self.assertIsNone(select_runtime_profile(getter({"onnxruntime", "onnxruntime-gpu"}))[0])

    def test_version_drift_and_missing_distribution_fail_validation(self) -> None:
        expected = {
            "fastapi": ("fastapi", "1.2.3"),
            "uvicorn": ("uvicorn", "4.5.6"),
        }

        def version(name: str) -> str:
            if name == "fastapi":
                return "9.9.9"
            raise importlib.metadata.PackageNotFoundError(name)

        issues, actual = validate_locked_distributions(expected, version)
        self.assertEqual(actual, {"fastapi": "9.9.9"})
        self.assertTrue(any("version drift" in issue for issue in issues))
        self.assertTrue(any("missing distribution" in issue for issue in issues))


if __name__ == "__main__":
    unittest.main(verbosity=2)

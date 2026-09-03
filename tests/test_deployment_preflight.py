"""Tests for fail-closed production runtime validation."""
from __future__ import annotations

import importlib.metadata
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from tools import release_gate

from workflow.deployment_preflight import (
    LOCK_FILES,
    parse_exact_lock,
    select_runtime_profile,
    validate_locked_distributions,
)


class DeploymentPreflightTests(unittest.TestCase):
    def test_release_gate_ignores_external_pytest_execution_controls(self) -> None:
        with mock.patch.dict(
            os.environ,
            {
                "PYTEST_ADDOPTS": "--collect-only --ignore=tests/test_core_workflow.py",
                "PYTEST_PLUGINS": "untrusted_external_plugin",
            },
            clear=False,
        ):
            args = release_gate._prepare_pytest_run()
            self.assertNotIn("PYTEST_ADDOPTS", os.environ)
            self.assertNotIn("PYTEST_PLUGINS", os.environ)
            self.assertEqual(os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"], "1")

        self.assertIn("--rootdir", args)
        self.assertEqual(args[args.index("--rootdir") + 1], str(release_gate.PROJECT_ROOT))
        self.assertIn("addopts=", args)
        self.assertNotIn("--collect-only", args)

    def test_release_gate_distinguishes_collection_from_execution(self) -> None:
        plugin = release_gate._NoSkipPytestPlugin()
        plugin.pytest_collection_finish(
            SimpleNamespace(
                items=[
                    SimpleNamespace(nodeid="tests/test_a.py::test_one"),
                    SimpleNamespace(nodeid="tests/test_b.py::test_two"),
                ]
            )
        )
        plugin.pytest_runtest_logreport(
            SimpleNamespace(
                nodeid="tests/test_a.py::test_one",
                when="call",
                skipped=False,
                longrepr=None,
            )
        )

        self.assertEqual(plugin.collected, 2)
        self.assertEqual(plugin.executed, {"tests/test_a.py::test_one"})
        self.assertEqual(
            plugin.nodeids - plugin.executed,
            {"tests/test_b.py::test_two"},
        )

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

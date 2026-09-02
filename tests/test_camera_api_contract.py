"""Dependency-free checks for the public camera HTTP contract."""
from __future__ import annotations

import ast
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class CameraApiContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server_tree = ast.parse(
            (PROJECT_ROOT / "workflow" / "api_server.py").read_text(encoding="utf-8")
        )
        cls.models_tree = ast.parse(
            (PROJECT_ROOT / "workflow" / "api_models.py").read_text(encoding="utf-8")
        )

    def _function(self, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
        for node in self.server_tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
                return node
        self.fail(f"missing function {name}")

    def test_record_start_and_stop_are_http_202(self) -> None:
        for function_name in ("start_camera_record", "stop_camera_record"):
            function = self._function(function_name)
            post = next(
                (
                    decorator
                    for decorator in function.decorator_list
                    if isinstance(decorator, ast.Call)
                    and isinstance(decorator.func, ast.Attribute)
                    and decorator.func.attr == "post"
                ),
                None,
            )
            self.assertIsNotNone(post, f"{function_name} must have @app.post")
            assert post is not None
            status_code = next(
                (keyword.value for keyword in post.keywords if keyword.arg == "status_code"),
                None,
            )
            self.assertIsInstance(status_code, ast.Constant)
            self.assertEqual(status_code.value, 202)

    def test_async_contract_bumps_public_api_version(self) -> None:
        app_assignment = next(
            node
            for node in self.server_tree.body
            if isinstance(node, ast.Assign)
            and any(isinstance(target, ast.Name) and target.id == "app" for target in node.targets)
        )
        self.assertIsInstance(app_assignment.value, ast.Call)
        version = next(
            keyword.value
            for keyword in app_assignment.value.keywords
            if keyword.arg == "version"
        )
        self.assertIsInstance(version, ast.Constant)
        self.assertEqual(version.value, "0.4.0")

    def test_api_lifespan_shuts_down_camera_supervisor(self) -> None:
        lifespan = self._function("_api_lifespan")
        calls = {
            node.func.id
            for node in ast.walk(lifespan)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }
        referenced_names = {
            node.id for node in ast.walk(lifespan) if isinstance(node, ast.Name)
        }
        self.assertIn("initialize_camera_process_supervisor", calls)
        self.assertIn("shutdown_recording_camera", referenced_names)

    def test_record_timeout_public_limit_matches_controller_limit(self) -> None:
        camera_model = next(
            node
            for node in self.models_tree.body
            if isinstance(node, ast.ClassDef) and node.name == "CameraRecordStartRequest"
        )
        timeout_assignment = next(
            node
            for node in camera_model.body
            if isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "timeout_ms"
        )
        self.assertIsInstance(timeout_assignment.value, ast.Call)
        assert isinstance(timeout_assignment.value, ast.Call)
        upper_bound = next(
            keyword.value
            for keyword in timeout_assignment.value.keywords
            if keyword.arg == "le"
        )
        self.assertIsInstance(upper_bound, ast.Constant)
        self.assertEqual(upper_bound.value, 15_000)

    def test_start_script_enforces_lifespan_and_bounded_graceful_shutdown(self) -> None:
        script = (PROJECT_ROOT / "start_api.bat").read_text(encoding="utf-8")
        self.assertIn("--workers 1", script)
        self.assertIn("--lifespan on", script)
        self.assertIn("--timeout-graceful-shutdown 45", script)


if __name__ == "__main__":
    unittest.main(verbosity=2)

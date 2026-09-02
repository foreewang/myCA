"""Ensure MVS autofocus cannot bypass the camera process boundary."""
from __future__ import annotations

import importlib
import sys
import types
import unittest
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


_saved_config_validator = sys.modules.get("workflow.config_validator")
_saved_autofocus_package = sys.modules.get("third_party.XWJJJ260511")
config_validator_stub = types.ModuleType("workflow.config_validator")
config_validator_stub.validate_autofocus_file = lambda *args, **kwargs: None  # type: ignore[attr-defined]
autofocus_package_stub = types.ModuleType("third_party.XWJJJ260511")
autofocus_package_stub.run_autofocus = lambda *args, **kwargs: None  # type: ignore[attr-defined]
sys.modules["workflow.config_validator"] = config_validator_stub
sys.modules["third_party.XWJJJ260511"] = autofocus_package_stub
try:
    autofocus_executor = importlib.import_module("workflow.autofocus_executor")
finally:
    if _saved_config_validator is None:
        sys.modules.pop("workflow.config_validator", None)
    else:
        sys.modules["workflow.config_validator"] = _saved_config_validator
    if _saved_autofocus_package is None:
        sys.modules.pop("third_party.XWJJJ260511", None)
    else:
        sys.modules["third_party.XWJJJ260511"] = _saved_autofocus_package


class ProxyStub:
    def __init__(self, *, recording_shared: bool) -> None:
        self.recording_shared = recording_shared


class CameraAutofocusContractTests(unittest.TestCase):
    def setUp(self) -> None:
        self.backend = "mvs"
        self.open_kwargs: dict[str, Any] = {}
        self.closed: list[Any] = []
        self.proxy = ProxyStub(recording_shared=True)

        self.original_executor = sys.modules.get("workflow.camera_executor")
        executor_stub = types.ModuleType("workflow.camera_executor")

        def open_camera(**kwargs: Any) -> ProxyStub:
            self.open_kwargs = dict(kwargs)
            return self.proxy

        executor_stub.open_camera = open_camera  # type: ignore[attr-defined]
        executor_stub.close_camera = lambda camera: self.closed.append(camera)  # type: ignore[attr-defined]
        sys.modules["workflow.camera_executor"] = executor_stub

        self.original_package = sys.modules.get("third_party.XWJJJ260511")
        self.run_stub = types.ModuleType("third_party.XWJJJ260511.run")
        self.run_stub._load_yaml_config = lambda path: {"motor": {}, "camera": {}}  # type: ignore[attr-defined]
        self.run_stub._section = lambda cfg, name: cfg.setdefault(name, {})  # type: ignore[attr-defined]
        self.run_stub._resolve_camera_settings = lambda camera, motor: (  # type: ignore[attr-defined]
            {
                "backend": self.backend,
                "mvs_python_dir": "MVS",
                "device_index": 2,
                "serial_number": "SERIAL-2",
                "ip": "192.168.1.253",
                "pixel_format": "mono8",
                "exposure_auto": False,
                "exposure_time_us": 5_000,
                "gain": 1.0,
            },
            "camera",
        )
        result = types.SimpleNamespace(focus_log=[], output_path=None)
        self.run_stub._run_autofocus = lambda camera, cfg: result  # type: ignore[attr-defined]
        self.run_stub._save_focus_log = lambda *args, **kwargs: None  # type: ignore[attr-defined]
        self.run_stub._get_output_path = lambda cfg, key: None  # type: ignore[attr-defined]
        package_stub = types.ModuleType("third_party.XWJJJ260511")
        package_stub.run = self.run_stub  # type: ignore[attr-defined]
        sys.modules["third_party.XWJJJ260511"] = package_stub

    def tearDown(self) -> None:
        if self.original_executor is None:
            sys.modules.pop("workflow.camera_executor", None)
        else:
            sys.modules["workflow.camera_executor"] = self.original_executor
        if self.original_package is None:
            sys.modules.pop("third_party.XWJJJ260511", None)
        else:
            sys.modules["third_party.XWJJJ260511"] = self.original_package

    def test_mvs_autofocus_always_opens_managed_proxy_and_closes_it(self) -> None:
        result = autofocus_executor._run_autofocus_reusing_recording_camera(
            Path("config/autofocus.yaml"),
            "4x",
        )
        self.assertIsNotNone(result)
        self.assertEqual(self.open_kwargs["serial_number"], "SERIAL-2")
        self.assertEqual(self.open_kwargs["camera_ip"], "192.168.1.253")
        self.assertEqual(self.open_kwargs["exposure_us"], 5_000)
        self.assertEqual(self.closed, [self.proxy])
        self.assertTrue(result.reused_recording_camera)

    def test_opencv_autofocus_keeps_existing_third_party_path(self) -> None:
        self.backend = "opencv"
        result = autofocus_executor._run_autofocus_reusing_recording_camera(
            Path("config/autofocus.yaml"),
            "4x",
        )
        self.assertIsNone(result)
        self.assertEqual(self.open_kwargs, {})
        self.assertEqual(self.closed, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

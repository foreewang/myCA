"""Linux industrial-PC defaults used by the deploy/linux branch."""
from __future__ import annotations

from pathlib import Path

from workflow.api_models import StageReciprocationStartRequest
from workflow.config_validator import load_yaml_unique
from workflow.platform_defaults import DEFAULT_MODBUS_PORT, DEFAULT_MVS_PYTHON_DIR


PROJECT_ROOT = Path(__file__).resolve().parents[1]
LINUX_MODBUS_PORT = "/dev/ttyUSB0"
LINUX_MVS_PYTHON_DIR = "/opt/MVS/Samples/64/Python/MvImport"


def test_modbus_and_mvs_constants_target_linux() -> None:
    assert DEFAULT_MODBUS_PORT == LINUX_MODBUS_PORT
    assert DEFAULT_MVS_PYTHON_DIR == LINUX_MVS_PYTHON_DIR
    assert StageReciprocationStartRequest().port == LINUX_MODBUS_PORT


def test_production_configs_use_linux_serial_and_mvs_paths() -> None:
    camera = load_yaml_unique(PROJECT_ROOT / "config" / "camera.yaml")["camera"]
    autofocus = load_yaml_unique(PROJECT_ROOT / "config" / "autofocus.yaml")
    objectives = load_yaml_unique(PROJECT_ROOT / "config" / "objectives.yaml")
    handoff = load_yaml_unique(PROJECT_ROOT / "config" / "handoff.yaml")["handoff"]

    assert camera["mvs_python_dir"] == LINUX_MVS_PYTHON_DIR
    assert autofocus["camera"]["mvs_python_dir"] == LINUX_MVS_PYTHON_DIR
    assert autofocus["motor"]["port"] == LINUX_MODBUS_PORT
    assert objectives["hardware"]["modbus"]["port"] == LINUX_MODBUS_PORT
    assert handoff["hardware"]["modbus"]["port"] == LINUX_MODBUS_PORT
    assert str(autofocus["output"]["image_path"]).startswith("/opt/colony_system/")
    assert str(autofocus["output"]["log_path"]).startswith("/opt/colony_system/")


def test_mvs_candidates_include_official_linux_install_layout() -> None:
    from devices.mvs_runtime import mvs_python_dir_candidates

    texts = [str(path).replace("\\", "/") for path in mvs_python_dir_candidates()]
    assert any(text.endswith("Samples/64/Python/MvImport") for text in texts)
    assert any(text.endswith("MvImport") for text in texts)


def test_mvs_native_preload_is_safe_without_sdk() -> None:
    from devices.mvs_runtime import ensure_mvs_native_libraries

    ensure_mvs_native_libraries()


def test_mvcam_common_runenv_points_at_lib_root_for_official_wrapper(tmp_path, monkeypatch) -> None:
    from devices.mvs_runtime import resolve_mvcam_common_runenv

    install = tmp_path / "MVS"
    lib64 = install / "lib" / "64"
    lib64.mkdir(parents=True)
    (lib64 / "libMvCameraControl.so.4.8.0.3").write_bytes(b"")
    monkeypatch.setenv("MVCAM_COMMON_RUNENV", str(install))
    assert resolve_mvcam_common_runenv() == install / "lib"


def test_start_script_sets_mvcam_runenv_to_lib_root() -> None:
    script = (PROJECT_ROOT / "start_api.sh").read_text(encoding="utf-8")
    assert 'MVCAM_COMMON_RUNENV="${MVS_ROOT}/lib"' in script


def test_linux_autofocus_output_paths_are_treated_as_absolute() -> None:
    from workflow.config_validator import _is_absolute_config_path

    assert _is_absolute_config_path("/opt/colony_system/data/autofocus/sharpest.png")
    assert _is_absolute_config_path("/opt/colony_system/data/autofocus/focus_log.csv")


def test_log_sanitizer_redacts_posix_absolute_paths(monkeypatch) -> None:
    from workflow.log_sanitizer import sanitize_log_detail

    monkeypatch.setenv("COLONY_LOG_REDACT_SENSITIVE", "1")
    sanitized = sanitize_log_detail("opened /opt/colony_system/data/secret-task.json")
    assert "secret-task.json" not in sanitized
    assert "/opt/colony_system/data" not in sanitized
    assert "<ABS_PATH>/<redacted>" in sanitized or "<DATA_ROOT>/<redacted>" in sanitized

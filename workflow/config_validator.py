from __future__ import annotations

import argparse
import ipaddress
from dataclasses import dataclass
from pathlib import Path, PureWindowsPath
from typing import Any, Iterable, Mapping

import yaml


EXPECTED_PLATE_TYPES = ("6-well", "12-well", "24-well", "48-well")
EXPECTED_HANDOFF_ACTIONS = ("load_in", "unload_out")
MVS_CAMERA_CONTROL_FILE = "MvCameraControl_class.py"
SUPPORTED_CAMERA_TRIGGER_MODES = {"software"}
SUPPORTED_CAMERA_PIXEL_FORMATS = {"mono8"}
SUPPORTED_CAMERA_SAVE_FORMATS = {"bmp", "png", "jpg", "jpeg", "tif", "tiff"}
SUPPORTED_AUTOFOCUS_BACKENDS = {"mvs"}
SUPPORTED_AUTOFOCUS_MOTOR_TYPES = {"modbus"}
SUPPORTED_AUTOFOCUS_TRIGGER_SCOPES = {"disabled", "once_per_task", "once_per_well"}
SUPPORTED_AUTOFOCUS_RUN_AT = {"before_first_capture_after_stage_move"}
LEGACY_PLATE_FIELDS = {
    "point_12",
    "point_12_d",
    "point_12_gap",
    "rpm_mm",
    "axis_mapping",
}
LEGACY_AUTOFOCUS_MOTOR_FIELDS = {"min_pos", "max_pos"}


@dataclass(frozen=True)
class ConfigIssue:
    path: str
    message: str

    def format(self) -> str:
        return f"{self.path}: {self.message}"


class ConfigValidationError(ValueError):
    def __init__(self, issues: Iterable[ConfigIssue]) -> None:
        self.issues = list(issues)
        super().__init__("\n".join(issue.format() for issue in self.issues))


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[Any, Any]:
    mapping: dict[Any, Any] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            line = key_node.start_mark.line + 1
            raise ConfigValidationError(
                [
                    ConfigIssue(
                        path=f"line {line}",
                        message=f"duplicate YAML key {key!r}",
                    )
                ]
            )
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def load_yaml_unique(path: str | Path) -> dict[str, Any]:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as f:
        data = yaml.load(f, Loader=_UniqueKeyLoader)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigValidationError([ConfigIssue(str(config_path), "top-level YAML value must be a mapping")])
    return data


def validate_plates_file(path: str | Path) -> dict[str, Any]:
    config = load_yaml_unique(path)
    validate_plates_config(config)
    return config


def validate_camera_file(path: str | Path, objectives_path: str | Path | None = None) -> dict[str, Any]:
    config = load_yaml_unique(path)
    validate_camera_config(
        config,
        require_top_level=True,
        objective_names=_load_objective_names(_resolve_objectives_path(path, objectives_path)),
    )
    return config


def validate_handoff_file(path: str | Path) -> dict[str, Any]:
    config = load_yaml_unique(path)
    validate_handoff_config(config)
    return config


def validate_autofocus_file(
    path: str | Path,
    objectives_path: str | Path | None = None,
    camera_path: str | Path | None = None,
) -> dict[str, Any]:
    config = load_yaml_unique(path)
    objectives_cfg = load_yaml_unique(_resolve_objectives_path(path, objectives_path))

    resolved_camera_path = _resolve_camera_path(path, camera_path)
    camera_cfg = None
    if camera_path is not None or resolved_camera_path.exists():
        camera_cfg = load_yaml_unique(resolved_camera_path)

    validate_autofocus_config(config, objectives_cfg=objectives_cfg, camera_cfg=camera_cfg)
    return config


def resolve_mvs_python_dir(camera_cfg: Mapping[str, Any]) -> str | None:
    """Return the effective MVS Python import directory.

    mvs_python_dir is the canonical field. mvs_sdk_path is accepted only as a
    legacy alias while old local configs are phased out.
    """
    for key in ("mvs_python_dir", "mvs_sdk_path"):
        value = camera_cfg.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _resolve_objectives_path(camera_path: str | Path, objectives_path: str | Path | None) -> Path:
    if objectives_path is not None:
        return Path(objectives_path)

    camera_path = Path(camera_path)
    sibling = camera_path.with_name("objectives.yaml")
    if sibling.exists():
        return sibling
    return Path(__file__).resolve().parent.parent / "config" / "objectives.yaml"


def _resolve_camera_path(config_path: str | Path, camera_path: str | Path | None) -> Path:
    if camera_path is not None:
        return Path(camera_path)

    config_path = Path(config_path)
    sibling = config_path.with_name("camera.yaml")
    if sibling.exists():
        return sibling
    return Path(__file__).resolve().parent.parent / "config" / "camera.yaml"


def _load_objective_names(objectives_path: str | Path) -> set[str]:
    config = load_yaml_unique(objectives_path)
    objectives = config.get("objectives")
    if not isinstance(objectives, Mapping) or not objectives:
        raise ConfigValidationError([ConfigIssue("objectives", f"required non-empty mapping is missing: {objectives_path}")])
    return {str(name) for name in objectives.keys()}


def validate_camera_config(
    config: Mapping[str, Any],
    *,
    require_top_level: bool = False,
    objective_names: Iterable[str] | None = None,
) -> None:
    issues: list[ConfigIssue] = []

    if require_top_level:
        camera = config.get("camera")
        if not isinstance(camera, Mapping):
            raise ConfigValidationError([ConfigIssue("camera", "required mapping is missing")])
    else:
        camera = config.get("camera") if "camera" in config else config
        if not isinstance(camera, Mapping):
            raise ConfigValidationError([ConfigIssue("camera", "required mapping is missing")])

    _validate_camera_mvs_path("camera", camera, issues)
    _require_int(camera, "device_index", "camera.device_index", issues, minimum=0)
    if "serial_number" in camera and camera.get("serial_number") is not None:
        _require_text(camera, "serial_number", "camera.serial_number", issues)
    if "ip" in camera and camera.get("ip") is not None:
        _require_ipv4(camera, "ip", "camera.ip", issues)

    resolution = camera.get("resolution")
    if not isinstance(resolution, Mapping):
        issues.append(ConfigIssue("camera.resolution", "required mapping is missing"))
    else:
        _validate_camera_resolution("camera.resolution", resolution, issues)

    _require_number(camera, "exposure_us", "camera.exposure_us", issues, minimum=0, exclusive_min=True)
    _require_number(camera, "gain", "camera.gain", issues, minimum=0, exclusive_min=False)

    objective_settings = camera.get("objective_settings")
    if not isinstance(objective_settings, Mapping) or not objective_settings:
        issues.append(ConfigIssue("camera.objective_settings", "required non-empty mapping is missing"))
    else:
        actual_objectives = {str(name) for name in objective_settings.keys()}
        for objective_name, objective_cfg in objective_settings.items():
            objective_path = f"camera.objective_settings.{objective_name}"
            if not isinstance(objective_cfg, Mapping):
                issues.append(ConfigIssue(objective_path, "objective camera config must be a mapping"))
                continue
            _require_number(objective_cfg, "exposure_us", f"{objective_path}.exposure_us", issues, minimum=0, exclusive_min=True)
            _require_number(objective_cfg, "gain", f"{objective_path}.gain", issues, minimum=0, exclusive_min=False)
        if objective_names is not None:
            expected_objectives = {str(name) for name in objective_names}
            for missing in sorted(expected_objectives - actual_objectives):
                issues.append(ConfigIssue(f"camera.objective_settings.{missing}", "required objective camera setting is missing"))
            for extra in sorted(actual_objectives - expected_objectives):
                issues.append(ConfigIssue(f"camera.objective_settings.{extra}", "objective is not defined in objectives.yaml"))

    _require_choice(camera, "trigger_mode", "camera.trigger_mode", issues, SUPPORTED_CAMERA_TRIGGER_MODES)
    _require_choice(camera, "pixel_format", "camera.pixel_format", issues, SUPPORTED_CAMERA_PIXEL_FORMATS)
    _require_choice(camera, "save_format", "camera.save_format", issues, SUPPORTED_CAMERA_SAVE_FORMATS)

    save_options = camera.get("save_options")
    if not isinstance(save_options, Mapping):
        issues.append(ConfigIssue("camera.save_options", "required mapping is missing"))
    else:
        _require_bool(save_options, "create_dir_if_missing", "camera.save_options.create_dir_if_missing", issues)
        _require_bool(save_options, "overwrite", "camera.save_options.overwrite", issues)

    if issues:
        raise ConfigValidationError(issues)


def validate_autofocus_config(
    config: Mapping[str, Any],
    *,
    objectives_cfg: Mapping[str, Any] | None = None,
    camera_cfg: Mapping[str, Any] | None = None,
) -> None:
    issues: list[ConfigIssue] = []

    root = config.get("autofocus") if "autofocus" in config else config
    if not isinstance(root, Mapping):
        raise ConfigValidationError([ConfigIssue("autofocus", "required mapping is missing")])

    objectives = _objectives_from_config(objectives_cfg, issues)
    objective_names = {str(name) for name in objectives.keys()} if objectives is not None else None
    project_camera = _project_camera_from_config(camera_cfg, issues)

    _require_bool(root, "enabled", "autofocus.enabled", issues)
    if "force" in root:
        _require_bool(root, "force", "autofocus.force", issues)

    trigger = root.get("trigger")
    if not isinstance(trigger, Mapping):
        issues.append(ConfigIssue("autofocus.trigger", "required mapping is missing"))
    else:
        _validate_autofocus_trigger("autofocus.trigger", trigger, issues, objective_names)

    mode = root.get("mode")
    if mode is not None:
        if not isinstance(mode, Mapping):
            issues.append(ConfigIssue("autofocus.mode", "must be a mapping when provided"))
        elif "preview" in mode:
            preview = _require_bool(mode, "preview", "autofocus.mode.preview", issues)
            if preview is True:
                issues.append(
                    ConfigIssue(
                        "autofocus.mode.preview",
                        "preview mode is not safe in the workflow entry; use the third-party CLI for camera preview",
                    )
                )

    camera = root.get("camera")
    if not isinstance(camera, Mapping):
        issues.append(ConfigIssue("autofocus.camera", "required mapping is missing"))
    else:
        _validate_autofocus_camera("autofocus.camera", camera, issues, objective_names, project_camera)

    motor = root.get("motor")
    if not isinstance(motor, Mapping):
        issues.append(ConfigIssue("autofocus.motor", "required mapping is missing"))
    else:
        _validate_autofocus_motor("autofocus.motor", motor, issues, objectives, objectives_cfg)

    focus = root.get("focus")
    if not isinstance(focus, Mapping):
        issues.append(ConfigIssue("autofocus.focus", "required mapping is missing"))
    else:
        _validate_autofocus_focus("autofocus.focus", focus, issues)

    output = root.get("output")
    if not isinstance(output, Mapping):
        issues.append(ConfigIssue("autofocus.output", "required mapping is missing"))
    else:
        _validate_autofocus_output("autofocus.output", output, issues)

    if issues:
        raise ConfigValidationError(issues)


def validate_plates_config(config: Mapping[str, Any]) -> None:
    issues: list[ConfigIssue] = []

    plates = config.get("plates")
    if not isinstance(plates, Mapping):
        raise ConfigValidationError([ConfigIssue("plates", "required mapping is missing")])

    actual_types = set(str(k) for k in plates.keys())
    expected_types = set(EXPECTED_PLATE_TYPES)
    for missing in sorted(expected_types - actual_types):
        issues.append(ConfigIssue(f"plates.{missing}", "required plate type is missing"))
    for extra in sorted(actual_types - expected_types):
        issues.append(ConfigIssue(f"plates.{extra}", "unexpected plate type or misplaced top-level field"))

    for plate_type in EXPECTED_PLATE_TYPES:
        plate = plates.get(plate_type)
        if not isinstance(plate, Mapping):
            continue
        _validate_plate(plate_type, plate, issues)

    if issues:
        raise ConfigValidationError(issues)


def validate_handoff_config(config: Mapping[str, Any]) -> None:
    issues: list[ConfigIssue] = []

    root = config.get("handoff") if "handoff" in config else config
    if not isinstance(root, Mapping):
        raise ConfigValidationError([ConfigIssue("handoff", "required mapping is missing")])

    hardware = root.get("hardware")
    if not isinstance(hardware, Mapping):
        issues.append(ConfigIssue("handoff.hardware", "required mapping is missing"))
    else:
        _validate_handoff_hardware("handoff.hardware", hardware, issues)

    points = root.get("points")
    point_names: set[str] = set()
    if not isinstance(points, Mapping) or not points:
        issues.append(ConfigIssue("handoff.points", "required non-empty mapping is missing"))
    else:
        point_names = {str(name) for name in points.keys()}
        for point_name, point_cfg in points.items():
            if not isinstance(point_cfg, Mapping):
                issues.append(ConfigIssue(f"handoff.points.{point_name}", "point config must be a mapping"))
                continue
            _validate_handoff_point(f"handoff.points.{point_name}", point_cfg, issues, require_motion=True)

    plate_overrides = root.get("plate_overrides")
    if plate_overrides is not None:
        if not isinstance(plate_overrides, Mapping):
            issues.append(ConfigIssue("handoff.plate_overrides", "must be a mapping when provided"))
        else:
            _validate_handoff_plate_overrides(plate_overrides, point_names, issues)

    actions = root.get("actions")
    if not isinstance(actions, Mapping):
        issues.append(ConfigIssue("handoff.actions", "required mapping is missing"))
    else:
        for action in EXPECTED_HANDOFF_ACTIONS:
            action_cfg = actions.get(action)
            action_path = f"handoff.actions.{action}"
            if not isinstance(action_cfg, Mapping):
                issues.append(ConfigIssue(action_path, "required action mapping is missing"))
                continue
            point_name = _require_text(action_cfg, "point", f"{action_path}.point", issues)
            if point_name is not None and point_names and point_name not in point_names:
                issues.append(ConfigIssue(f"{action_path}.point", f"references undefined point {point_name!r}"))
            _require_text(action_cfg, "ready_state", f"{action_path}.ready_state", issues)
            if "message" in action_cfg and action_cfg.get("message") is not None:
                _require_text(action_cfg, "message", f"{action_path}.message", issues)

        for action in sorted(str(k) for k in actions.keys()):
            if action not in EXPECTED_HANDOFF_ACTIONS:
                issues.append(ConfigIssue(f"handoff.actions.{action}", "unexpected handoff action"))

    if issues:
        raise ConfigValidationError(issues)


def _objectives_from_config(
    objectives_cfg: Mapping[str, Any] | None,
    issues: list[ConfigIssue],
) -> Mapping[str, Any] | None:
    if objectives_cfg is None:
        return None
    objectives = objectives_cfg.get("objectives")
    if not isinstance(objectives, Mapping) or not objectives:
        issues.append(ConfigIssue("objectives.objectives", "required non-empty mapping is missing"))
        return None
    return objectives


def _project_camera_from_config(
    camera_cfg: Mapping[str, Any] | None,
    issues: list[ConfigIssue],
) -> Mapping[str, Any] | None:
    if camera_cfg is None:
        return None
    camera = camera_cfg.get("camera") if "camera" in camera_cfg else camera_cfg
    if not isinstance(camera, Mapping):
        issues.append(ConfigIssue("camera", "required mapping is missing"))
        return None
    return camera


def _validate_autofocus_trigger(
    base: str,
    trigger: Mapping[str, Any],
    issues: list[ConfigIssue],
    objective_names: set[str] | None,
) -> None:
    _require_bool(trigger, "after_objective_switch", f"{base}.after_objective_switch", issues)
    _require_bool(trigger, "always_before_capture", f"{base}.always_before_capture", issues)
    _require_choice(trigger, "scope", f"{base}.scope", issues, SUPPORTED_AUTOFOCUS_TRIGGER_SCOPES)
    _require_choice(trigger, "run_at", f"{base}.run_at", issues, SUPPORTED_AUTOFOCUS_RUN_AT)
    if "force" in trigger:
        _require_bool(trigger, "force", f"{base}.force", issues)

    objectives = trigger.get("always_before_capture_objectives")
    if not isinstance(objectives, list):
        issues.append(ConfigIssue(f"{base}.always_before_capture_objectives", "must be a list"))
        return

    for index, objective in enumerate(objectives):
        path = f"{base}.always_before_capture_objectives[{index}]"
        if not isinstance(objective, str) or not objective.strip():
            issues.append(ConfigIssue(path, "must be a non-empty objective name"))
            continue
        if objective_names is not None and objective.strip() not in objective_names:
            issues.append(ConfigIssue(path, f"objective is not defined in objectives.yaml: {objective!r}"))


def _validate_autofocus_camera(
    base: str,
    camera: Mapping[str, Any],
    issues: list[ConfigIssue],
    objective_names: set[str] | None,
    project_camera: Mapping[str, Any] | None,
) -> None:
    backend = _require_choice(camera, "backend", f"{base}.backend", issues, SUPPORTED_AUTOFOCUS_BACKENDS)
    if backend == "mvs":
        _require_ipv4(camera, "ip", f"{base}.ip", issues)
        _require_ipv4(camera, "net_export_ip", f"{base}.net_export_ip", issues)
        _validate_camera_mvs_path(base, camera, issues)
        if "opencv_index" in camera:
            issues.append(ConfigIssue(f"{base}.opencv_index", "is not used when backend is mvs; remove it from production config"))

    exposure_auto = _require_bool(camera, "exposure_auto", f"{base}.exposure_auto", issues)
    if exposure_auto is not False:
        issues.append(ConfigIssue(f"{base}.exposure_auto", "must be false for comparable autofocus scores"))
    if "exposure_time_us" in camera:
        if exposure_auto is True:
            issues.append(ConfigIssue(f"{base}.exposure_time_us", "cannot be set when exposure_auto is true"))
        _require_number(camera, "exposure_time_us", f"{base}.exposure_time_us", issues, minimum=0, exclusive_min=True)

    objective_settings = camera.get("objective_settings")
    if not isinstance(objective_settings, Mapping) or not objective_settings:
        issues.append(ConfigIssue(f"{base}.objective_settings", "required non-empty mapping is missing"))
    else:
        actual_objectives = {str(name) for name in objective_settings.keys()}
        for objective_name, objective_cfg in objective_settings.items():
            objective_path = f"{base}.objective_settings.{objective_name}"
            if not isinstance(objective_cfg, Mapping):
                issues.append(ConfigIssue(objective_path, "objective autofocus camera config must be a mapping"))
                continue
            objective_exposure_auto = _require_bool(
                objective_cfg,
                "exposure_auto",
                f"{objective_path}.exposure_auto",
                issues,
            )
            if objective_exposure_auto is not False:
                issues.append(ConfigIssue(f"{objective_path}.exposure_auto", "must be false for comparable autofocus scores"))
            _require_number(
                objective_cfg,
                "exposure_time_us",
                f"{objective_path}.exposure_time_us",
                issues,
                minimum=0,
                exclusive_min=True,
            )

        if objective_names is not None:
            for missing in sorted(objective_names - actual_objectives):
                issues.append(ConfigIssue(f"{base}.objective_settings.{missing}", "required objective autofocus camera setting is missing"))
            for extra in sorted(actual_objectives - objective_names):
                issues.append(ConfigIssue(f"{base}.objective_settings.{extra}", "objective is not defined in objectives.yaml"))

    if project_camera is not None:
        _compare_autofocus_camera_with_project_camera(base, camera, project_camera, issues)


def _compare_autofocus_camera_with_project_camera(
    base: str,
    autofocus_camera: Mapping[str, Any],
    project_camera: Mapping[str, Any],
    issues: list[ConfigIssue],
) -> None:
    autofocus_ip = autofocus_camera.get("ip")
    project_ip = project_camera.get("ip")
    if (
        isinstance(autofocus_ip, str)
        and autofocus_ip.strip()
        and isinstance(project_ip, str)
        and project_ip.strip()
        and autofocus_ip.strip() != project_ip.strip()
    ):
        issues.append(ConfigIssue(f"{base}.ip", f"must match camera.ip ({project_ip.strip()})"))

    autofocus_mvs_dir = resolve_mvs_python_dir(autofocus_camera)
    project_mvs_dir = resolve_mvs_python_dir(project_camera)
    if autofocus_mvs_dir and project_mvs_dir and Path(autofocus_mvs_dir) != Path(project_mvs_dir):
        issues.append(ConfigIssue(f"{base}.mvs_python_dir", f"must match camera.mvs_python_dir ({project_mvs_dir})"))

    autofocus_settings = autofocus_camera.get("objective_settings")
    project_settings = project_camera.get("objective_settings")
    if not isinstance(autofocus_settings, Mapping) or not isinstance(project_settings, Mapping):
        return

    for objective_name, autofocus_objective in autofocus_settings.items():
        if not isinstance(autofocus_objective, Mapping):
            continue
        project_objective = project_settings.get(objective_name)
        if not isinstance(project_objective, Mapping):
            continue
        autofocus_exposure = autofocus_objective.get("exposure_time_us")
        project_exposure = project_objective.get("exposure_us")
        if _is_plain_number(autofocus_exposure) and _is_plain_number(project_exposure):
            if float(autofocus_exposure) != float(project_exposure):
                issues.append(
                    ConfigIssue(
                        f"{base}.objective_settings.{objective_name}.exposure_time_us",
                        f"must match camera.objective_settings.{objective_name}.exposure_us ({project_exposure})",
                    )
                )


def _validate_autofocus_motor(
    base: str,
    motor: Mapping[str, Any],
    issues: list[ConfigIssue],
    objectives: Mapping[str, Any] | None,
    objectives_cfg: Mapping[str, Any] | None,
) -> None:
    _require_choice(motor, "type", f"{base}.type", issues, SUPPORTED_AUTOFOCUS_MOTOR_TYPES)
    port = _require_text(motor, "port", f"{base}.port", issues)
    baudrate = _require_int(motor, "baudrate", f"{base}.baudrate", issues, minimum=1)
    focus_slave = _require_int(motor, "focus_slave", f"{base}.focus_slave", issues, minimum=1)

    for legacy_key in sorted(LEGACY_AUTOFOCUS_MOTOR_FIELDS):
        if legacy_key in motor:
            issues.append(ConfigIssue(f"{base}.{legacy_key}", "legacy fallback field is not used by the production workflow"))

    objective_names = {str(name) for name in objectives.keys()} if objectives is not None else None
    objective = _require_text(motor, "objective", f"{base}.objective", issues)
    if objective is not None and objective_names is not None and objective not in objective_names:
        issues.append(ConfigIssue(f"{base}.objective", f"objective is not defined in objectives.yaml: {objective!r}"))

    ranges = motor.get("objective_ranges")
    if not isinstance(ranges, Mapping) or not ranges:
        issues.append(ConfigIssue(f"{base}.objective_ranges", "required non-empty mapping is missing"))
    else:
        actual_objectives = {str(name) for name in ranges.keys()}
        if objective_names is not None:
            for missing in sorted(objective_names - actual_objectives):
                issues.append(ConfigIssue(f"{base}.objective_ranges.{missing}", "required objective focus range is missing"))
            for extra in sorted(actual_objectives - objective_names):
                issues.append(ConfigIssue(f"{base}.objective_ranges.{extra}", "objective is not defined in objectives.yaml"))

        for objective_name, range_cfg in ranges.items():
            range_path = f"{base}.objective_ranges.{objective_name}"
            if not isinstance(range_cfg, Mapping):
                issues.append(ConfigIssue(range_path, "objective focus range must be a mapping"))
                continue
            min_pos = _require_number(range_cfg, "min_pos", f"{range_path}.min_pos", issues)
            max_pos = _require_number(range_cfg, "max_pos", f"{range_path}.max_pos", issues)
            if min_pos is not None and max_pos is not None:
                if min_pos >= max_pos:
                    issues.append(ConfigIssue(range_path, "min_pos must be smaller than max_pos"))
                focus_target = _objective_focus_target(objectives, str(objective_name))
                if focus_target is not None and not (min_pos <= focus_target <= max_pos):
                    issues.append(
                        ConfigIssue(
                            range_path,
                            f"must contain objectives.{objective_name}.switch.focus_target_pos ({focus_target:g})",
                        )
                    )

    _require_int(motor, "profile_vel", f"{base}.profile_vel", issues, minimum=1)
    _require_int(motor, "profile_acc", f"{base}.profile_acc", issues, minimum=1)
    _require_int(motor, "profile_dec", f"{base}.profile_dec", issues, minimum=1)

    _compare_autofocus_motor_with_objectives_hardware(
        base,
        issues,
        objectives_cfg,
        port=port,
        baudrate=baudrate,
        focus_slave=focus_slave,
    )


def _compare_autofocus_motor_with_objectives_hardware(
    base: str,
    issues: list[ConfigIssue],
    objectives_cfg: Mapping[str, Any] | None,
    *,
    port: str | None,
    baudrate: int | None,
    focus_slave: int | None,
) -> None:
    if objectives_cfg is None:
        return
    hardware = objectives_cfg.get("hardware")
    if not isinstance(hardware, Mapping):
        return

    modbus = hardware.get("modbus")
    if isinstance(modbus, Mapping):
        objective_port = modbus.get("port")
        if port is not None and isinstance(objective_port, str) and port.strip() != objective_port.strip():
            issues.append(ConfigIssue(f"{base}.port", f"must match objectives.hardware.modbus.port ({objective_port})"))
        objective_baudrate = modbus.get("baudrate")
        if baudrate is not None and isinstance(objective_baudrate, int) and baudrate != objective_baudrate:
            issues.append(ConfigIssue(f"{base}.baudrate", f"must match objectives.hardware.modbus.baudrate ({objective_baudrate})"))

    focus_axis = hardware.get("focus_axis")
    if isinstance(focus_axis, Mapping):
        objective_focus_slave = focus_axis.get("slave")
        if focus_slave is not None and isinstance(objective_focus_slave, int) and focus_slave != objective_focus_slave:
            issues.append(ConfigIssue(f"{base}.focus_slave", f"must match objectives.hardware.focus_axis.slave ({objective_focus_slave})"))


def _objective_focus_target(objectives: Mapping[str, Any] | None, objective_name: str) -> float | None:
    if objectives is None:
        return None
    objective_cfg = objectives.get(objective_name)
    if not isinstance(objective_cfg, Mapping):
        return None
    switch_cfg = objective_cfg.get("switch")
    if not isinstance(switch_cfg, Mapping):
        return None
    focus_target = switch_cfg.get("focus_target_pos")
    if not _is_plain_number(focus_target):
        return None
    return float(focus_target)


def _validate_autofocus_focus(base: str, focus: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    _require_number(focus, "tol", f"{base}.tol", issues, minimum=0, exclusive_min=True)
    _require_int(focus, "max_iter", f"{base}.max_iter", issues, minimum=1)
    _require_number(focus, "settle_ms", f"{base}.settle_ms", issues, minimum=0)

    center_roi = _require_number(focus, "center_roi", f"{base}.center_roi", issues, minimum=0, exclusive_min=True)
    if center_roi is not None and center_roi > 1:
        issues.append(ConfigIssue(f"{base}.center_roi", "must be <= 1"))

    downsample = _require_number(focus, "downsample", f"{base}.downsample", issues, minimum=0, exclusive_min=True)
    if downsample is not None and downsample > 1:
        issues.append(ConfigIssue(f"{base}.downsample", "must be <= 1"))


def _validate_autofocus_output(base: str, output: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    _require_bool(output, "timestamp_folder", f"{base}.timestamp_folder", issues)
    image_path = _require_text(output, "image_path", f"{base}.image_path", issues)
    log_path = _require_text(output, "log_path", f"{base}.log_path", issues)

    if image_path is not None:
        path = Path(image_path)
        if not _is_absolute_config_path(image_path):
            issues.append(ConfigIssue(f"{base}.image_path", "must be absolute because third-party relative paths resolve under third_party/XWJJJ260511"))
        if path.suffix.lower() not in {".bmp", ".png", ".jpg", ".jpeg", ".tif", ".tiff"}:
            issues.append(ConfigIssue(f"{base}.image_path", "must use an image extension"))

    if log_path is not None:
        path = Path(log_path)
        if not _is_absolute_config_path(log_path):
            issues.append(ConfigIssue(f"{base}.log_path", "must be absolute because third-party relative paths resolve under third_party/XWJJJ260511"))
        if path.suffix.lower() != ".csv":
            issues.append(ConfigIssue(f"{base}.log_path", "must be a .csv file"))


def _is_absolute_config_path(value: str) -> bool:
    return Path(value).is_absolute() or PureWindowsPath(value).is_absolute()


def _is_plain_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float))


def _validate_plate(plate_type: str, plate: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    base = f"plates.{plate_type}"

    for legacy_key in sorted(LEGACY_PLATE_FIELDS):
        if legacy_key in plate:
            issues.append(ConfigIssue(f"{base}.{legacy_key}", "legacy field is not used by the current workflow"))

    _require_int(plate, "rows", f"{base}.rows", issues, minimum=1)
    _require_int(plate, "cols", f"{base}.cols", issues, minimum=1)

    a1_start = plate.get("a1_start")
    if not isinstance(a1_start, Mapping):
        issues.append(ConfigIssue(f"{base}.a1_start", "required mapping is missing"))
    else:
        _require_int(a1_start, "x", f"{base}.a1_start.x", issues)
        _require_int(a1_start, "y", f"{base}.a1_start.y", issues)

    _require_number(plate, "well_diameter_mm", f"{base}.well_diameter_mm", issues, minimum=0, exclusive_min=True)
    _require_number(plate, "well_gap_mm", f"{base}.well_gap_mm", issues, minimum=0, exclusive_min=False)
    _require_number(plate, "pulses_per_mm", f"{base}.pulses_per_mm", issues, minimum=0, exclusive_min=True)

    for key in (
        "row_stage_sign",
        "col_stage_sign",
        "x_stage_sign_for_view_down",
        "y_stage_sign_for_view_right",
    ):
        _require_sign(plate, key, f"{base}.{key}", issues)

    stage_limits = plate.get("stage_limits")
    if not isinstance(stage_limits, Mapping):
        issues.append(ConfigIssue(f"{base}.stage_limits", "required mapping is missing"))
    else:
        _validate_stage_limits(f"{base}.stage_limits", stage_limits, issues)

    runtime_guard = plate.get("runtime_guard")
    if not isinstance(runtime_guard, Mapping):
        issues.append(ConfigIssue(f"{base}.runtime_guard", "required mapping is missing"))
    else:
        _validate_runtime_guard(f"{base}.runtime_guard", runtime_guard, issues)


def _validate_stage_limits(base: str, cfg: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    _require_bool(cfg, "enabled", f"{base}.enabled", issues)
    x_min = _require_int(cfg, "x_min", f"{base}.x_min", issues)
    x_max = _require_int(cfg, "x_max", f"{base}.x_max", issues)
    y_min = _require_int(cfg, "y_min", f"{base}.y_min", issues)
    y_max = _require_int(cfg, "y_max", f"{base}.y_max", issues)
    _require_int(cfg, "safety_margin", f"{base}.safety_margin", issues, minimum=0)

    if x_min is not None and x_max is not None and x_min >= x_max:
        issues.append(ConfigIssue(base, "x_min must be smaller than x_max"))
    if y_min is not None and y_max is not None and y_min >= y_max:
        issues.append(ConfigIssue(base, "y_min must be smaller than y_max"))


def _validate_runtime_guard(base: str, cfg: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    _require_bool(cfg, "enabled", f"{base}.enabled", issues)
    _require_int(cfg, "stuck_min_expected_move_pulse", f"{base}.stuck_min_expected_move_pulse", issues, minimum=0)
    _require_int(cfg, "stuck_max_actual_move_pulse", f"{base}.stuck_max_actual_move_pulse", issues, minimum=0)
    _require_int(cfg, "max_err_to_target_pulse", f"{base}.max_err_to_target_pulse", issues, minimum=0)
    _require_bool(cfg, "abort_on_motion_failure", f"{base}.abort_on_motion_failure", issues)


def _validate_camera_mvs_path(base: str, cfg: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    mvs_python_dir = cfg.get("mvs_python_dir")
    mvs_sdk_path = cfg.get("mvs_sdk_path")

    if "mvs_python_dir" in cfg and not (isinstance(mvs_python_dir, str) and mvs_python_dir.strip()):
        issues.append(ConfigIssue(f"{base}.mvs_python_dir", "must be a non-empty string"))
    if "mvs_sdk_path" in cfg and not (isinstance(mvs_sdk_path, str) and mvs_sdk_path.strip()):
        issues.append(ConfigIssue(f"{base}.mvs_sdk_path", "legacy alias must be a non-empty string when provided"))

    if (
        isinstance(mvs_python_dir, str)
        and mvs_python_dir.strip()
        and isinstance(mvs_sdk_path, str)
        and mvs_sdk_path.strip()
        and Path(mvs_python_dir.strip()) != Path(mvs_sdk_path.strip())
    ):
        issues.append(ConfigIssue(f"{base}.mvs_sdk_path", "legacy alias conflicts with mvs_python_dir; keep only mvs_python_dir"))

    resolved = resolve_mvs_python_dir(cfg)
    if resolved is None:
        issues.append(ConfigIssue(f"{base}.mvs_python_dir", "required MVS Python SDK import directory is missing"))
        return

    sdk_dir = Path(resolved)
    if not sdk_dir.exists():
        issues.append(ConfigIssue(f"{base}.mvs_python_dir", f"path does not exist: {resolved}"))
        return
    if not sdk_dir.is_dir():
        issues.append(ConfigIssue(f"{base}.mvs_python_dir", f"path is not a directory: {resolved}"))
        return

    sdk_entrypoint = sdk_dir / MVS_CAMERA_CONTROL_FILE
    if not sdk_entrypoint.is_file():
        issues.append(ConfigIssue(f"{base}.mvs_python_dir", f"missing {MVS_CAMERA_CONTROL_FILE} in SDK import directory"))


def _validate_camera_resolution(base: str, cfg: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    _require_int(cfg, "width", f"{base}.width", issues, minimum=1)
    _require_int(cfg, "height", f"{base}.height", issues, minimum=1)
    if "allow_downscale" in cfg:
        _require_bool(cfg, "allow_downscale", f"{base}.allow_downscale", issues)

    presets = cfg.get("presets")
    if presets is None:
        return
    if not isinstance(presets, Mapping):
        issues.append(ConfigIssue(f"{base}.presets", "must be a mapping when provided"))
        return
    for preset_name, preset_cfg in presets.items():
        preset_path = f"{base}.presets.{preset_name}"
        if not isinstance(preset_cfg, Mapping):
            issues.append(ConfigIssue(preset_path, "preset must be a mapping"))
            continue
        _require_int(preset_cfg, "width", f"{preset_path}.width", issues, minimum=1)
        _require_int(preset_cfg, "height", f"{preset_path}.height", issues, minimum=1)


def _validate_handoff_hardware(base: str, cfg: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    modbus = cfg.get("modbus")
    if not isinstance(modbus, Mapping):
        issues.append(ConfigIssue(f"{base}.modbus", "required mapping is missing"))
    else:
        _require_text(modbus, "port", f"{base}.modbus.port", issues)
        _require_int(modbus, "baudrate", f"{base}.modbus.baudrate", issues, minimum=1)

    x_axis = cfg.get("x_axis")
    y_axis = cfg.get("y_axis")
    x_slave = None
    y_slave = None
    if not isinstance(x_axis, Mapping):
        issues.append(ConfigIssue(f"{base}.x_axis", "required mapping is missing"))
    else:
        x_slave = _require_int(x_axis, "slave", f"{base}.x_axis.slave", issues, minimum=1)
    if not isinstance(y_axis, Mapping):
        issues.append(ConfigIssue(f"{base}.y_axis", "required mapping is missing"))
    else:
        y_slave = _require_int(y_axis, "slave", f"{base}.y_axis.slave", issues, minimum=1)

    if x_slave is not None and y_slave is not None and x_slave == y_slave:
        issues.append(ConfigIssue(base, "x_axis.slave and y_axis.slave must be different"))


def _validate_handoff_point(
    base: str,
    cfg: Mapping[str, Any],
    issues: list[ConfigIssue],
    *,
    require_motion: bool,
) -> None:
    _require_int(cfg, "x", f"{base}.x", issues)
    _require_int(cfg, "y", f"{base}.y", issues)
    if "meaning" in cfg and cfg.get("meaning") is not None:
        _require_text(cfg, "meaning", f"{base}.meaning", issues)

    motion_fields = (
        ("profile_vel", "int", 1),
        ("profile_acc", "int", 1),
        ("profile_dec", "int", 1),
        ("timeout_s", "number", 0),
        ("settle_s", "number", 0),
        ("arrival_tolerance_pulse", "int", 0),
    )
    for key, value_type, minimum in motion_fields:
        if key not in cfg:
            if require_motion:
                issues.append(ConfigIssue(f"{base}.{key}", "required field is missing"))
            continue
        if value_type == "int":
            _require_int(cfg, key, f"{base}.{key}", issues, minimum=int(minimum))
        else:
            _require_number(cfg, key, f"{base}.{key}", issues, minimum=float(minimum), exclusive_min=(key == "timeout_s"))


def _validate_handoff_plate_overrides(
    plate_overrides: Mapping[str, Any],
    point_names: set[str],
    issues: list[ConfigIssue],
) -> None:
    expected_plates = set(EXPECTED_PLATE_TYPES)
    for plate_type, override_map in plate_overrides.items():
        plate_path = f"handoff.plate_overrides.{plate_type}"
        if str(plate_type) not in expected_plates:
            issues.append(ConfigIssue(plate_path, "unexpected plate type"))
        if not isinstance(override_map, Mapping):
            issues.append(ConfigIssue(plate_path, "plate override must be a mapping"))
            continue
        for point_name, point_cfg in override_map.items():
            point_path = f"{plate_path}.{point_name}"
            if point_names and str(point_name) not in point_names:
                issues.append(ConfigIssue(point_path, "references undefined point"))
            if not isinstance(point_cfg, Mapping):
                issues.append(ConfigIssue(point_path, "point override must be a mapping"))
                continue
            _validate_handoff_point_override(point_path, point_cfg, issues)


def _require_text(cfg: Mapping[str, Any], key: str, path: str, issues: list[ConfigIssue]) -> str | None:
    value = cfg.get(key)
    if isinstance(value, str) and value.strip():
        return value
    issues.append(ConfigIssue(path, "must be a non-empty string"))
    return None


def _require_ipv4(cfg: Mapping[str, Any], key: str, path: str, issues: list[ConfigIssue]) -> str | None:
    value = _require_text(cfg, key, path, issues)
    if value is None:
        return None
    try:
        ipaddress.IPv4Address(value.strip())
    except ValueError:
        issues.append(ConfigIssue(path, "must be a valid IPv4 address"))
        return None
    return value.strip()


def _validate_handoff_point_override(base: str, cfg: Mapping[str, Any], issues: list[ConfigIssue]) -> None:
    if "x" in cfg:
        _require_int(cfg, "x", f"{base}.x", issues)
    if "y" in cfg:
        _require_int(cfg, "y", f"{base}.y", issues)
    if "meaning" in cfg and cfg.get("meaning") is not None:
        _require_text(cfg, "meaning", f"{base}.meaning", issues)

    int_fields = ("profile_vel", "profile_acc", "profile_dec", "arrival_tolerance_pulse")
    for key in int_fields:
        if key in cfg:
            _require_int(cfg, key, f"{base}.{key}", issues, minimum=0 if key == "arrival_tolerance_pulse" else 1)

    if "timeout_s" in cfg:
        _require_number(cfg, "timeout_s", f"{base}.timeout_s", issues, minimum=0, exclusive_min=True)
    if "settle_s" in cfg:
        _require_number(cfg, "settle_s", f"{base}.settle_s", issues, minimum=0, exclusive_min=False)


def _require_bool(cfg: Mapping[str, Any], key: str, path: str, issues: list[ConfigIssue]) -> bool | None:
    value = cfg.get(key)
    if isinstance(value, bool):
        return value
    issues.append(ConfigIssue(path, "must be a boolean"))
    return None


def _require_sign(cfg: Mapping[str, Any], key: str, path: str, issues: list[ConfigIssue]) -> int | None:
    value = _require_int(cfg, key, path, issues)
    if value is None:
        return None
    if value not in (-1, 1):
        issues.append(ConfigIssue(path, "must be -1 or 1"))
        return None
    return value


def _require_choice(
    cfg: Mapping[str, Any],
    key: str,
    path: str,
    issues: list[ConfigIssue],
    choices: set[str],
) -> str | None:
    value = _require_text(cfg, key, path, issues)
    if value is None:
        return None
    normalized = value.strip().lower()
    if normalized not in choices:
        allowed = ", ".join(sorted(choices))
        issues.append(ConfigIssue(path, f"must be one of: {allowed}"))
        return None
    return normalized


def _require_int(
    cfg: Mapping[str, Any],
    key: str,
    path: str,
    issues: list[ConfigIssue],
    *,
    minimum: int | None = None,
) -> int | None:
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        issues.append(ConfigIssue(path, "must be an integer"))
        return None
    if minimum is not None and value < minimum:
        issues.append(ConfigIssue(path, f"must be >= {minimum}"))
        return None
    return value


def _require_number(
    cfg: Mapping[str, Any],
    key: str,
    path: str,
    issues: list[ConfigIssue],
    *,
    minimum: float | None = None,
    exclusive_min: bool = False,
) -> float | None:
    value = cfg.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        issues.append(ConfigIssue(path, "must be a number"))
        return None
    number = float(value)
    if minimum is not None:
        invalid = number <= minimum if exclusive_min else number < minimum
        if invalid:
            op = ">" if exclusive_min else ">="
            issues.append(ConfigIssue(path, f"must be {op} {minimum:g}"))
            return None
    return number


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate colony system YAML configuration files")
    parser.add_argument(
        "--plates",
        default=None,
        help="Path to config/plates.yaml",
    )
    parser.add_argument(
        "--camera",
        default=None,
        help="Path to config/camera.yaml",
    )
    parser.add_argument(
        "--objectives",
        default=None,
        help="Path to config/objectives.yaml, used when validating camera objective_settings",
    )
    parser.add_argument(
        "--handoff",
        default=None,
        help="Path to config/handoff.yaml",
    )
    parser.add_argument(
        "--autofocus",
        default=None,
        help="Path to config/autofocus.yaml",
    )
    args = parser.parse_args()

    project_config_dir = Path(__file__).resolve().parent.parent / "config"
    targets = []
    if args.plates is None and args.camera is None and args.handoff is None and args.autofocus is None:
        targets.extend(
            [
                ("camera", project_config_dir / "camera.yaml"),
                ("plates", project_config_dir / "plates.yaml"),
                ("autofocus", project_config_dir / "autofocus.yaml"),
                ("handoff", project_config_dir / "handoff.yaml"),
            ]
        )
    else:
        if args.plates is not None:
            targets.append(("plates", Path(args.plates)))
        if args.camera is not None:
            targets.append(("camera", Path(args.camera)))
        if args.autofocus is not None:
            targets.append(("autofocus", Path(args.autofocus)))
        if args.handoff is not None:
            targets.append(("handoff", Path(args.handoff)))

    for kind, path in targets:
        if kind == "camera":
            validate_camera_file(path, objectives_path=args.objectives or project_config_dir / "objectives.yaml")
        elif kind == "plates":
            validate_plates_file(path)
        elif kind == "autofocus":
            validate_autofocus_file(
                path,
                objectives_path=args.objectives or project_config_dir / "objectives.yaml",
                camera_path=args.camera or project_config_dir / "camera.yaml",
            )
        elif kind == "handoff":
            validate_handoff_file(path)
        print(f"OK: {path}")


if __name__ == "__main__":
    main()

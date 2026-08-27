"""Strict model manifest validation and ONNX Runtime session loading.

The 4x model entrypoint never falls back to the legacy OpenCV detector.  A
missing model, checksum mismatch, incompatible graph, or unavailable provider
is an error so that a model failure cannot be reported as "zero colonies".
"""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import json
from pathlib import Path
from math import isfinite
from typing import Any, Dict, Iterable, Sequence


MANIFEST_NAME = "model_manifest.json"
SUPPORTED_SCHEMA_VERSION = 1
REQUIRED_INFERENCE_KEYS = {
    "tile_enabled",
    "tile_size",
    "tile_overlap",
    "max_tile_count",
    "letterbox_value",
    "detector_review_threshold",
    "detector_accept_threshold",
    "detector_merge_iou",
    "detector_merge_containment",
    "detector_merge_center_ratio",
    "roi_pad_ratio",
    "segment_threshold",
    "segment_review_score",
    "min_mask_inside_detector_ratio",
    "mask_morphology_px",
    "mask_duplicate_iom",
    "min_mask_area_px",
    "min_connected_component_area_px",
    "detector_gpu_memory_limit_mb",
    "segmenter_gpu_memory_limit_mb",
}


class VisionModelError(RuntimeError):
    """The model contract, model files, or inference runtime is invalid."""


class VisionInputQualityError(VisionModelError):
    """The image is outside the release's validated acquisition envelope."""


def _positive_int_pair(value: Any, name: str) -> tuple[int, int]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)) or len(value) != 2:
        raise VisionModelError(f"{name} must be [width, height]")
    try:
        width, height = int(value[0]), int(value[1])
    except Exception as exc:
        raise VisionModelError(f"{name} must contain integers") from exc
    if width <= 0 or height <= 0:
        raise VisionModelError(f"{name} values must be positive")
    return width, height


def _float_list(value: Any, name: str, channels: int) -> tuple[float, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise VisionModelError(f"{name} must be a list")
    try:
        out = tuple(float(x) for x in value)
    except Exception as exc:
        raise VisionModelError(f"{name} must contain numbers") from exc
    if len(out) not in (1, channels):
        raise VisionModelError(f"{name} must contain 1 or {channels} values")
    return out * channels if len(out) == 1 else out


def _sha256_file(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _required_string(raw: Dict[str, Any], key: str, prefix: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise VisionModelError(f"{prefix}.{key} is required")
    return value.strip()


@dataclass(frozen=True)
class OnnxModelSpec:
    name: str
    path: Path
    opset: int
    input_size: tuple[int, int]
    input_channels: int
    input_name: str
    input_dtype: str
    input_layout: str
    color_mode: str
    resize_mode: str
    pad_alignment: str
    resize_interpolation: str
    normalization_formula: str
    output_names: tuple[str, ...]
    output_shapes: tuple[tuple[Any, ...], ...]
    output_format: str
    output_coordinate_space: str
    mean: tuple[float, ...]
    std: tuple[float, ...]
    expected_sha256: str

    @classmethod
    def from_dict(cls, name: str, raw: Dict[str, Any], model_dir: Path) -> "OnnxModelSpec":
        if not isinstance(raw, dict):
            raise VisionModelError(f"manifest.{name} must be an object")
        prefix = f"manifest.{name}"
        file_name = _required_string(raw, "file", prefix)
        path = (model_dir / file_name).resolve(strict=False)
        try:
            path.relative_to(model_dir.resolve(strict=False))
        except ValueError as exc:
            raise VisionModelError(f"{prefix}.file must stay inside model_dir") from exc

        try:
            opset = int(raw.get("opset"))
        except Exception as exc:
            raise VisionModelError(f"{prefix}.opset is required and must be an integer") from exc
        if opset < 11:
            raise VisionModelError(f"{prefix}.opset must be >= 11")

        channels = int(raw.get("input_channels", 3 if name == "detector" else 1))
        if channels not in (1, 3):
            raise VisionModelError(f"{prefix}.input_channels must be 1 or 3")
        color_mode = str(raw.get("color_mode") or "").strip().lower()
        valid_color_modes = {1: {"gray"}, 3: {"gray_replicated", "bgr", "rgb"}}
        if color_mode not in valid_color_modes[channels]:
            raise VisionModelError(
                f"{prefix}.color_mode must be one of {sorted(valid_color_modes[channels])}"
            )
        resize_mode = str(raw.get("resize_mode") or "").strip().lower()
        expected_resize = "letterbox" if name == "detector" else "stretch"
        if resize_mode != expected_resize:
            raise VisionModelError(f"{prefix}.resize_mode must be {expected_resize!r}")
        pad_alignment = str(raw.get("pad_alignment") or "").strip().lower()
        if name == "detector" and pad_alignment not in {"center", "top_left"}:
            raise VisionModelError(f"{prefix}.pad_alignment must be 'center' or 'top_left'")
        if name == "segmenter" and pad_alignment != "none":
            raise VisionModelError(f"{prefix}.pad_alignment must be 'none'")
        resize_interpolation = str(raw.get("resize_interpolation") or "").strip().lower()
        if resize_interpolation not in {"area", "linear"}:
            raise VisionModelError(f"{prefix}.resize_interpolation must be 'area' or 'linear'")
        normalization_formula = str(raw.get("normalization_formula") or "").strip()
        if normalization_formula != "(pixel-mean)/std":
            raise VisionModelError(
                f"{prefix}.normalization_formula must be '(pixel-mean)/std'"
            )

        input_dtype = str(raw.get("input_dtype") or "float32").strip().lower()
        if input_dtype != "float32":
            raise VisionModelError(f"{prefix}.input_dtype must be 'float32'")
        input_layout = str(raw.get("input_layout") or "NCHW").strip().upper()
        if input_layout != "NCHW":
            raise VisionModelError(f"{prefix}.input_layout must be 'NCHW'")

        mean = _float_list(raw.get("mean", [0.0]), f"{prefix}.mean", channels)
        std = _float_list(raw.get("std", [255.0]), f"{prefix}.std", channels)
        if any(not isfinite(x) for x in (*mean, *std)):
            raise VisionModelError(f"{prefix}.mean/std must be finite")
        if any(x <= 0.0 for x in std):
            raise VisionModelError(f"{prefix}.std must be positive")

        output_names_raw = raw.get("output_names")
        if not isinstance(output_names_raw, Sequence) or isinstance(output_names_raw, (str, bytes)):
            raise VisionModelError(f"{prefix}.output_names must be a non-empty list")
        output_names = tuple(str(x).strip() for x in output_names_raw if str(x).strip())
        if not output_names:
            raise VisionModelError(f"{prefix}.output_names must be a non-empty list")
        output_shapes_raw = raw.get("output_shapes")
        if (
            not isinstance(output_shapes_raw, Sequence)
            or isinstance(output_shapes_raw, (str, bytes))
            or len(output_shapes_raw) != len(output_names)
        ):
            raise VisionModelError(
                f"{prefix}.output_shapes must contain one shape per output name"
            )
        output_shapes: list[tuple[Any, ...]] = []
        for shape in output_shapes_raw:
            if not isinstance(shape, Sequence) or isinstance(shape, (str, bytes)) or not shape:
                raise VisionModelError(f"{prefix}.output_shapes entries must be non-empty lists")
            output_shapes.append(tuple(shape))

        expected = _required_string(raw, "sha256", prefix).lower()
        if len(expected) != 64 or any(c not in "0123456789abcdef" for c in expected):
            raise VisionModelError(f"{prefix}.sha256 is invalid")

        return cls(
            name=name,
            path=path,
            opset=opset,
            input_size=_positive_int_pair(raw.get("input_size"), f"{prefix}.input_size"),
            input_channels=channels,
            input_name=_required_string(raw, "input_name", prefix),
            input_dtype=input_dtype,
            input_layout=input_layout,
            color_mode=color_mode,
            resize_mode=resize_mode,
            pad_alignment=pad_alignment,
            resize_interpolation=resize_interpolation,
            normalization_formula=normalization_formula,
            output_names=output_names,
            output_shapes=tuple(output_shapes),
            output_format=_required_string(raw, "output_format", prefix),
            output_coordinate_space=_required_string(raw, "output_coordinate_space", prefix),
            mean=mean,
            std=std,
            expected_sha256=expected,
        )

    @property
    def expected_input_shape(self) -> list[int]:
        width, height = self.input_size
        return [1, self.input_channels, height, width]

    def validate_file(self) -> None:
        if not self.path.is_file():
            raise VisionModelError(f"{self.name} model not found: {self.path}")
        actual = _sha256_file(self.path)
        if actual != self.expected_sha256:
            raise VisionModelError(
                f"{self.name} SHA-256 mismatch: expected {self.expected_sha256}, got {actual}"
            )


@dataclass(frozen=True)
class VisionModelManifest:
    path: Path
    manifest_sha256: str
    model_version: str
    dataset_version: str
    objective: str
    expected_image_size: tuple[int, int]
    class_names: tuple[str, ...]
    detector: OnnxModelSpec
    segmenter: OnnxModelSpec
    inference: Dict[str, Any]
    image_qc: Dict[str, Any]

    @classmethod
    def load(cls, model_dir: str | Path, manifest_name: str = MANIFEST_NAME) -> "VisionModelManifest":
        model_dir = Path(model_dir).resolve(strict=False)
        path = model_dir / manifest_name
        if not path.is_file():
            raise VisionModelError(f"model manifest not found: {path}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise VisionModelError(f"cannot parse model manifest: {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise VisionModelError("model manifest root must be an object")
        if int(raw.get("schema_version", -1)) != SUPPORTED_SCHEMA_VERSION:
            raise VisionModelError(
                f"unsupported model manifest schema_version={raw.get('schema_version')}; "
                f"expected {SUPPORTED_SCHEMA_VERSION}"
            )
        objective = str(raw.get("objective") or "").strip().lower()
        if objective != "4x":
            raise VisionModelError(f"this entrypoint only supports objective=4x, got {objective!r}")
        version = _required_string(raw, "model_version", "manifest")
        dataset_version = _required_string(raw, "dataset_version", "manifest")
        class_names_raw = raw.get("class_names")
        if class_names_raw != ["ipsc_clone"]:
            raise VisionModelError("manifest.class_names must be exactly ['ipsc_clone']")

        detector = OnnxModelSpec.from_dict("detector", raw.get("detector"), model_dir)
        segmenter = OnnxModelSpec.from_dict("segmenter", raw.get("segmenter"), model_dir)
        if detector.output_format != "nms_xyxy_score_class":
            raise VisionModelError("detector.output_format must be 'nms_xyxy_score_class'")
        if detector.output_coordinate_space != "letterboxed_input_pixels":
            raise VisionModelError(
                "detector.output_coordinate_space must be 'letterboxed_input_pixels'"
            )
        if len(detector.output_names) != 1:
            raise VisionModelError("detector must expose exactly one [N,6] output")
        if segmenter.output_format not in ("foreground_logits", "foreground_boundary_logits"):
            raise VisionModelError(
                "segmenter.output_format must be 'foreground_logits' or 'foreground_boundary_logits'"
            )
        if segmenter.output_coordinate_space != "resized_roi_pixels":
            raise VisionModelError(
                "segmenter.output_coordinate_space must be 'resized_roi_pixels'"
            )
        expected_segment_outputs = 2 if segmenter.output_format == "foreground_boundary_logits" else 1
        if len(segmenter.output_names) != expected_segment_outputs:
            raise VisionModelError(
                f"segmenter.output_names must contain {expected_segment_outputs} name(s)"
            )

        inference = raw.get("inference") or {}
        if not isinstance(inference, dict):
            raise VisionModelError("manifest.inference must be an object")
        missing_inference = sorted(REQUIRED_INFERENCE_KEYS - set(inference))
        if missing_inference:
            raise VisionModelError(
                f"manifest.inference is missing required release keys: {missing_inference}"
            )
        if not isinstance(inference.get("tile_enabled", True), bool):
            raise VisionModelError("manifest.inference.tile_enabled must be boolean")
        for key, default in (
            ("detector_accept_threshold", 0.70),
            ("detector_review_threshold", 0.35),
            ("segment_threshold", 0.50),
        ):
            value = float(inference.get(key, default))
            if not 0.0 <= value <= 1.0:
                raise VisionModelError(f"manifest.inference.{key} must be in [0,1]")
        if segmenter.output_format == "foreground_boundary_logits":
            boundary_threshold = float(inference.get("boundary_threshold", 0.50))
            if not 0.0 <= boundary_threshold <= 1.0:
                raise VisionModelError("manifest.inference.boundary_threshold must be in [0,1]")
        tile_overlap = float(inference.get("tile_overlap", 0.20))
        if not 0.0 <= tile_overlap < 1.0:
            raise VisionModelError("manifest.inference.tile_overlap must be in [0,1)")
        if int(inference.get("tile_size", 1280)) <= 0:
            raise VisionModelError("manifest.inference.tile_size must be positive")
        expected_w, expected_h = _positive_int_pair(
            raw.get("expected_image_size", [5120, 5120]), "manifest.expected_image_size"
        )
        tile_size = int(inference.get("tile_size", 1280))
        stride = max(1, int(round(tile_size * (1.0 - tile_overlap))))
        tile_count_upper = (
            max(1, (max(0, expected_w - tile_size) + stride - 1) // stride + 1)
            * max(1, (max(0, expected_h - tile_size) + stride - 1) // stride + 1)
        )
        try:
            max_tile_count = int(inference.get("max_tile_count", 100))
        except Exception as exc:
            raise VisionModelError("manifest.inference.max_tile_count must be an integer") from exc
        if max_tile_count <= 0 or tile_count_upper > max_tile_count:
            raise VisionModelError(
                f"manifest tiling would create about {tile_count_upper} tiles, above max_tile_count={max_tile_count}"
            )
        if float(inference.get("roi_pad_ratio", 0.20)) < 0.0:
            raise VisionModelError("manifest.inference.roi_pad_ratio must be non-negative")
        mask_duplicate_iom = float(inference.get("mask_duplicate_iom", 0.85))
        if not 0.0 < mask_duplicate_iom <= 1.0:
            raise VisionModelError("manifest.inference.mask_duplicate_iom must be in (0,1]")
        for key, default in (
            ("detector_merge_iou", 0.90),
            ("detector_merge_containment", 0.95),
            ("detector_merge_center_ratio", 0.10),
            ("segment_review_score", 0.50),
            ("min_mask_inside_detector_ratio", 0.50),
        ):
            value = float(inference.get(key, default))
            if not isfinite(value) or not 0.0 <= value <= 1.0:
                raise VisionModelError(f"manifest.inference.{key} must be finite and in [0,1]")
        if float(inference.get("detector_merge_iou", 0.90)) < 0.85:
            raise VisionModelError("detector_merge_iou must be >= 0.85 to protect adjacent clones")
        if float(inference.get("detector_merge_containment", 0.95)) < 0.90:
            raise VisionModelError("detector_merge_containment must be >= 0.90")
        if float(inference.get("detector_merge_center_ratio", 0.10)) > 0.15:
            raise VisionModelError("detector_merge_center_ratio must be <= 0.15")
        for key, default, minimum in (
            ("letterbox_value", 114, 0),
            ("mask_morphology_px", 3, 0),
            ("min_mask_area_px", 64, 1),
            ("min_connected_component_area_px", 4, 1),
            ("detector_gpu_memory_limit_mb", 1400, 256),
            ("segmenter_gpu_memory_limit_mb", 1600, 256),
        ):
            try:
                value = int(inference.get(key, default))
            except Exception as exc:
                raise VisionModelError(f"manifest.inference.{key} must be an integer") from exc
            if value < minimum:
                raise VisionModelError(f"manifest.inference.{key} must be >= {minimum}")
        if int(inference.get("letterbox_value", 114)) > 255:
            raise VisionModelError("manifest.inference.letterbox_value must be <= 255")
        if float(inference.get("detector_review_threshold", 0.35)) >= float(
            inference.get("detector_accept_threshold", 0.70)
        ):
            raise VisionModelError("review threshold must be lower than accept threshold")

        image_qc = raw.get("image_qc")
        if not isinstance(image_qc, dict) or not isinstance(image_qc.get("enabled"), bool):
            raise VisionModelError("manifest.image_qc.enabled must be explicit boolean")
        if image_qc["enabled"]:
            for key in (
                "min_dynamic_range",
                "max_dark_fraction",
                "max_bright_fraction",
                "min_laplacian_variance",
            ):
                try:
                    value = float(image_qc[key])
                except Exception as exc:
                    raise VisionModelError(f"manifest.image_qc.{key} is required and numeric") from exc
                if not isfinite(value) or value < 0:
                    raise VisionModelError(f"manifest.image_qc.{key} must be finite and non-negative")
            for key in ("max_dark_fraction", "max_bright_fraction"):
                if float(image_qc[key]) > 1.0:
                    raise VisionModelError(f"manifest.image_qc.{key} must be <= 1")
            for key, default in (("dark_value", 1), ("bright_value", 254)):
                try:
                    value = int(image_qc.get(key, default))
                except Exception as exc:
                    raise VisionModelError(f"manifest.image_qc.{key} must be an integer") from exc
                if not 0 <= value <= 255:
                    raise VisionModelError(f"manifest.image_qc.{key} must be in [0,255]")
        elif not str(image_qc.get("disabled_reason") or "").strip():
            raise VisionModelError("disabled image_qc requires image_qc.disabled_reason")

        manifest = cls(
            path=path,
            manifest_sha256=_sha256_file(path),
            model_version=version,
            dataset_version=dataset_version,
            objective=objective,
            expected_image_size=(expected_w, expected_h),
            class_names=("ipsc_clone",),
            detector=detector,
            segmenter=segmenter,
            inference=dict(inference),
            image_qc=dict(image_qc),
        )
        manifest.detector.validate_file()
        manifest.segmenter.validate_file()
        return manifest


@dataclass
class LoadedOnnxSession:
    spec: OnnxModelSpec
    session: Any
    provider: str
    fallback_reason: str | None = None

    @property
    def input_name(self) -> str:
        return self.spec.input_name

    @property
    def output_names(self) -> tuple[str, ...]:
        return self.spec.output_names

    def run(self, tensor: Any) -> list[Any]:
        try:
            return list(self.session.run(list(self.output_names), {self.input_name: tensor}))
        except Exception as exc:
            raise VisionModelError(f"{self.spec.name} inference failed: {exc}") from exc


def _providers_for_runtime(
    available: Iterable[str], *, prefer_cuda: bool, allow_cpu_fallback: bool
) -> list[str]:
    available = set(str(x) for x in available)
    if not prefer_cuda:
        if "CPUExecutionProvider" not in available:
            raise VisionModelError(
                f"required ONNX Runtime provider is unavailable: CPUExecutionProvider; "
                f"available={sorted(available)}"
            )
        return ["CPUExecutionProvider"]
    if "CUDAExecutionProvider" in available:
        return ["CUDAExecutionProvider"]
    if allow_cpu_fallback and "CPUExecutionProvider" in available:
        return ["CPUExecutionProvider"]
    raise VisionModelError(
        "required ONNX Runtime provider is unavailable: CUDAExecutionProvider; "
        f"available={sorted(available)}"
    )


def _cuda_provider_options(memory_limit_mb: int) -> Dict[str, Any]:
    return {
        "device_id": 0,
        "gpu_mem_limit": max(256, int(memory_limit_mb)) * 1024 * 1024,
        "arena_extend_strategy": "kSameAsRequested",
        "cudnn_conv_algo_search": "DEFAULT",
        "do_copy_in_default_stream": 1,
    }


def _metadata_shape(meta: Any) -> list[Any]:
    return list(getattr(meta, "shape", []) or [])


def _validate_session_contract(spec: OnnxModelSpec, session: Any) -> None:
    inputs = list(session.get_inputs())
    outputs = list(session.get_outputs())
    input_by_name = {str(x.name): x for x in inputs}
    output_by_name = {str(x.name): x for x in outputs}
    if set(input_by_name) != {spec.input_name}:
        raise VisionModelError(
            f"{spec.name} input names mismatch: manifest={[spec.input_name]}, "
            f"graph={sorted(input_by_name)}"
        )
    input_meta = input_by_name[spec.input_name]
    if str(getattr(input_meta, "type", "")) != "tensor(float)":
        raise VisionModelError(f"{spec.name} input dtype must be tensor(float)")
    if _metadata_shape(input_meta) != spec.expected_input_shape:
        raise VisionModelError(
            f"{spec.name} input shape mismatch: expected={spec.expected_input_shape}, "
            f"graph={_metadata_shape(input_meta)}"
        )
    missing = [name for name in spec.output_names if name not in output_by_name]
    if missing:
        raise VisionModelError(f"{spec.name} output names missing from graph: {missing}")
    for name in spec.output_names:
        if str(getattr(output_by_name[name], "type", "")) != "tensor(float)":
            raise VisionModelError(f"{spec.name} output {name!r} must be tensor(float)")

    for name, expected_shape in zip(spec.output_names, spec.output_shapes):
        actual_shape = _metadata_shape(output_by_name[name])
        if len(actual_shape) != len(expected_shape):
            raise VisionModelError(
                f"{spec.name} output {name!r} rank mismatch: expected={list(expected_shape)}, graph={actual_shape}"
            )
        for expected_dim, actual_dim in zip(expected_shape, actual_shape):
            if expected_dim in (None, -1, "N", "dynamic"):
                continue
            if actual_dim != expected_dim:
                raise VisionModelError(
                    f"{spec.name} output {name!r} shape mismatch: expected={list(expected_shape)}, graph={actual_shape}"
                )


def _new_session(ort_module: Any, model_path: Path, provider: str, memory_limit_mb: int) -> Any:
    providers: list[Any]
    if provider == "CUDAExecutionProvider":
        providers = [(provider, _cuda_provider_options(memory_limit_mb))]
    else:
        providers = [provider]
    session_options = ort_module.SessionOptions()
    if provider == "CUDAExecutionProvider":
        # Force unsupported nodes to fail graph load. The caller can then make
        # the explicit whole-bundle CPU fallback decision instead of allowing a
        # hidden per-node CPU fallback with unpredictable latency.
        session_options.add_session_config_entry("session.disable_cpu_ep_fallback", "1")
    return ort_module.InferenceSession(
        str(model_path), sess_options=session_options, providers=providers
    )


def load_onnx_session(
    spec: OnnxModelSpec,
    *,
    prefer_cuda: bool = True,
    allow_cpu_fallback: bool = False,
    gpu_memory_limit_mb: int = 1400,
    ort_module: Any = None,
) -> LoadedOnnxSession:
    """Load and validate one ONNX graph, using explicit CPU fallback only."""
    spec.validate_file()
    if ort_module is None:
        try:
            import onnxruntime as ort_module  # type: ignore
        except ImportError as exc:
            raise VisionModelError(
                "onnxruntime is not installed; install exactly one of the vision runtime requirement sets"
            ) from exc

    available_providers = list(ort_module.get_available_providers())
    selected = _providers_for_runtime(
        available_providers,
        prefer_cuda=prefer_cuda,
        allow_cpu_fallback=allow_cpu_fallback,
    )[0]
    fallback_reason: str | None = (
        "CUDAExecutionProvider unavailable"
        if prefer_cuda and selected == "CPUExecutionProvider"
        else None
    )
    if selected == "CUDAExecutionProvider" and hasattr(ort_module, "preload_dlls"):
        try:
            ort_module.preload_dlls(directory="")
        except Exception as exc:
            if not allow_cpu_fallback:
                raise VisionModelError(f"cannot preload CUDA/cuDNN runtime libraries: {exc}") from exc
            fallback_reason = f"CUDA library preload failed: {exc}"
            selected = "CPUExecutionProvider"

    try:
        session = _new_session(ort_module, spec.path, selected, gpu_memory_limit_mb)
    except Exception as exc:
        if selected != "CUDAExecutionProvider" or not allow_cpu_fallback:
            raise VisionModelError(f"cannot load {spec.name} model {spec.path}: {exc}") from exc
        fallback_reason = f"CUDA session creation failed: {exc}"
        try:
            session = _new_session(ort_module, spec.path, "CPUExecutionProvider", gpu_memory_limit_mb)
            selected = "CPUExecutionProvider"
        except Exception as cpu_exc:
            raise VisionModelError(
                f"cannot load {spec.name} on CUDA ({exc}) or CPU ({cpu_exc})"
            ) from cpu_exc

    active = [str(x) for x in session.get_providers()]
    if selected not in active:
        raise VisionModelError(
            f"{spec.name} requested provider {selected} is not active; active={active}"
        )
    _validate_session_contract(spec, session)
    return LoadedOnnxSession(
        spec=spec,
        session=session,
        provider=selected,
        fallback_reason=fallback_reason,
    )


@dataclass
class VisionModelBundle:
    manifest: VisionModelManifest
    detector: LoadedOnnxSession
    segmenter: LoadedOnnxSession

    @property
    def runtime_backend(self) -> str:
        return f"detector={self.detector.provider};segmenter={self.segmenter.provider}"

    @property
    def fallback_reasons(self) -> Dict[str, str]:
        return {
            key: value
            for key, value in {
                "detector": self.detector.fallback_reason,
                "segmenter": self.segmenter.fallback_reason,
            }.items()
            if value
        }


def load_model_bundle(
    model_dir: str | Path,
    *,
    prefer_cuda: bool = True,
    allow_cpu_fallback: bool = False,
    ort_module: Any = None,
) -> VisionModelBundle:
    manifest = VisionModelManifest.load(model_dir)
    detector = load_onnx_session(
        manifest.detector,
        prefer_cuda=prefer_cuda,
        allow_cpu_fallback=allow_cpu_fallback,
        gpu_memory_limit_mb=int(manifest.inference.get("detector_gpu_memory_limit_mb", 1400)),
        ort_module=ort_module,
    )
    segmenter = load_onnx_session(
        manifest.segmenter,
        prefer_cuda=prefer_cuda,
        allow_cpu_fallback=allow_cpu_fallback,
        gpu_memory_limit_mb=int(manifest.inference.get("segmenter_gpu_memory_limit_mb", 1600)),
        ort_module=ort_module,
    )
    if prefer_cuda and not allow_cpu_fallback and (
        detector.provider != "CUDAExecutionProvider"
        or segmenter.provider != "CUDAExecutionProvider"
    ):
        raise VisionModelError("strict CUDA mode requires both detector and segmenter on CUDA")
    if prefer_cuda and allow_cpu_fallback and detector.provider != segmenter.provider:
        # Keep runtime behaviour deterministic: if either graph cannot use CUDA,
        # rebuild both on CPU instead of mixing providers and memory behaviour.
        detector = load_onnx_session(
            manifest.detector,
            prefer_cuda=False,
            allow_cpu_fallback=False,
            ort_module=ort_module,
        )
        segmenter = load_onnx_session(
            manifest.segmenter,
            prefer_cuda=False,
            allow_cpu_fallback=False,
            ort_module=ort_module,
        )
        detector.fallback_reason = "bundle provider mismatch; both sessions rebuilt on CPU"
        segmenter.fallback_reason = "bundle provider mismatch; both sessions rebuilt on CPU"
    return VisionModelBundle(manifest=manifest, detector=detector, segmenter=segmenter)

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

import cv2
import numpy as np
import pytest

from vision.vision.image_loader import load_image, save_image
from vision.vision.instance_pipeline import detect_from_array
from vision.vision.instance_postprocess import (
    build_sliding_tiles,
    component_containing_or_nearest_center,
    merge_detections,
)
from vision.vision.model_runtime import (
    OnnxModelSpec,
    VisionModelBundle,
    VisionModelError,
    VisionModelManifest,
    _providers_for_runtime,
)
from workflow.clone_dedupe import (
    dedupe_well_clones,
    project_bbox_to_well,
    project_clone_to_stage,
)
from workflow.detect_api import DetectAPIError, normalize_detect_result
from vision.tools.validate_coco_instances import validate_coco
from vision.tools.evaluate_coco_instances import evaluate_coco_predictions


def test_direct_cli_bootstrap_exposes_repository_packages(tmp_path: Path) -> None:
    repository_root = Path(__file__).resolve().parents[1]
    cli_path = repository_root / "vision" / "run_detect.py"
    code = (
        "import runpy; "
        f"runpy.run_path({str(cli_path)!r}, run_name='cli_bootstrap'); "
        "import vision.vision.detect_pipeline; import workflow.file_io"
    )
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def _model_spec(name: str, path: Path, input_size: tuple[int, int]) -> OnnxModelSpec:
    channels = 1
    return OnnxModelSpec(
        name=name,
        path=path,
        opset=17,
        input_size=input_size,
        input_channels=channels,
        input_name="input",
        input_dtype="float32",
        input_layout="NCHW",
        color_mode="gray",
        resize_mode="letterbox" if name == "detector" else "stretch",
        pad_alignment="center" if name == "detector" else "none",
        resize_interpolation="area",
        normalization_formula="(pixel-mean)/std",
        output_names=("output",),
        output_shapes=((1, "N", 6),) if name == "detector" else ((1, 1, 32, 32),),
        output_format="nms_xyxy_score_class" if name == "detector" else "foreground_logits",
        output_coordinate_space="letterboxed_input_pixels" if name == "detector" else "resized_roi_pixels",
        mean=(0.0,),
        std=(255.0,),
        expected_sha256="0" * 64,
    )


class _DetectorSession:
    provider = "CPUExecutionProvider"
    fallback_reason = None

    def __init__(self, spec: OnnxModelSpec) -> None:
        self.spec = spec

    def run(self, _tensor: np.ndarray) -> list[np.ndarray]:
        return [
            np.asarray(
                [
                    [8.0, 8.0, 30.0, 30.0, 0.92, 0.0],
                    [36.0, 36.0, 58.0, 58.0, 0.55, 0.0],
                ],
                dtype=np.float32,
            )
        ]


class _SegmenterSession:
    provider = "CPUExecutionProvider"
    fallback_reason = None

    def __init__(self, spec: OnnxModelSpec) -> None:
        self.spec = spec

    def run(self, _tensor: np.ndarray) -> list[np.ndarray]:
        logits = np.full((1, 1, 32, 32), -8.0, dtype=np.float32)
        cv2.circle(logits[0, 0], (16, 16), 11, 8.0, -1)
        return [logits]


def _fake_bundle(tmp_path: Path) -> VisionModelBundle:
    detector_spec = _model_spec("detector", tmp_path / "detector.onnx", (64, 64))
    segmenter_spec = _model_spec("segmenter", tmp_path / "segmenter.onnx", (32, 32))
    manifest = VisionModelManifest(
        path=tmp_path / "model_manifest.json",
        manifest_sha256="0" * 64,
        model_version="unit-test",
        dataset_version="unit-test-data",
        objective="4x",
        expected_image_size=(64, 64),
        class_names=("ipsc_clone",),
        detector=detector_spec,
        segmenter=segmenter_spec,
        inference={
            "tile_enabled": False,
            "tile_size": 64,
            "tile_overlap": 0.20,
            "max_tile_count": 100,
            "letterbox_value": 114,
            "detector_review_threshold": 0.35,
            "detector_accept_threshold": 0.70,
            "detector_merge_iou": 0.90,
            "detector_merge_containment": 0.95,
            "detector_merge_center_ratio": 0.10,
            "segment_threshold": 0.50,
            "segment_review_score": 0.50,
            "min_mask_inside_detector_ratio": 0.50,
            "roi_pad_ratio": 0.20,
            "min_mask_area_px": 10,
            "min_connected_component_area_px": 1,
            "mask_morphology_px": 1,
            "mask_duplicate_iom": 0.85,
            "detector_gpu_memory_limit_mb": 512,
            "segmenter_gpu_memory_limit_mb": 512,
        },
        image_qc={"enabled": False, "disabled_reason": "unit test fixture"},
    )
    return VisionModelBundle(
        manifest=manifest,
        detector=_DetectorSession(detector_spec),
        segmenter=_SegmenterSession(segmenter_spec),
    )


def test_unicode_image_path_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "中文数据" / "克隆图像.bmp"
    expected = np.arange(12 * 16, dtype=np.uint8).reshape(12, 16)
    save_image(path, expected)
    actual = load_image(path, cv2.IMREAD_GRAYSCALE)
    assert np.array_equal(actual, expected)


def test_sliding_tile_ownership_covers_every_pixel_center_once() -> None:
    tiles = build_sliding_tiles(31, 27, tile_size=12, overlap=0.25)
    for y in range(27):
        for x in range(31):
            assert sum(tile.owns_global_center(x + 0.5, y + 0.5) for tile in tiles) == 1


def test_presegment_merge_never_suppresses_same_view_instances_or_size_mismatch() -> None:
    detections = [
        {"bbox": [0, 0, 100, 100], "score": 0.95, "source": "tile_001"},
        {"bbox": [2, 2, 98, 98], "score": 0.90, "source": "tile_001"},
        {"bbox": [20, 20, 40, 40], "score": 0.92, "source": "full_image"},
    ]
    assert len(
        merge_detections(
            detections,
            iou_threshold=0.90,
            containment_threshold=0.95,
            center_ratio=0.10,
        )
    ) == 3


def test_component_selection_ignores_tiny_center_noise_in_favor_of_colony_support() -> None:
    mask = np.zeros((64, 64), dtype=np.uint8)
    mask[30, 30] = 255
    cv2.circle(mask, (45, 32), 7, 255, -1)
    selected = component_containing_or_nearest_center(
        mask,
        [30, 30],
        target_bbox_xywh=[20, 20, 36, 30],
        min_component_area_px=1,
    )
    assert selected[30, 30] == 0
    assert selected[32, 45] == 255


def test_model_pipeline_separates_formal_and_review_and_writes_uint16_mask(tmp_path: Path) -> None:
    out_dir = tmp_path / "输出"
    result = detect_from_array(
        np.full((64, 64), 127, dtype=np.uint8),
        model_dir=tmp_path,
        out_dir=out_dir,
        provider="cpu",
        objective_name="4x",
        model_bundle=_fake_bundle(tmp_path),
    )

    assert result["schema_version"] == 2
    assert result["component_count"] == len(result["components"]) == 1
    assert result["review_candidate_count"] == len(result["review_candidates"]) == 1
    assert result["components"][0]["id"] == "C001"
    assert result["review_candidates"][0]["id"] == "R001"
    assert result["review_candidates"][0]["review_reasons"] == [
        "detector_score_below_accept_threshold"
    ]
    assert result["quality_assessment"]["status"] == "not_assessed"
    assert result["components"][0]["is_pickable"] is False
    labels = cv2.imdecode(
        np.fromfile(str(out_dir / "05_instance_mask.png"), dtype=np.uint8),
        cv2.IMREAD_UNCHANGED,
    )
    assert labels.dtype == np.uint16
    assert set(np.unique(labels)) == {0, 1}
    persisted = json.loads((out_dir / "07_result.json").read_text(encoding="utf-8"))
    assert persisted["component_count"] == 1


def test_schema_v2_rejects_count_mismatch() -> None:
    with pytest.raises(DetectAPIError, match="component_count"):
        normalize_detect_result(
            {
                "schema_version": 2,
                "component_count": 2,
                "review_candidate_count": 0,
                "components": [{"id": "C001", "center_pixel": [1, 2], "bbox": [0, 0, 2, 2]}],
                "review_candidates": [],
            }
        )


def test_schema_v2_preserves_contour_and_review_metadata() -> None:
    result = normalize_detect_result(
        {
            "schema_version": 2,
            "objective_name": "4x",
            "component_count": 1,
            "review_candidate_count": 1,
            "components": [
                {
                    "id": "C001",
                    "center_pixel": [4, 5],
                    "bbox": [1, 2, 6, 7],
                    "contour_points": [[1, 2], [2, 3], [3, 2]],
                    "detection_confidence": 0.9,
                    "segmentation_status": "success",
                    "eligible_for_10x_centering": True,
                    "instance_label": 1,
                    "is_pickable": False,
                }
            ],
            "review_candidates": [
                {
                    "id": "R001",
                    "center_pixel": [8, 9],
                    "bbox": [7, 8, 2, 2],
                    "review_reasons": ["low_score"],
                    "is_pickable": False,
                }
            ],
            "models": {"model_version": "v1"},
            "runtime": {"backend": "cpu"},
        }
    )
    assert result["clone_count"] == 1
    assert result["review_candidate_count"] == 1
    assert result["clones"][0]["contour_points"] == [[1, 2], [2, 3], [3, 2]]
    assert result["review_candidates"][0]["review_reasons"] == ["low_score"]
    assert result["models"]["model_version"] == "v1"


def test_cpu_provider_is_selected_without_enabling_fallback() -> None:
    assert _providers_for_runtime(
        ["CPUExecutionProvider"], prefer_cuda=False, allow_cpu_fallback=False
    ) == ["CPUExecutionProvider"]


def test_manifest_requires_model_sha256(tmp_path: Path) -> None:
    (tmp_path / "detector.onnx").write_bytes(b"detector")
    (tmp_path / "segmenter.onnx").write_bytes(b"segmenter")
    raw = json.loads(
        (Path(__file__).parents[1] / "vision" / "models" / "ipsc_4x" / "model_manifest.example.json").read_text(
            encoding="utf-8"
        )
    )
    raw["detector"].pop("sha256")
    (tmp_path / "model_manifest.json").write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(VisionModelError, match="sha256"):
        VisionModelManifest.load(tmp_path)


def test_explicit_legacy_entrypoint_ignores_model_runtime_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    from vision.vision import detect_pipeline

    received: dict = {}

    def fake_detect_from_path(image_path: str, **kwargs: object) -> dict:
        received.update(kwargs)
        return {"input_path": image_path}

    monkeypatch.setattr(detect_pipeline, "detect_from_path", fake_detect_from_path)
    detect_pipeline.process_image(
        "legacy.bmp",
        model_dir="models",
        provider="cuda",
        allow_cpu_fallback=False,
        objective_name="4x",
    )
    assert received == {"out_dir": None}


def _clone(center_x: int, clone_id: str) -> dict:
    return {
        "clone_id": clone_id,
        "source_detection_id": clone_id,
        "center_px": [center_x, 50],
        "offset_from_image_center_px": [center_x - 50, 0],
        "bbox": [center_x - 20, 30, 40, 40],
        "confidence": 0.9,
        "detection_confidence": 0.9,
        "segmentation_status": "success",
        "truncated": False,
    }


def _image(index: int, stage_x: int, clone: dict) -> dict:
    return {
        "index": index,
        "stage_x_target": stage_x,
        "stage_y_target": 0,
        "stage_x_actual": stage_x,
        "stage_y_actual": 0,
        "image_width_px": 100,
        "image_height_px": 100,
        "image_center_px": [50, 50],
        "mm_per_pixel": {"x": 0.01, "y": 0.01},
        "clones": [clone],
    }


def test_cross_view_dedupe_merges_same_physical_clone() -> None:
    images = [_image(1, 0, _clone(60, "I1:C001")), _image(2, 20, _clone(80, "I2:C001"))]
    result = dedupe_well_clones(
        images,
        plate_cfg={
            "pulses_per_mm": 100,
            "x_stage_sign_for_view_right": 1,
            "y_stage_sign_for_view_down": 1,
        },
        reference={"well_start": {"x": 0, "y": 0}},
        scan_config={"overlap": 0.2, "dedupe_registration_calibrated": True},
    )
    assert len(result["unique_clones"]) == 1
    assert result["unique_clones"][0]["observation_count"] == 2
    assert images[0]["clones"][0]["global_clone_id"] == "G001"
    assert images[1]["clones"][0]["global_clone_id"] == "G001"


def test_dedupe_projects_bbox_center_independently_from_safe_point() -> None:
    image = _image(1, 0, _clone(60, "I1:C001"))
    clone = image["clones"][0]
    clone["center_px"] = [75, 50]
    clone["offset_from_image_center_px"] = [25, 0]
    clone["bbox"] = [40, 30, 40, 40]
    geometry = project_bbox_to_well(
        image,
        clone,
        {
            "pulses_per_mm": 100,
            "x_stage_sign_for_view_right": 1,
            "y_stage_sign_for_view_down": 1,
        },
        {"x": 0, "y": 0},
    )
    assert geometry["center_well_mm"][0] == pytest.approx(-0.25)
    assert geometry["bbox_center_well_mm"][0] == pytest.approx(-0.10)
    assert geometry["bbox_well_mm"][0] == pytest.approx(-0.30)


@pytest.mark.parametrize("x_sign", [-1, 1])
@pytest.mark.parametrize("y_sign", [-1, 1])
def test_stage_projection_is_consistent_for_all_axis_signs(x_sign: int, y_sign: int) -> None:
    x_ppm = 100
    y_ppm = 200
    image = {
        "stage_x_target": round(x_sign * 0.5 * x_ppm),
        "stage_y_target": round(y_sign * -0.3 * y_ppm),
        "stage_x_actual": None,
        "stage_y_actual": None,
        "mm_per_pixel": {"x": 0.01, "y": 0.02},
    }
    clone = {"offset_from_image_center_px": [30, -10]}
    projected = project_clone_to_stage(
        image,
        clone,
        {
            "pulses_per_mm": {"x": x_ppm, "y": y_ppm},
            "x_stage_sign_for_view_right": x_sign,
            "y_stage_sign_for_view_down": y_sign,
        },
    )
    assert projected["x_pulse"] / (x_sign * x_ppm) == pytest.approx(0.2)
    assert projected["y_pulse"] / (y_sign * y_ppm) == pytest.approx(-0.1)
    assert projected["coordinate_source"] == {"x": "target", "y": "target"}


def test_cross_view_dedupe_does_not_merge_nearby_nonoverlapping_masks() -> None:
    first = _clone(60, "I1:C001")
    second = _clone(80, "I2:C001")
    second["offset_from_image_center_px"] = [-30, 0]
    second["center_px"] = [20, 50]
    second["bbox"] = [0, 30, 40, 40]
    images = [_image(1, 0, first), _image(2, 20, second)]
    result = dedupe_well_clones(
        images,
        plate_cfg={
            "pulses_per_mm": 100,
            "x_stage_sign_for_view_right": 1,
            "y_stage_sign_for_view_down": 1,
        },
        reference={"well_start": {"x": 0, "y": 0}},
        scan_config={"overlap": 0.2, "dedupe_registration_calibrated": True},
    )
    assert len(result["unique_clones"]) == 2


def test_dedupe_never_bridges_two_instances_from_the_same_image() -> None:
    a = _clone(50, "I1:A")
    c = _clone(80, "I1:C")
    image_one = _image(1, 0, a)
    image_one["clones"].append(c)
    b = _clone(65, "I2:B")
    image_two = _image(2, 15, b)
    result = dedupe_well_clones(
        [image_one, image_two],
        plate_cfg={
            "pulses_per_mm": 100,
            "x_stage_sign_for_view_right": 1,
            "y_stage_sign_for_view_down": 1,
        },
        reference={"well_start": {"x": 0, "y": 0}},
        scan_config={"overlap": 0.2, "dedupe_registration_calibrated": True},
        registration_tolerance_mm=0.30,
    )
    assert len(result["unique_clones"]) == 2
    assert all(len({source.split(":")[0] for source in item["source_detections"]}) == item["observation_count"] for item in result["unique_clones"])


def test_dedupe_absolute_tolerance_prevents_large_neighbor_merge() -> None:
    first = _clone(50, "I1:A")
    second = _clone(50, "I2:B")
    first["bbox"] = [0, 0, 100, 100]
    second["bbox"] = [0, 0, 100, 100]
    images = [_image(1, 0, first), _image(2, 40, second)]
    result = dedupe_well_clones(
        images,
        plate_cfg={
            "pulses_per_mm": 100,
            "x_stage_sign_for_view_right": 1,
            "y_stage_sign_for_view_down": 1,
        },
        reference={"well_start": {"x": 0, "y": 0}},
        scan_config={"overlap": 0.2, "dedupe_registration_calibrated": True},
        registration_tolerance_mm=0.10,
    )
    assert len(result["unique_clones"]) == 2


def test_detect_executor_emits_unique_and_review_counts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from workflow import detect_executor

    image_path = tmp_path / "scan.bmp"
    save_image(image_path, np.zeros((100, 100), dtype=np.uint8))
    captured_kwargs: dict = {}

    def fake_detect(_path: str, **kwargs: object) -> dict:
        captured_kwargs.update(kwargs["detect_kwargs"])
        return {
            "schema_version": 2,
            "clone_count": 1,
            "review_candidate_count": 1,
            "clones": [
                {
                    "clone_id": "C001",
                    "center_px": [50, 50],
                    "bbox": [30, 30, 40, 40],
                    "area_px": 1000,
                    "score": 0.9,
                    "confidence": 0.9,
                    "detection_confidence": 0.9,
                    "segmentation_status": "success",
                    "is_pickable": None,
                    "raw": {"contour_points": [[30, 30], [70, 30], [70, 70], [30, 70]]},
                }
            ],
            "review_candidates": [
                {
                    "clone_id": "R001",
                    "center_px": [10, 10],
                    "bbox": [5, 5, 10, 10],
                    "review_reasons": ["low_score"],
                    "raw": {},
                }
            ],
            "models": {"model_version": "v1"},
            "runtime": {"backend": "CPUExecutionProvider"},
        }

    monkeypatch.setattr(detect_executor, "run_detect_on_image", fake_detect)
    result = detect_executor.execute_detect_on_scan_result(
        {
            "task": {
                "detect": {
                    "save_overlay": False,
                    "model_dir": "C:/models/release",
                    "provider": "cpu",
                    "deduplication": {"calibrated": True, "registration_tolerance_mm": 0.10},
                }
            },
            "plate": {
                "pulses_per_mm": 100,
                "x_stage_sign_for_view_right": 1,
                "y_stage_sign_for_view_down": 1,
            },
        },
        {
            "task_id": "v2-integration",
            "plate_type": "test",
            "well_name": "A1",
            "objective_name": "4x",
        },
        {
            "reference": {"well_start": {"x": 1000, "y": 2000}},
            "scan_config": {"fov_mm": {"width": 1.0, "height": 1.0}, "overlap": 0.2},
            "captures": [
                {
                    "index": 1,
                    "row_index": 0,
                    "col_index": 0,
                    "stage_x_target": 1000,
                    "stage_y_target": 2000,
                    "motion_result": {
                        "after": {"x": {"current_pos": 1000}, "y": {"current_pos": 2000}}
                    },
                    "capture_result": {"saved_path": str(image_path)},
                }
            ],
        },
    )
    assert result["total_image_clone_count"] == 1
    assert result["unique_clone_count"] == result["total_clone_count"] == 1
    assert result["review_candidate_count"] == 1
    assert result["images"][0]["clones"][0]["is_pickable"] is False
    assert result["images"][0]["review_candidates"][0]["is_pickable"] is False
    assert result["unique_clones"][0]["quality_assessment"]["status"] == "not_assessed"
    assert captured_kwargs["model_dir"] == "C:/models/release"


def test_compensation_selector_separates_10x_centering_from_pick_permission() -> None:
    from workflow.compensate_executor import select_clone_for_compensation

    detect_result = {
        "images": [
            {
                "index": 1,
                "clones": [
                    {
                        "clone_id": "C001",
                        "eligible_for_10x_centering": True,
                        "is_pickable": False,
                    }
                ],
            }
        ]
    }
    image, clone = select_clone_for_compensation(
        detect_result, {"purpose": "10x_centering", "mode": "first"}
    )
    assert image["index"] == 1 and clone["clone_id"] == "C001"
    with pytest.raises(ValueError, match="is_pickable=true"):
        select_clone_for_compensation(detect_result, {"purpose": "pick", "mode": "first"})


def test_detection_preflight_requires_model_dir_before_capture() -> None:
    from workflow.run_task import preflight_detection_backend

    with pytest.raises(ValueError, match="detect.model_dir"):
        preflight_detection_backend(
            {"task": {"detect": {}}, "camera": {"resolution": {"width": 5120, "height": 5120, "allow_downscale": False}}},
            {"stages": ["capture", "detect"], "objective_name": "4x"},
        )


def test_detection_preflight_allows_explicit_legacy_without_model_dir() -> None:
    from workflow.run_task import preflight_detection_backend

    preflight_detection_backend(
        {"task": {"detect": {"entrypoint": "vision.vision.detect_pipeline:process_image"}}},
        {"stages": ["capture", "detect"], "objective_name": "4x"},
    )


def test_detection_preflight_rejects_uncalibrated_overlap_before_model_load() -> None:
    from workflow.run_task import preflight_detection_backend

    with pytest.raises(ValueError, match="deduplication.calibrated"):
        preflight_detection_backend(
            {"task": {"detect": {"model_dir": "C:/not-loaded-because-calibration-fails"}}, "camera": {"resolution": {"width": 5120, "height": 5120, "allow_downscale": False}}},
            {
                "stages": ["capture", "detect"],
                "objective_name": "4x",
                "overlap": 0.2,
            },
        )


def test_detection_preflight_rejects_uncalibrated_default_overlap() -> None:
    from workflow.run_task import build_pipeline_params, preflight_detection_backend

    ctx = {
        "task": {
            "task_id": "default-overlap-detect",
            "task_type": "pipeline",
            "stages": ["capture", "detect"],
            "plate_type": "24-well",
            "objective_name": "4x",
            "detect": {"model_dir": "C:/not-loaded-because-calibration-fails"},
        },
        "objective": {"fov_mm": {"width": 3.22, "height": 3.22}},
        "camera": {"resolution": {"width": 5120, "height": 5120, "allow_downscale": False}},
    }
    params = build_pipeline_params(ctx)
    assert params["overlap"] == 0.1
    with pytest.raises(ValueError, match="deduplication.calibrated"):
        preflight_detection_backend(ctx, params)


def test_coco_validator_rejects_group_split_leakage_and_unreviewed_empty(tmp_path: Path) -> None:
    save_image(tmp_path / "a.bmp", np.zeros((16, 16), dtype=np.uint8))
    save_image(tmp_path / "b.bmp", np.zeros((16, 16), dtype=np.uint8))
    payload = {
        "categories": [{"id": 1, "name": "ipsc_clone"}],
        "images": [
            {
                "id": 1,
                "file_name": "a.bmp",
                "width": 16,
                "height": 16,
                "objective": "4x",
                "group_id": "same-batch-well",
                "split": "train",
            },
            {
                "id": 2,
                "file_name": "b.bmp",
                "width": 16,
                "height": 16,
                "objective": "4x",
                "group_id": "same-batch-well",
                "split": "test",
            },
        ],
        "annotations": [
            {
                "id": 1,
                "image_id": 1,
                "category_id": 1,
                "bbox": [2, 2, 8, 8],
                "segmentation": [[2, 2, 10, 2, 10, 10, 2, 10]],
                "truncated": False,
            }
        ],
    }
    annotation_path = tmp_path / "annotations.json"
    annotation_path.write_text(json.dumps(payload), encoding="utf-8")
    report = validate_coco(annotation_path, tmp_path)
    assert report["valid"] is False
    assert any("leaks across splits" in error for error in report["errors"])
    assert any("complete_negative" in error for error in report["errors"])


def test_coco_evaluator_reports_one_to_one_instance_metrics(tmp_path: Path) -> None:
    annotation_path = tmp_path / "annotations.json"
    prediction_path = tmp_path / "predictions.json"
    annotation_path.write_text(
        json.dumps(
            {
                "images": [
                    {"id": 1, "file_name": "test/a.bmp", "width": 32, "height": 32, "split": "test"}
                ],
                "annotations": [
                    {
                        "id": 1,
                        "image_id": 1,
                        "segmentation": [[5, 5, 15, 5, 15, 15, 5, 15]],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    prediction_path.write_text(
        json.dumps(
            {
                "images": [
                    {
                        "image_path": "C:/dataset/test/a.bmp",
                        "clones": [
                            {"id": "C001", "contour_points": [[5, 5], [15, 5], [15, 15], [5, 15]]}
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = evaluate_coco_predictions(annotation_path, prediction_path)
    assert report["true_positive"] == 1
    assert report["false_positive"] == report["false_negative"] == 0
    assert report["precision"] == report["recall"] == 1.0
    assert report["mean_matched_dice"] == pytest.approx(1.0)

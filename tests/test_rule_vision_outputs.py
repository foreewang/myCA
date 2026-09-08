"""Exercise real rule inference/output pixels and ownership of image buffers.

The output modes are compared using identical OpenCV RNG seeds. Segmentation is
only stubbed in the explicit overlap/failure/allocation tests; image drawing,
encoding, Unicode paths, result building, and scoring remain real throughout.
"""
from __future__ import annotations

import gc
import json
import weakref
from pathlib import Path

import cv2
import numpy as np
import pytest

from vision.vision import detect_pipeline as pipeline
from vision.vision import image_loader, postprocess


PRODUCTION_FILES = {"05_contour_mask.bmp", "06_overlay.bmp", "07_result.json"}
DEBUG_FILES = {
    "01_gray.bmp", "02_coarse_flat.bmp", "03_coarse_binary.bmp", "04_refine_density.bmp"
}
SCALE_BAR = {"mm_per_pixel": 0.002, "length_mm": 0.1, "margin_px": 32}


def _sample(positive: bool = True) -> np.ndarray:
    image = np.full((512, 512), 210, np.uint8)
    if positive:
        cv2.circle(image, (170, 256), 85, 55, -1)
        cv2.circle(image, (341, 341), 55, 65, -1)
    return image


def _run(image: np.ndarray, output: Path | None, **kwargs) -> dict:
    cv2.setRNGSeed(1729)
    return pipeline.detect_from_gray(image, src_path="same-input.png", out_dir=output, **kwargs)


def _assert_output_modes(image: np.ndarray, tmp_path: Path, **kwargs) -> tuple[dict, dict]:
    full = tmp_path / "完整调试"
    minimal = tmp_path / "生产结果"
    original = image.copy()
    debug_result = _run(image, full, save_debug=True, **kwargs)
    production_result = _run(image, minimal, **kwargs)
    memory_result = _run(image, None, save_debug=True, **kwargs)
    np.testing.assert_array_equal(image, original)
    assert debug_result == production_result
    # No-output mode has always reported no drawn scale bar, even if configured.
    assert memory_result["scale_bar"] is None
    assert memory_result == {**production_result, "scale_bar": None}
    assert {p.name for p in minimal.iterdir()} == PRODUCTION_FILES
    assert {p.name for p in full.iterdir()} == PRODUCTION_FILES | DEBUG_FILES
    for name in ("05_contour_mask.bmp", "06_overlay.bmp"):
        np.testing.assert_array_equal(
            image_loader.load_image(minimal / name), image_loader.load_image(full / name)
        )
    for directory in (minimal, full):
        with (directory / "07_result.json").open(encoding="utf-8") as stream:
            assert json.load(stream) == production_result
    minimum_bytes = sum(p.stat().st_size for p in minimal.iterdir())
    debug_bytes = sum(p.stat().st_size for p in full.iterdir())
    assert 0.49 < minimum_bytes / debug_bytes < 0.52
    return production_result, memory_result


@pytest.mark.parametrize("positive", [False, True], ids=["empty", "two-colonies"])
@pytest.mark.parametrize("scale_bar", [None, SCALE_BAR], ids=["no-scale", "with-scale"])
def test_real_algorithm_keeps_results_and_final_pixels_across_output_modes(
    tmp_path: Path, positive: bool, scale_bar: dict | None
) -> None:
    result, _ = _assert_output_modes(_sample(positive), tmp_path, scale_bar=scale_bar)
    assert result["component_count"] == (2 if positive else 0)
    assert bool(result["scale_bar"]) is bool(scale_bar)
    if positive:
        assert all(item["area_px"] > 5000 for item in result["components"])
        assert all(len(item["contour_points"]) > 10 for item in result["components"])
        assert all(item["refine_method"] == "none" for item in result["components"])
        assert all(item["edge_refine_success"] is False for item in result["components"])
        assert all(item["edge_refine_reason"] == "disabled" for item in result["components"])
        assert all(item["is_valid_for_compensation"] for item in result["components"])


def test_explicit_hybrid_edge_refine_still_runs_grabcut(tmp_path: Path) -> None:
    result, _ = _assert_output_modes(_sample(True), tmp_path, edge_refine_method="hybrid")
    assert result["component_count"] == 2
    assert all(item["refine_method"] == "grabcut" for item in result["components"])
    assert all(item["edge_refine_success"] for item in result["components"])


@pytest.mark.parametrize("input_kind", ["gray8", "gray16", "bgr", "bgra"])
def test_array_and_path_entrypoints_preserve_inputs_and_agree(
    tmp_path: Path, input_kind: str
) -> None:
    gray = _sample()
    if input_kind == "gray16":
        source = gray.astype(np.uint16) * 257
    elif input_kind in {"bgr", "bgra"}:
        source = np.stack((gray, np.roll(gray, 3, axis=1), np.roll(gray, 2, axis=0)), axis=2)
        if input_kind == "bgra":
            source = np.dstack((source, np.tile(np.arange(512, dtype=np.uint16) % 256, (512, 1)).astype(np.uint8)))
    else:
        source = gray
    before = source.copy()
    image_path = tmp_path / "细胞图像.png"
    image_loader.save_image(image_path, source)
    cv2.setRNGSeed(1729)
    array_result = pipeline.detect_from_gray(source, src_path=image_path)
    cv2.setRNGSeed(1729)
    path_result = pipeline.detect_from_path(image_path, out_dir=None)
    assert array_result == path_result
    assert path_result["component_count"] > 0
    assert any(c["contour_points"] for c in path_result["components"])
    np.testing.assert_array_equal(source, before)


@pytest.mark.parametrize("input_kind", ["float32-gray", "float32-bgra", "uint16-bgr", "strided-uint8"])
def test_public_array_normalization_keeps_json_final_pixels_and_caller_input(
    tmp_path: Path, input_kind: str,
) -> None:
    gray = _sample()
    if input_kind == "float32-gray":
        source = gray.astype(np.float32) * 1.25 - 12.5
    elif input_kind == "float32-bgra":
        source = np.stack((gray, np.roll(gray, 3, axis=1), np.roll(gray, 2, axis=0),
                           np.full_like(gray, 127)), axis=2).astype(np.float32)
    elif input_kind == "uint16-bgr":
        source = np.stack((gray, np.roll(gray, 3, axis=1), np.roll(gray, 2, axis=0)),
                          axis=2).astype(np.uint16) * 257
    else:
        source = gray[:, ::-1]
        assert not source.flags.c_contiguous
    before = source.copy()
    normalized = image_loader.to_gray_u8(source)
    normalized_before = normalized.copy()
    direct_output, normalized_output = tmp_path / "直接输入", tmp_path / "显式标准化"
    direct_result = _run(source, direct_output, scale_bar=SCALE_BAR)
    normalized_result = _run(normalized, normalized_output, scale_bar=SCALE_BAR)
    assert direct_result == normalized_result
    assert direct_result["component_count"] > 0
    assert any(component["contour_points"] for component in direct_result["components"])
    for name in ("05_contour_mask.bmp", "06_overlay.bmp"):
        np.testing.assert_array_equal(image_loader.load_image(direct_output / name),
                                      image_loader.load_image(normalized_output / name))
    for output in (direct_output, normalized_output):
        assert json.loads((output / "07_result.json").read_text(encoding="utf-8")) == direct_result
    np.testing.assert_array_equal(source, before)
    np.testing.assert_array_equal(normalized, normalized_before)


def _stub_segmentation(monkeypatch: pytest.MonkeyPatch) -> np.ndarray:
    """Return an independent expected union for overlapping ROIs and a failure."""
    shape = (384, 448)
    specs = [(40, 50, 120, 100), (100, 80, 120, 100), (300, 270, 45, 40)]
    expected = np.zeros(shape, np.uint8)
    expected[60:140, 50:150] = 255
    expected[90:170, 110:210] = 255
    cursor = [0]

    def coarse(gray, **kwargs):
        cursor[0] = 0
        items = []
        for x, y, width, height in specs:
            items.append({
                "coarse_bbox": [x, y, width, height],
                "coarse_center_pixel": [x + width // 2, y + height // 2],
                "confidence": 0.9,
                "is_valid_for_compensation": True,
            })
        return items, {
            "flat": np.full((96, 112), 64, np.uint8),
            "binary_small": np.full((96, 112), 255, np.uint8),
            "scale": 4.0, "seed_thresh": 85,
            "density_thresh": 101.0, "coarse_candidate_count": 3,
        }

    def refine(roi, center_local, **kwargs):
        index = cursor[0]
        cursor[0] += 1
        density = np.full((20, 24), 50 + index * 60, np.uint8)
        if index == 2:
            return None, {"density": density, "center_history_small": [[8, 9]]}
        mask = np.zeros(roi.shape, np.uint8)
        mask[10:-10, 10:-10] = 255
        contour = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)[0][0]
        return {
            "contour_local": contour[:, 0, :].tolist(),
            "center_local": center_local,
            "bbox_local": [10, 10, roi.shape[1] - 20, roi.shape[0] - 20],
            "area_px": int(cv2.countNonZero(mask)),
            "mask_full": mask,
            "edge_refine_success": True,
        }, {"density": density}

    monkeypatch.setattr(pipeline, "detect_coarse_rois", coarse)
    monkeypatch.setattr(pipeline, "refine_contour_in_roi", refine)
    return expected


def test_overlap_union_and_failed_target_preserve_real_rendering(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected_mask = _stub_segmentation(monkeypatch)
    image = np.full(expected_mask.shape, 160, np.uint8)
    result, _ = _assert_output_modes(
        image, tmp_path, refine_pad_ratio=0, detect_well_border=False, scale_bar=SCALE_BAR
    )
    assert result["component_ids"] == ["C01", "C02", "C03"]
    assert result["components"][2]["contour_points"] == []
    assert result["components"][2]["is_valid_for_compensation"] is False
    np.testing.assert_array_equal(
        image_loader.load_image(tmp_path / "生产结果" / "05_contour_mask.bmp"), expected_mask
    )
    overlay = image_loader.load_image(tmp_path / "生产结果" / "06_overlay.bmp")
    # A failed refinement remains visibly marked in red at its coarse center.
    np.testing.assert_array_equal(overlay[290, 322], [0, 0, 255])


def test_low_level_defaults_keep_full_debug_and_do_not_mutate_overlay(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    expected_mask = _stub_segmentation(monkeypatch)
    image = np.full(expected_mask.shape, 160, np.uint8)
    components, debug = pipeline.detect_and_refine(image, refine_pad_ratio=0, detect_well_border=False)
    assert debug["full_refine_density"].shape == image.shape
    np.testing.assert_array_equal(debug["contour_mask"], expected_mask)
    before = debug["overlay"].copy()
    first = postprocess.save_outputs("same.png", tmp_path / "one", image, components, debug, SCALE_BAR)
    second = postprocess.save_outputs("same.png", tmp_path / "two", image, components, debug, SCALE_BAR)
    np.testing.assert_array_equal(debug["overlay"], before)
    assert first == second
    assert {p.name for p in (tmp_path / "one").iterdir()} == PRODUCTION_FILES | DEBUG_FILES
    assert not np.array_equal(image_loader.load_image(tmp_path / "one" / "06_overlay.bmp"), before)


@pytest.mark.parametrize("collect_outputs,collect_debug,expected_allocations", [
    (False, False, 0), (False, True, 0), (True, False, 1), (True, True, 2)
])
def test_disabled_collection_skips_full_frame_allocation_and_rendering(
    monkeypatch: pytest.MonkeyPatch, collect_outputs: bool, collect_debug: bool,
    expected_allocations: int,
) -> None:
    expected_mask = _stub_segmentation(monkeypatch)
    image = np.full(expected_mask.shape, 160, np.uint8)
    allocations = []
    render_calls = []
    resize_calls = []
    original_zeros_like = np.zeros_like
    original_cvt_color = cv2.cvtColor
    original_resize = cv2.resize

    def observe_allocations(array, *args, **kwargs):
        if array.shape == image.shape:
            allocations.append(array.shape)
        return original_zeros_like(array, *args, **kwargs)

    def observe_color(array, code, *args, **kwargs):
        render_calls.append(code)
        return original_cvt_color(array, code, *args, **kwargs)

    def observe_resize(array, size, *args, **kwargs):
        resize_calls.append(size)
        return original_resize(array, size, *args, **kwargs)

    monkeypatch.setattr(pipeline.np, "zeros_like", observe_allocations)
    monkeypatch.setattr(pipeline.cv2, "cvtColor", observe_color)
    monkeypatch.setattr(pipeline.cv2, "resize", observe_resize)
    _, debug = pipeline.detect_and_refine(
        image, refine_pad_ratio=0, detect_well_border=False,
        collect_outputs=collect_outputs, collect_debug=collect_debug,
    )
    assert len(allocations) == expected_allocations
    assert len(render_calls) == int(collect_outputs)
    assert len(resize_calls) == (2 if collect_outputs and collect_debug else 0)
    assert (debug["overlay"] is not None) is collect_outputs
    assert (debug["contour_mask"] is not None) is collect_outputs
    assert (debug["full_refine_density"] is not None) is (collect_outputs and collect_debug)


def test_path_reuses_owned_gray_and_memory_input_keeps_ownership(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    owned_gray = _sample()
    caller_image = owned_gray.copy()
    observed = []

    def consume(gray, **kwargs):
        observed.append(gray)
        gray[0, 0] = 0  # Simulate an internal consumer with an in-place operation.
        return {"ok": True}

    monkeypatch.setattr(pipeline, "load_gray_image", lambda path: owned_gray)
    monkeypatch.setattr(pipeline, "_detect_from_normalized_gray", consume)
    assert pipeline.detect_from_path("unused.png", out_dir=None) == {"ok": True}
    assert observed[0] is owned_gray
    assert pipeline.detect_from_gray(caller_image) == {"ok": True}
    assert not np.shares_memory(observed[1], caller_image)
    assert caller_image[0, 0] == 210


def test_repeated_real_detection_releases_internal_image_buffers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    refs: list[weakref.ReferenceType] = []
    previous_roi_refs: list[weakref.ReferenceType] = []
    original_detect = pipeline.detect_and_refine
    original_refine = pipeline.refine_contour_in_roi

    def observe_refine(*args, **kwargs):
        # Releasing at function return is insufficient for peak memory: the
        # previous ROI must also be gone before the next GrabCut starts.
        assert all(ref() is None for ref in previous_roi_refs)
        item, debug = original_refine(*args, **kwargs)
        previous_roi_refs.clear()
        for payload in (item or {}, debug):
            previous_roi_refs.extend(
                weakref.ref(value) for value in payload.values() if isinstance(value, np.ndarray)
            )
        refs.extend(previous_roi_refs)
        return item, debug

    def observe_detect(gray, *args, **kwargs):
        refs.append(weakref.ref(gray))
        components, debug = original_detect(gray, *args, **kwargs)
        refs.extend(weakref.ref(value) for value in debug.values() if isinstance(value, np.ndarray))
        return components, debug

    monkeypatch.setattr(pipeline, "detect_and_refine", observe_detect)
    monkeypatch.setattr(pipeline, "refine_contour_in_roi", observe_refine)
    retained_results = []
    image = _sample()
    for iteration in range(9):
        output = None if iteration % 3 == 0 else tmp_path / str(iteration)
        retained_results.append(_run(image, output, save_debug=iteration % 3 == 2, scale_bar=SCALE_BAR))
        gc.collect()
        assert refs, "The probe must observe actual NumPy image buffers."
        assert all(ref() is None for ref in refs), f"An internal image survived iteration {iteration}."
    assert all(result["component_count"] == 2 for result in retained_results)
    assert len(refs) > 50


@pytest.mark.parametrize("value", [None, 0, 1, "false", "true", [], {}])
def test_save_debug_requires_a_boolean(value) -> None:
    with pytest.raises(ValueError, match="save_debug must be a boolean"):
        pipeline.detect_from_gray(_sample(False), save_debug=value)

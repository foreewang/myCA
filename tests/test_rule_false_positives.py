"""Regression for rotated textured streaks and rule candidate counting."""
import cv2
import numpy as np
import pytest

from vision.vision.detect_pipeline import detect_from_gray
from workflow.detect_api import normalize_detect_result, rule_texture_kwargs


@pytest.mark.parametrize("angle", [0, 35, 75])
def test_rotated_textured_streak_is_review_not_clone(angle):
    mask = np.zeros((800, 800), np.uint8)
    box = cv2.boxPoints(((400, 400), (440, 85), angle)).astype(np.int32)
    cv2.fillPoly(mask, [box], 255)
    image = np.full(mask.shape, 180, np.uint8)
    noise = np.random.default_rng(23).normal(0, 18, mask.shape)
    image[mask > 0] = np.clip(180 + noise[mask > 0], 0, 255).astype(np.uint8)
    baseline = normalize_detect_result(detect_from_gray(
        image, texture_backend="cpu", min_rotated_aspect_ratio=0))
    assert baseline["clone_count"] == 1
    tuned = normalize_detect_result(detect_from_gray(image, texture_backend="cpu"))
    assert tuned["clone_count"] == 0
    assert tuned["review_candidate_count"] == 1
    assert tuned["review_candidates"][0]["review_reasons"] == ["elongated_texture"]
    assert not tuned["review_candidates"][0]["is_pickable"]


@pytest.mark.parametrize("threshold", [-.1, 1.1, float("nan"), float("inf")])
def test_invalid_shape_threshold(threshold):
    with pytest.raises(ValueError, match="min_rotated_aspect_ratio"):
        detect_from_gray(np.zeros((40, 40), np.uint8), min_rotated_aspect_ratio=threshold)


def test_rule_failed_candidates_do_not_inflate_count_or_change_third_party_contract():
    raw = {"component_count": 2, "components": [
        {"id": "C01", "center_pixel": [10, 20], "is_valid_for_compensation": True},
        {"id": "C02", "center_pixel": [40, 50], "is_valid_for_compensation": False,
         "segmentation_status": "insufficient_instance_support"},
    ]}
    assert normalize_detect_result(raw)["clone_count"] == 2
    raw["texture_processing"] = {"algorithm": "connected_texture_v1"}
    normalized = normalize_detect_result(raw)
    assert normalized["clone_count"] == 1
    assert normalized["review_candidate_count"] == 1
    assert normalized["clones"][0]["clone_id"] == "C01"
    assert normalized["review_candidates"][0]["clone_id"] == "C02"
    assert len(raw["components"]) == 2


def test_shape_tuning_is_forwarded_only_to_rule_entrypoints():
    config = {"min_rotated_aspect_ratio": .25}
    for entrypoint in ("vision.vision.detect_pipeline:process_image", "vision.detect_pipeline:process_image"):
        assert rule_texture_kwargs(entrypoint, config) == config
    assert rule_texture_kwargs("vision.vision.instance_pipeline:process_image", config) == {}
    assert rule_texture_kwargs("custom:process_image", config) == {}

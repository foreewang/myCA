"""Dense colonies must separate from textured background without shrinking."""
import cv2
import numpy as np
import pytest

from vision.vision.detect_pipeline import detect_from_gray
from vision.vision.texture_segment import refine_texture_roi
from workflow.detect_api import normalize_detect_result


@pytest.mark.parametrize("kind", ["dark", "textured"])
def test_whole_colony_on_textured_background(kind):
    rng = np.random.default_rng(117)
    image = np.clip(rng.normal(220, 10, (1024, 1024)), 0, 255).astype(np.uint8)
    truth = np.zeros_like(image)
    cv2.ellipse(truth, (700, 600), (140, 100), 20, 0, 360, 255, -1)
    mean, std = (70, 4) if kind == "dark" else (200, 40)
    values = rng.normal(mean, std, image.shape)
    image[truth > 0] = np.clip(values[truth > 0], 0, 255).astype(np.uint8)
    raw = detect_from_gray(image, texture_backend="cpu")
    result = normalize_detect_result(raw)
    assert result["clone_count"] == 1
    actual = np.zeros_like(truth)
    cv2.fillPoly(actual, [np.asarray(result["clones"][0]["contour_points"], np.int32)], 255)
    intersection = np.count_nonzero((actual > 0) & (truth > 0))
    union = np.count_nonzero((actual > 0) | (truth > 0))
    assert intersection / union > .75


def test_textured_background_alone_is_not_a_colony():
    image = np.clip(np.random.default_rng(117).normal(220, 10, (1024, 1024)), 0, 255).astype(np.uint8)
    assert detect_from_gray(image, texture_backend="cpu")["component_count"] == 0


def test_smooth_dark_disk_on_textured_background_is_not_a_colony():
    image = np.clip(np.random.default_rng(117).normal(220, 10, (1024, 1024)), 0, 255).astype(np.uint8)
    cv2.circle(image, (700, 600), 110, 30, -1)
    assert detect_from_gray(image, texture_backend="cpu")["component_count"] == 0


def test_final_fragment_cannot_pass_using_pre_refinement_area():
    mask = np.zeros((600, 800), np.uint8)
    cv2.circle(mask, (400, 300), 110, 255, -1)
    image = np.full(mask.shape, 180, np.uint8)
    noise = np.random.default_rng(23).normal(180, 18, mask.shape)
    image[mask > 0] = np.clip(noise[mask > 0], 0, 255).astype(np.uint8)

    def shrink(gray, initial, center, **kwargs):
        fragment = np.zeros_like(initial)
        cv2.circle(fragment, (400, 300), 35, 255, -1)
        return fragment, True, {}

    refined, _ = refine_texture_roi(
        image, [400, 300], instance_support=mask,
        clip_bbox_local=[0, 0, 800, 600], texture_backend="cpu",
        edge_refine_method="grabcut", grabcut=shrink,
    )
    assert refined["segmentation_status"] == "insufficient_instance_support"
    assert refined["retained_support_ratio"] < .15
    assert not refined["is_valid_for_compensation"]

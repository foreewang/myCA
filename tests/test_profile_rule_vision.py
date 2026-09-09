import cv2
import numpy as np
import pytest

from tools.profile_rule_vision import StageProfiler, profile_image
from vision.vision.detect_pipeline import process_image
from vision.vision.image_loader import load_image


def test_nested_time_is_exclusive_and_sums_to_total():
    ticks = iter([0., 1., 3., 5.])
    profiler = StageProfiler(clock=lambda: next(ticks))
    with profiler.span('parent'):
        with profiler.span('child'):
            pass
    assert profiler.seconds == {'parent': 3., 'child': 2.}
    assert sum(profiler.seconds.values()) == profiler.events[-1]['inclusive_s'] == 5.


def test_profiling_preserves_real_results_pixels_and_restores_functions(tmp_path):
    gray = np.full((512, 512), 180, np.uint8)
    mask = np.zeros_like(gray)
    cv2.circle(mask, (250, 250), 90, 255, -1)
    noise = np.random.default_rng(9).normal(0, 18, gray.shape)
    gray[mask > 0] = np.clip(180 + noise[mask > 0], 0, 255).astype(np.uint8)
    path = tmp_path / 'image.png'
    cv2.imencode('.png', gray)[1].tofile(str(path))
    original_fromfile, original_circle = np.fromfile, cv2.circle
    expected = process_image(path, out_dir=tmp_path/'normal', texture_backend='cpu')
    timing, actual = profile_image(path, tmp_path/'profiled', backend='cpu')
    assert timing['error'] is None
    assert actual == expected
    assert timing['pickable_count'] == 1
    assert sum(timing['stage_seconds'].values()) == pytest.approx(timing['total_s'], abs=1e-9)
    assert timing['stage_seconds']['roi_texture_compute'] > 0
    assert timing['stage_seconds']['save_mask'] > 0
    assert timing['stage_seconds']['save_json'] > 0
    assert np.fromfile is original_fromfile and cv2.circle is original_circle
    for name in ('05_contour_mask.bmp', '06_overlay.bmp'):
        np.testing.assert_array_equal(load_image(tmp_path/'normal'/name), load_image(tmp_path/'profiled'/name))


def test_failed_input_is_reported_and_instrumentation_is_restored(tmp_path):
    original = np.fromfile
    timing, result = profile_image(tmp_path/'missing.bmp', tmp_path/'out', backend='cpu')
    assert result is None and 'FileNotFoundError' in timing['error']
    assert np.fromfile is original
    assert sum(timing['stage_seconds'].values()) == pytest.approx(timing['total_s'], abs=1e-9)

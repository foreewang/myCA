"""Texture/geometry regression independent of the unlabelled microscope dataset."""
import json
import os
import inspect

import cv2
import numpy as np
import pytest

from vision.vision import gpu_ops
from vision.vision.detect_pipeline import detect_from_gray, process_image
from vision.vision.preprocess import map_bbox, map_points, resize_keep_ratio
from vision.vision.texture_segment import safe_texture_point


def textured(mask, mean=180, background=180, seed=23):
    image = np.full(mask.shape, background, np.uint8)
    noise = np.random.default_rng(seed).normal(0, 18, mask.shape)
    image[mask > 0] = np.clip(mean + noise[mask > 0], 0, 255).astype(np.uint8)
    return image


def disk(shape=(600, 800), center=(250, 300), radius=95):
    mask = np.zeros(shape, np.uint8)
    cv2.circle(mask, center, radius, 255, -1)
    return mask


def run(image, **kwargs):
    return detect_from_gray(image, texture_backend='cpu', **kwargs)


def raster(component, shape):
    mask = np.zeros(shape, np.uint8)
    cv2.fillPoly(mask, [np.asarray(component['contour_points'], np.int32)], 255)
    return mask


@pytest.mark.parametrize('mean', [55, 180, 215])
def test_bright_and_dark_texture_without_dark_center(mean):
    truth = disk()
    result = run(textured(truth, mean))
    assert result['component_count'] == 1
    component = result['components'][0]
    actual = raster(component, truth.shape)
    iou = np.count_nonzero((actual > 0) & (truth > 0)) / np.count_nonzero((actual > 0) | (truth > 0))
    assert iou > .8
    x, y = component['safe_point']
    assert truth[y, x] and actual[y, x]
    assert component['center_pixel'] == component['safe_point']
    assert component['detection_source'] == 'texture'
    assert not {'dark_core_center_pixel', 'dark_core_area_small',
                'dark_core_area_ratio', 'foreground_ratio'} & component.keys()
    assert component['is_pickable'] is True


@pytest.mark.parametrize('kind', ['blank', 'dark_disk', 'line', 'gradient', 'low_noise'])
def test_non_texture_negatives(kind):
    image = np.full((600, 800), 180, np.uint8)
    if kind == 'dark_disk':
        image[disk() > 0] = 20
    elif kind == 'line':
        cv2.line(image, (0, 80), (799, 400), 20, 8)
    elif kind == 'gradient':
        image[:] = np.linspace(50, 220, 800).astype(np.uint8)
    elif kind == 'low_noise':
        image = np.clip(image + np.random.default_rng(8).normal(0, .3, image.shape), 0, 255).astype(np.uint8)
    assert run(image)['component_count'] == 0


def test_nonconvex_colony_preserves_concavity_and_selects_inside():
    mask = np.zeros((600, 800), np.uint8)
    cv2.rectangle(mask, (140, 120), (380, 460), 255, -1)
    cv2.rectangle(mask, (210, 0), (310, 350), 0, -1)
    result = run(textured(mask))
    assert result['component_count'] == 1
    c = result['components'][0]
    actual = raster(c, mask.shape)
    assert actual[200, 260] == 0  # A radial/ellipse contour would fill the U.
    x, y = c['safe_point']
    assert mask[y, x] > 0
    assert c['is_pickable']


def test_two_nearby_colonies_are_not_merged_or_cross_centered():
    one = disk(center=(220, 300), radius=85)
    two = disk(center=(420, 300), radius=85)
    result = run(textured(cv2.bitwise_or(one, two)))
    assert result['component_count'] == 2
    points = [c['safe_point'] for c in result['components']]
    assert sum(one[y, x] > 0 for x, y in points) == 1
    assert sum(two[y, x] > 0 for x, y in points) == 1
    assert all(c['is_pickable'] for c in result['components'])


def test_clipped_colony_retains_border_metadata():
    result = run(textured(disk(center=(20, 300))))
    assert result['component_count'] == 1
    c = result['components'][0]
    assert c['image_edge_clipped'] and 'left' in c['image_border_sides']
    assert c['safe_point'][0] > 0
    assert run(textured(disk(center=(20, 300))), reject_border_touch=True)['component_count'] == 0


def test_insufficient_clearance_never_authorizes_downstream():
    result = run(textured(disk()), safe_margin_px=1000)
    assert result['component_count'] == 1
    assert not result['components'][0]['is_valid_for_compensation']
    assert not result['components'][0]['is_pickable']


def test_disconnected_texture_mean_is_not_used_as_point():
    mask = np.zeros((100, 200), np.uint8)
    mask[20:80, 10:60] = 255
    mask[20:80, 140:190] = 255
    point, distance = safe_texture_point(mask, mask, mask, margin=10)
    assert mask[point[1], point[0]] and distance >= 10


def test_large_non_integer_scaled_image_returns_original_coordinates():
    truth = disk(shape=(1537, 2501), center=(1980, 1000), radius=205)
    result = run(textured(truth))
    assert result['component_count'] == 1
    c = result['components'][0]
    x, y = c['safe_point']
    assert truth[y, x] and x > 1700 and y > 750
    actual = raster(c, truth.shape)
    iou = np.count_nonzero((actual > 0) & (truth > 0)) / np.count_nonzero((actual > 0) | (truth > 0))
    assert iou > .85


def test_large_texture_hole_is_excluded_from_safe_domain():
    truth = disk(radius=120)
    hole = disk(radius=50)
    truth[hole > 0] = 0
    result = run(textured(truth))
    assert result['component_count'] == 1
    x, y = result['components'][0]['safe_point']
    assert truth[y, x] and not hole[y, x]


def test_actual_axis_scaling_roundtrips_non_square_non_integer_dimensions():
    shape = (1237, 1999)
    small, _ = resize_keep_ratio(np.zeros(shape, np.uint8), 1024)
    assert max(small.shape) == 1024
    pts = np.array([[0, 0], [1998, 1236], [831.25, 557.75]])
    np.testing.assert_allclose(map_points(map_points(pts, shape, small.shape), small.shape, shape), pts, atol=1e-9)
    assert map_bbox([0, 0, small.shape[1], small.shape[0]], small.shape, shape) == [0, 0, 1999, 1237]


def test_auto_failure_latches_but_explicit_cuda_raises(monkeypatch):
    gpu_ops.reset_backend()
    calls = []
    def fail(*args):
        calls.append(1)
        raise RuntimeError('simulated device loss')
    monkeypatch.setattr(gpu_ops, '_cuda_moments', fail)
    image = textured(disk())
    for _ in range(2):
        _, meta = gpu_ops.texture_moments(image, backend='auto')
        assert meta['backend'] == 'cpu' and 'device loss' in meta['fallback_reason']
    assert len(calls) == 1
    with pytest.raises(RuntimeError, match='CUDA texture computation failed'):
        gpu_ops.texture_moments(image, backend='cuda')
    gpu_ops.reset_backend()


def test_process_image_downstream_normalization(tmp_path):
    from workflow.detect_api import run_detect_on_image
    image = textured(disk())
    path = tmp_path / 'texture.png'
    cv2.imencode('.png', image)[1].tofile(str(path))
    raw = process_image(path, texture_backend='cpu')
    normalized = run_detect_on_image(str(path), entrypoint='vision.vision.detect_pipeline:process_image',
                                    detect_kwargs={'texture_backend': 'cpu', 'mm_per_pixel': {'x': .01, 'y': .01}})
    assert raw['component_count'] == len(normalized['clones']) == 1
    assert list(normalized['clones'][0]['center_px']) == raw['components'][0]['safe_point']
    json.dumps(raw, allow_nan=False)
    assert set(raw['texture_processing']) == {'algorithm', 'coarse_backend', 'fallback_reason'}
    assert 'coarse_seed_thresh' not in raw
    saved = process_image(path, texture_backend='cpu', out_dir=tmp_path / 'outputs')
    assert saved == raw
    on_disk = json.loads((tmp_path / 'outputs' / '07_result.json').read_text(encoding='utf-8'))
    assert 'coarse_seed_thresh' not in on_disk


def test_scale_bar_still_works_without_segmentation_scale_parameter(tmp_path):
    image = textured(disk())
    result = detect_from_gray(image, texture_backend='cpu', out_dir=tmp_path,
                              scale_bar={'mm_per_pixel': .002, 'length_mm': .1})
    assert result['scale_bar']['mm_per_pixel'] == .002


def test_failed_components_omit_obsolete_dark_fields(tmp_path):
    image = textured(disk())
    # A tight ROI raises the background estimate into the colony texture and
    # exercises the failed-refinement result builder with a real coarse target.
    result = detect_from_gray(image, texture_backend='cpu', refine_pad_ratio=0,
                              out_dir=tmp_path)
    assert result['components'] and not result['components'][0]['contour_points']
    removed = {'dark_core_center_pixel', 'dark_core_area_small',
               'dark_core_area_ratio', 'foreground_ratio'}
    for component in result['components']:
        assert not removed & component.keys()


@pytest.mark.parametrize('name', [
    'seed_thresh', 'seed_quantile', 'seed_hard_floor', 'seed_hard_ceil',
    'core_density_min', 'min_foreground_ratio', 'max_foreground_ratio',
    'min_dark_core_area_ratio', 'max_dark_core_area_ratio',
    'radial_mode', 'recenter_iterations', 'mm_per_pixel',
])
def test_removed_public_options_are_rejected_by_array_and_path_entrypoints(tmp_path, name):
    from vision.vision.detect_pipeline import detect_and_refine, detect_from_path
    image = np.full((32, 32), 180, np.uint8)
    path = tmp_path / 'input.png'
    cv2.imencode('.png', image)[1].tofile(str(path))
    for fn, source in ((detect_from_gray, image), (detect_and_refine, image),
                       (detect_from_path, path), (process_image, path)):
        with pytest.raises(TypeError, match=name):
            fn(source, texture_backend='cpu', **{name: 1})


@pytest.mark.parametrize('function_name,removed', [
    ('detect_coarse_rois', ['flat_sigma', 'density_sigma', 'close_kernel',
                          'open_kernel', 'pad_ratio', 'nms_iou_thr']),
    ('refine_contour_in_roi', ['dark_percentile', 'density_sigma',
                             'radial_target_alpha', 'n_angles', 'recenter_min_shift_px']),
])
def test_removed_low_level_options_are_rejected(function_name, removed):
    from vision.vision import segment
    fn = getattr(segment, function_name)
    image = np.full((32, 32), 180, np.uint8)
    args = (image, [16, 16]) if function_name == 'refine_contour_in_roi' else (image,)
    for name in removed:
        with pytest.raises(TypeError, match=name):
            fn(*args, texture_backend='cpu', **{name: 1})


@pytest.mark.parametrize('name,value', [('texture_backend', 'bad'), ('texture_window', 4),
                                      ('texture_noise_floor', 0), ('coarse_work_max', 0)])
def test_invalid_texture_options(name, value):
    with pytest.raises(ValueError):
        detect_from_gray(textured(disk()), **{name: value})


@pytest.mark.skipif(os.environ.get('IPSC_TEST_CUDA') != '1', reason='Set IPSC_TEST_CUDA=1 for required real GPU tests')
def test_real_cuda_moments_and_final_instances():
    mask = cv2.bitwise_or(disk(center=(220, 300), radius=85), disk(center=(420, 300), radius=85))
    image = textured(mask)
    cpu, _ = gpu_ops.texture_moments(image, backend='cpu')
    cuda, meta = gpu_ops.texture_moments(image, backend='cuda')
    assert meta['backend'] == 'cuda'
    for left, right in zip(cpu, cuda):
        np.testing.assert_allclose(left, right, atol=.02, rtol=0)
    expected = detect_from_gray(image, texture_backend='cpu')
    actual = detect_from_gray(image)  # No override: the production default must execute CUDA.
    assert actual['texture_processing']['coarse_backend'] == 'cuda'
    assert expected['component_count'] == actual['component_count'] == 2
    for a, b in zip(expected['components'], actual['components']):
        am, bm = raster(a, image.shape), raster(b, image.shape)
        assert np.count_nonzero((am > 0) & (bm > 0)) / np.count_nonzero((am > 0) | (bm > 0)) >= .995
        assert np.max(np.abs(np.array(a['center_pixel']) - b['center_pixel'])) <= 1
        assert a['is_pickable'] == b['is_pickable']


def test_all_texture_entrypoints_default_to_cuda():
    from vision.vision import detect_pipeline, preprocess, segment, texture_segment
    assert gpu_ops.DEFAULT_TEXTURE_BACKEND == 'cuda'
    for fn, key in [(gpu_ops.texture_moments, 'backend'), (preprocess.texture_signal, 'backend'),
                    (segment.detect_coarse_rois, 'texture_backend'),
                    (segment.refine_contour_in_roi, 'texture_backend'),
                    (texture_segment.coarse_texture_rois, 'texture_backend'),
                    (texture_segment.refine_texture_roi, 'texture_backend'),
                    (detect_pipeline.detect_and_refine, 'texture_backend'),
                    (detect_pipeline.detect_from_gray, 'texture_backend')]:
        assert inspect.signature(fn).parameters[key].default == 'cuda'


def test_default_gpu_failure_does_not_silently_use_cpu(monkeypatch):
    def fail(*args):
        raise RuntimeError('device unavailable')
    monkeypatch.setattr(gpu_ops, '_cuda_moments', fail)
    with pytest.raises(RuntimeError, match='CUDA texture computation failed'):
        detect_from_gray(textured(disk()))


def test_unicode_temp_uses_memory_cache_without_rewriting_temp(monkeypatch):
    monkeypatch.setattr(gpu_ops.os, 'name', 'nt')
    monkeypatch.setattr(gpu_ops.tempfile, 'gettempdir', lambda: 'C:/Users/\u738b/Temp')
    monkeypatch.delenv('CUPY_CACHE_IN_MEMORY', raising=False)
    old_temp = os.environ.get('TEMP')
    gpu_ops._configure_cuda_cache()
    assert os.environ['CUPY_CACHE_IN_MEMORY'] == '1'
    assert os.environ.get('TEMP') == old_temp
    monkeypatch.setenv('CUPY_CACHE_IN_MEMORY', '0')
    gpu_ops._configure_cuda_cache()
    assert os.environ['CUPY_CACHE_IN_MEMORY'] == '0'


def test_texture_options_do_not_leak_to_model_entrypoint():
    from workflow.detect_api import rule_texture_kwargs
    cfg = {'texture_backend': 'cpu', 'safe_margin_px': 20, 'seed_thresh': 99}
    assert rule_texture_kwargs(None, cfg) == {}
    assert rule_texture_kwargs('vendor:process_image', cfg) == {}
    assert rule_texture_kwargs('vision.vision.detect_pipeline:process_image', cfg) == {
        'texture_backend': 'cpu', 'safe_margin_px': 20}


def test_closed_loop_recapture_uses_same_texture_options_and_coordinates(tmp_path, monkeypatch):
    from workflow import compensate_executor
    path = tmp_path / 'recapture.png'
    image = textured(disk())
    cv2.imencode('.png', image)[1].tofile(str(path))
    params = {'fov_mm': {'width': 8., 'height': 6.},
              'detect_rule_options': {'texture_backend': 'cpu', 'safe_margin_px': 12}}
    captured = {}
    actual_detect = compensate_executor.run_detect_on_image
    def observe(*args, **kwargs):
        captured.update(kwargs['detect_kwargs'])
        return actual_detect(*args, **kwargs)
    monkeypatch.setattr(compensate_executor, 'run_detect_on_image', observe)
    item = compensate_executor._build_image_item_from_capture(
        image_path=str(path), params=params, stage_x_actual=100, stage_y_actual=200,
        detect_entrypoint='vision.vision.detect_pipeline:process_image')
    assert captured['texture_backend'] == 'cpu'
    assert captured['safe_margin_px'] == 12
    assert len(item['clones']) == 1
    c = item['clones'][0]
    assert c['offset_from_image_center_px'] == [c['center_px'][0]-400, c['center_px'][1]-300]
    assert c['is_valid_for_compensation'] and c['is_pickable']

"""Production contracts for the user-accepted green single-frame candidates."""
import json

import cv2
import numpy as np
import pytest

from vision.vision.single_image_refine_v4 import analyze
from vision.vision.detect_pipeline import detect_from_gray, process_image
from vision.vision.image_loader import load_gray_image, save_image
from workflow.detect_api import normalize_detect_result, run_detect_on_image


def sample():
    image = np.full((640,640),225,np.uint8)
    mask = np.zeros_like(image)
    cv2.circle(mask,(180,320),110,255,-1)
    noise = np.random.default_rng(18).normal(180,27,image.shape)
    image[mask>0] = np.clip(noise[mask>0],0,255).astype(np.uint8)
    cv2.circle(image,(470,320),75,35,-1)
    return image


def test_only_accepted_green_geometry_is_promoted_and_input_unchanged():
    image=sample();before=image.copy()
    frozen,maps=analyze(image,fov_mm=(2.,2.))
    actual=detect_from_gray(image,mm_per_pixel={'x':2/640,'y':2/640})
    expected=[c for c in frozen['components'] if c['status']=='normal_candidate']
    assert actual['component_count']==len(expected)==1
    for a,b in zip(actual['components'],expected):
        for key in ('id','bbox','center_pixel','contour_points','area_mm2','status'):
            assert a[key]==b[key]
        assert a['is_pickable'] and a['is_valid_for_compensation']
        assert not a['requires_human_validation']
    assert any(c['status']=='overgrown' for c in actual['review_candidates'])
    assert all(not c['is_pickable'] and not c['is_valid_for_compensation'] for c in actual['review_candidates'])
    normalized=normalize_detect_result(actual)
    assert normalized['clone_count']==1
    assert normalized['review_candidate_count']==len(actual['review_candidates'])
    np.testing.assert_array_equal(image,before)
    json.dumps(actual,allow_nan=False)


def test_output_modes_serialize_same_reviews_and_preserve_internal_exclusions(tmp_path):
    image=sample()
    kwargs={'src_path':'same.bmp','fov_mm':(2.,2.),'scale_bar':{'mm_per_pixel':2/640,'length_mm':.1}}
    full=detect_from_gray(image,out_dir=tmp_path/'full',save_debug=True,**kwargs)
    minimum=detect_from_gray(image,out_dir=tmp_path/'minimum',**kwargs)
    memory=detect_from_gray(image,**kwargs)
    assert full==minimum
    assert memory=={**full,'scale_bar':None}
    for folder in ('full','minimum'):
        assert json.loads((tmp_path/folder/'07_result.json').read_text(encoding='utf8'))==full
    assert len(list((tmp_path/'full').iterdir()))==7
    assert len(list((tmp_path/'minimum').iterdir()))==3
    mask=load_gray_image(tmp_path/'minimum'/'05_contour_mask.bmp')
    assert mask[320,470]==0
    x,y=full['components'][0]['center_pixel']
    assert mask[y,x]>0


@pytest.mark.parametrize('alias',['vision.vision.detect_pipeline:process_image','vision.detect_pipeline:process_image'])
def test_workflow_aliases_use_physical_calibration_and_match_direct_result(tmp_path,alias):
    path=tmp_path/'sample.bmp';save_image(path,sample())
    scale={'x':2/640,'y':2/640}
    result=run_detect_on_image(path,entrypoint=alias,detect_kwargs={'mm_per_pixel':scale,'texture_backend':'auto'})
    assert result['clone_count']==1
    assert result['review_candidate_count']>0
    raw=result['raw_result']
    assert raw['texture_processing']['physical_scale_source']=='mm_per_pixel'
    assert raw['texture_processing']['coarse_backend']=='cpu'
    assert raw['texture_processing']['requested_backend']=='auto'
    assert raw['fov_mm']=={'width':2.,'height':2.}
    assert all(c['is_pickable'] for c in result['clones'])
    assert not any(c['is_pickable'] for c in result['review_candidates'])


def test_scan_recapture_and_pickable_export_contain_only_green(tmp_path):
    from workflow.detect_executor import execute_detect_on_scan_result
    from workflow.compensate_executor import _build_image_item_from_capture
    from workflow.pickable_result import build_pickable_detect_result
    path=tmp_path/'sample.bmp';save_image(path,sample())
    entry='vision.vision.detect_pipeline:process_image'
    plate={'pulses_per_mm':{'x':1000,'y':2000},'x_stage_sign_for_view_right':-1,'y_stage_sign_for_view_down':1}
    params={'task_id':'single-image-test','objective_name':'4x','plate_type':'test','well_name':'A1','fov_mm':{'width':2.,'height':2.}}
    scan={'scan_config':{'fov_mm':params['fov_mm']},'captures':[{'index':1,'row_index':0,'col_index':0,
          'stage_x_target':100,'stage_y_target':200,'capture_result':{'saved_path':str(path)}}]}
    result=execute_detect_on_scan_result({'plate':plate,'task':{'detect':{'entrypoint':entry,'save_overlay':False}}},params,scan)
    recapture=_build_image_item_from_capture(image_path=str(path),params=params,stage_x_actual=100,stage_y_actual=200,detect_entrypoint=entry)
    a=result['images'][0]['clones'];b=recapture['clones']
    assert len(a)==len(b)==1
    assert a[0]['center_px']==b[0]['center_px']
    assert a[0]['is_pickable'] and b[0]['is_pickable']
    output=build_pickable_detect_result(result)
    assert output['total_clone_count']==1
    assert result['images'][0]['review_candidates']
    assert not any(c['is_pickable'] for c in result['images'][0]['review_candidates'])


def test_explicit_safe_margin_demotes_instead_of_authorizing(tmp_path):
    result=detect_from_gray(sample(),fov_mm=(2.,2.),safe_margin_px=10000,out_dir=tmp_path)
    assert result['component_count']==0
    assert any('no_safe_interior' in c['review_reasons'] for c in result['review_candidates'])
    assert not np.any(load_gray_image(tmp_path/'05_contour_mask.bmp'))


def test_frozen_legacy_remains_explicit_and_equal():
    from vision.vision.legacy_detect_pipeline import detect_from_gray as old
    image=sample()
    assert detect_from_gray(image,rule_algorithm='connected_texture_v1',texture_backend='cpu')==old(image,texture_backend='cpu')


@pytest.mark.parametrize('kwargs',[
    {'mm_per_pixel':0},{'mm_per_pixel':float('nan')},
    {'mm_per_pixel':.01,'fov_mm':(2.,2.)},
    {'rule_algorithm':'unknown'}, {'single_image_options':{'unknown':2}},
    {'single_image_options':{'normal_min_solidity':2}},
    {'single_image_options':{'seed_occupancy':.5}},
])
def test_invalid_calibration_and_rule_options_fail(kwargs):
    with pytest.raises(ValueError):
        detect_from_gray(sample(),**kwargs)

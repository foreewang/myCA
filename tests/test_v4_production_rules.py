"""Safety and compatibility contracts at the V4 production boundary."""
import importlib
import cv2
import numpy as np
import pytest
from vision.vision import detect_pipeline as pipeline
from tests.test_accepted_single_image_rules import sample


def test_default_v4_and_explicit_previous_version_remain_distinct():
    current=pipeline.detect_from_gray(sample(),fov_mm=(2.,2.))
    previous=pipeline.detect_from_gray(sample(),fov_mm=(2.,2.),rule_algorithm='single_image_rules_v2')
    assert current['texture_processing']['algorithm']=='single_image_rules_v4'
    assert previous['texture_processing']['algorithm']=='single_image_rules_v2'
    from vision.experiments.single_image_rules import analyze
    frozen,_=analyze(sample(),fov_mm=(2.,2.))
    expected=[c for c in frozen['components'] if c['status']=='normal_candidate']
    assert len(previous['components'])==len(expected)
    for a,b in zip(previous['components'],expected):
        for key in ('id','bbox','center_pixel','contour_points','status','area_mm2'):
            assert a[key]==b[key]
    assert current['rule_revision']=='accepted_ipscell_v4_20260927'
    assert set(current['rule_stage_configs'])=={'segmentation','refine_v2','refine_v3','refine_v4'}


@pytest.mark.parametrize('change',['invalid_point','wrong_owner','dark_hole'])
def test_invalid_final_texture_point_cannot_be_promoted(monkeypatch,change):
    stage=importlib.import_module('vision.vision.single_image_refine_v4')
    original=stage.revise
    def revise(*args,**kwargs):
        result,maps=original(*args,**kwargs)
        c=next(c for c in result['components'] if c['status']=='normal_candidate')
        if change=='invalid_point':c['center_pixel']=None
        else:
            x,y=c['center_pixel'];h,w=maps['gray'].shape
            wx=int((x+.5)*w/result['input_size']['width']);wy=int((y+.5)*h/result['input_size']['height'])
            maps['safe_labels'][wy,wx]=99 if change=='wrong_owner' else 0
            if change=='dark_hole':maps['exclusion_mask'][wy,wx]=255
        return result,maps
    monkeypatch.setattr(stage,'revise',revise)
    result=pipeline.detect_from_gray(sample(),fov_mm=(2.,2.))
    assert result['components']==[]
    assert not any(c['is_pickable'] or c['is_valid_for_compensation'] for c in result['review_candidates'])
    assert any('no_safe_interior' in c['review_reasons'] for c in result['review_candidates'])


def test_safe_margin_is_measured_in_original_pixels():
    result=pipeline.detect_from_gray(sample(),fov_mm=(2.,2.))
    c=result['components'][0]
    assert c['safe_clearance_px']>1 and c['confidence']>0
    below=pipeline.detect_from_gray(sample(),fov_mm=(2.,2.),safe_margin_px=c['safe_clearance_px']-.1)
    above=pipeline.detect_from_gray(sample(),fov_mm=(2.,2.),safe_margin_px=c['safe_clearance_px']+.1)
    assert len(below['components'])==1 and above['components']==[]


def test_border_and_max_keep_restrictions_still_demote():
    image=np.full((640,640),225,np.uint8)
    texture=np.random.default_rng(77).normal(180,27,image.shape).clip(0,255).astype(np.uint8)
    mask=np.zeros_like(image);cv2.circle(mask,(10,300),140,255,-1);cv2.circle(mask,(450,300),105,255,-1)
    image[mask>0]=texture[mask>0]
    baseline=pipeline.detect_from_gray(image,fov_mm=(2.,2.))
    assert len(baseline['components'])==2
    bounded=pipeline.detect_from_gray(image,fov_mm=(2.,2.),reject_border_touch=True)
    assert len(bounded['components'])==1
    assert all(not c['touch_image_border'] for c in bounded['components'])
    limited=pipeline.detect_from_gray(image,fov_mm=(2.,2.),max_keep=1)
    assert len(limited['components'])==1
    assert any('max_keep_limit' in c['review_reasons'] for c in limited['review_candidates'])

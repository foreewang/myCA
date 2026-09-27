"""Behavioral tests for the isolated experiment, not biology ground truth."""
import cv2
import numpy as np
import pytest

from vision.experiments.single_image_rules import analyze


def scene(size=640, center=(320, 320), radius=180):
    mask = np.zeros((size,size),np.uint8)
    cv2.circle(mask,center,radius,255,-1)
    image = np.full(mask.shape,225,np.uint8)
    values = np.random.default_rng(180).normal(180,27,image.shape)
    image[mask>0] = np.clip(values[mask>0],0,255).astype(np.uint8)
    return image, mask


@pytest.mark.parametrize('kind',['blank','smooth_shadow','sparse_dots'])
def test_negatives_do_not_become_normal_candidates(kind):
    image=np.full((640,640),225,np.uint8)
    if kind=='smooth_shadow':
        image[:]=np.rint(np.linspace(5,225,640)).astype(np.uint8)
    elif kind=='sparse_dots':
        for y in range(30,620,40):
            for x in range(30,620,40):
                cv2.circle(image,(x,y),3,35,-1)
    result,_=analyze(image,fov_mm=(2.,2.))
    assert not any(c['status']=='normal_candidate' for c in result['components'])


def test_15mm_colony_is_preserved_across_input_resolutions():
    image,truth=scene(size=1024,center=(512,512),radius=239)
    before=image.copy()
    result,_=analyze(image,fov_mm=(3.22,3.22))
    result2,_=analyze(cv2.resize(image,(2048,2048),interpolation=cv2.INTER_NEAREST),fov_mm=(3.22,3.22))
    normals=[c for c in result['components'] if c['status']=='normal_candidate']
    normals2=[c for c in result2['components'] if c['status']=='normal_candidate']
    assert len(normals)==len(normals2)==1
    assert abs(normals[0]['equivalent_diameter_mm']-1.5)<.12
    assert abs(normals[0]['area_mm2']-normals2[0]['area_mm2'])<.01
    predicted=np.zeros_like(truth)
    cv2.fillPoly(predicted,[np.array(normals[0]['contour_points'],np.int32)],255)
    assert np.count_nonzero((predicted>0)&(truth>0))/np.count_nonzero((predicted>0)|(truth>0))>.85
    assert np.array_equal(image,before)
    assert result['production_enabled'] is False
    assert all(c['is_pickable'] is False for c in result['components'])


def test_large_dark_core_cannot_leave_an_accepted_textured_ring():
    image,_=scene()
    cv2.circle(image,(320,320),75,35,-1)
    result,maps=analyze(image,fov_mm=(2.,2.))
    assert any(c['status']=='overgrown' for c in result['dark_regions'])
    assert not any(c['status']=='normal_candidate' for c in result['components'])
    assert np.count_nonzero(maps['accepted_mask'])==0


def test_neighbor_dark_blob_does_not_veto_separate_colony():
    image,_=scene(center=(170,320),radius=110)
    cv2.circle(image,(470,320),75,35,-1)
    result,_=analyze(image,fov_mm=(2.,2.))
    normals=[c for c in result['components'] if c['status']=='normal_candidate']
    assert len(normals)==1
    assert normals[0]['center_pixel'][0]<300
    assert any(c['status']=='overgrown' for c in result['dark_regions'])


def test_small_dark_spot_does_not_veto_whole_colony_or_select_dark_point():
    image,_=scene()
    cv2.circle(image,(320,320),24,35,-1)
    result,maps=analyze(image,fov_mm=(2.,2.))
    normals=[c for c in result['components'] if c['status']=='normal_candidate']
    assert len(normals)==1
    x,y=normals[0]['center_pixel']
    assert (x-320)**2+(y-320)**2>24**2
    assert not np.any((maps['accepted_mask']>0)&(maps['dark_mask']>0))


def test_edge_colony_is_retained_with_truncation_metadata():
    image,_=scene(center=(630,25),radius=200)
    result,_=analyze(image,fov_mm=(2.,2.))
    assert result['components']
    c=max(result['components'],key=lambda c:c['area_mm2'])
    assert c['touch_image_border'] and c['size_is_lower_bound']
    assert 'image_edge_clipped' in c['review_reasons']


@pytest.mark.parametrize('fov',[(0,2),(-1,2),(float('nan'),2)])
def test_invalid_physical_scale_is_rejected(fov):
    with pytest.raises(ValueError,match='fov_mm'):
        analyze(np.zeros((64,64),np.uint8),fov_mm=fov)

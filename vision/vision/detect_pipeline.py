"""Production rules using the accepted V4 single-image green candidates.

Select rule_algorithm='connected_texture_v1' for the frozen old algorithm.
Model inference keeps its separate entrypoint.
"""
from dataclasses import fields
from collections.abc import Mapping

import cv2
import numpy as np

from .image_loader import load_gray_image, to_gray_u8
from .postprocess import save_outputs
from .single_image_segment import SingleImageConfig, segment_single_image

RULE_ALGORITHM = 'single_image_rules_v4'
PREVIOUS_ALGORITHM = 'single_image_rules_v2'
LEGACY_ALGORITHM = 'connected_texture_v1'
LEGACY_OPTIONS = {
    'coarse_work_max', 'refine_pad_ratio', 'edge_refine_method',
    'edge_refine_iterations', 'max_bbox_area_ratio', 'refine_clip_to_coarse_bbox',
    'refine_clip_pad_ratio', 'texture_noise_floor', 'texture_window',
    'refine_work_max', 'min_rotated_aspect_ratio',
}


def _options(options):
    unknown = set(options) - LEGACY_OPTIONS
    if unknown:
        raise TypeError(f'Unsupported rule arguments: {", ".join(sorted(unknown))}')


def _physical_scale(shape, mm_per_pixel, fov_mm):
    h, w = shape
    explicit = None
    if fov_mm is not None:
        explicit = ([fov_mm['width'], fov_mm['height']] if isinstance(fov_mm, Mapping) else fov_mm)
        if len(explicit) != 2:
            raise ValueError('fov_mm must contain width and height')
        explicit = tuple(float(v) for v in explicit)
    if mm_per_pixel is not None:
        pixel = ([mm_per_pixel['x'], mm_per_pixel['y']] if isinstance(mm_per_pixel, Mapping)
                 else [mm_per_pixel, mm_per_pixel])
        if any(isinstance(v, bool) for v in pixel):
            raise ValueError('mm_per_pixel must be positive finite numbers')
        pixel = tuple(float(v) for v in pixel)
        if not all(np.isfinite(v) and v > 0 for v in pixel):
            raise ValueError('mm_per_pixel must be positive finite numbers')
        calculated = (pixel[0]*w, pixel[1]*h)
        if explicit is not None and not np.allclose(explicit, calculated, rtol=1e-6, atol=1e-9):
            raise ValueError('fov_mm conflicts with mm_per_pixel and image dimensions')
        explicit, source = calculated, 'mm_per_pixel'
    else:
        source = 'fov_mm' if explicit is not None else 'default_4x_fov'
        explicit = explicit if explicit is not None else (3.22, 3.22)
    if not all(np.isfinite(v) and v > 0 for v in explicit):
        raise ValueError('fov_mm must contain positive finite numbers')
    return explicit, source


def detect_and_refine(
    gray, *, mm_per_pixel=None, fov_mm=None, single_image_options=None,
    rule_algorithm=RULE_ALGORITHM, texture_backend=None, safe_margin_px=1.0,
    max_keep=None, reject_border_touch=False, collect_outputs=True,
    collect_debug=True, **legacy_options,
):
    """Return accepted components plus rendering/result metadata.

    Defaults preserve the accepted review, including clipped green candidates.
    Explicit restrictions (margin, border rejection, max_keep) may demote these
    candidates; excluded records remain in review_candidates.
    """
    _options(legacy_options)
    # Only V4 changes its default; explicit older algorithms retain CPU default.
    if texture_backend is None:
        texture_backend = 'auto' if rule_algorithm == RULE_ALGORITHM else 'cpu'
    if rule_algorithm == LEGACY_ALGORITHM:
        from .legacy_detect_pipeline import detect_and_refine as legacy_detect
        return legacy_detect(gray, texture_backend=texture_backend,
                             safe_margin_px=safe_margin_px, max_keep=max_keep,
                             reject_border_touch=reject_border_touch,
                             collect_outputs=collect_outputs, collect_debug=collect_debug,
                             **legacy_options)
    if rule_algorithm not in {RULE_ALGORITHM, PREVIOUS_ALGORITHM}:
        raise ValueError(f'Unknown rule_algorithm: {rule_algorithm}')
    if texture_backend not in {'cpu', 'cuda', 'auto'}:
        raise ValueError('texture_backend must be cpu, cuda or auto')
    if not np.isfinite(safe_margin_px) or safe_margin_px < 0:
        raise ValueError('safe_margin_px must be finite and nonnegative')
    if max_keep is not None and (isinstance(max_keep, bool) or not isinstance(max_keep, int) or max_keep < 1):
        raise ValueError('max_keep must be a positive integer or None')
    if not isinstance(reject_border_touch, bool):
        raise ValueError('reject_border_touch must be a boolean')
    if single_image_options is not None and not isinstance(single_image_options, Mapping):
        raise ValueError('single_image_options must be a mapping')
    options = dict(single_image_options or {})
    unknown = set(options) - {f.name for f in fields(SingleImageConfig)}
    if unknown:
        raise ValueError(f'Unknown single_image_options: {", ".join(sorted(unknown))}')
    fov, scale_source = _physical_scale(gray.shape, mm_per_pixel, fov_mm)
    base, base_maps = segment_single_image(
        gray, fov_mm=fov, config=SingleImageConfig(**options),
        background_backend=texture_backend if rule_algorithm == RULE_ALGORITHM else 'cpu')
    evidence, maps = base, base_maps
    if rule_algorithm == RULE_ALGORITHM:
        # These are frozen, image-only refinements from the accepted V4 report.
        # The production adapter alone authorizes normal candidates for picking.
        from .single_image_refine_v2 import refine_evidence
        from .single_image_refine_v3 import revise as revise_v3
        from .single_image_refine_v4 import revise as revise_v4
        v2, m2 = refine_evidence(base, base_maps)
        v3, m3 = revise_v3(base, base_maps, v2, m2)
        evidence, maps = revise_v4(base, base_maps, v3, m3)
    h, w = gray.shape
    wh, ww = maps['gray'].shape
    accepted, reviews, accepted_labels = [], [], []
    for candidate in evidence['components'] + evidence['dark_regions']:
        item = dict(candidate)
        points = np.asarray(item['contour_points'], np.int32)
        if 'center_pixel' not in item:
            x, y, bw, bh = item['bbox']
            item['center_pixel'] = [x+bw//2, y+bh//2]
        normal = item['status'] == 'normal_candidate'
        clearance = float(item.get('safe_clearance_px', 0.))
        occupancy_mean = float(item.get('texture_occupancy_mean', 0.))
        if rule_algorithm == RULE_ALGORITHM and normal:
            # Measure clearance at the reviewed point, not at a potentially
            # different maximum, and retain holes / dark exclusions.
            safe = maps['safe_labels'] == int(item['id'][1:])
            center = item.get('center_pixel')
            clearance = 0.
            if center is not None and 0 <= center[0] < w and 0 <= center[1] < h:
                x = min(ww-1, int((center[0]+.5)*ww/w))
                y = min(wh-1, int((center[1]+.5)*wh/h))
                if safe[y, x]:
                    distance = cv2.distanceTransform(np.pad(safe.astype(np.uint8), 1), cv2.DIST_L2, 5)[1:-1,1:-1]
                    clearance = float(distance[y, x]) * min(w/ww, h/wh)
            occupancy_mean = float(maps['occupancy'][safe].mean()) if safe.any() else 0.
        reasons = list(item.get('review_reasons') or [])
        if normal and clearance < max(1., safe_margin_px):
            normal = False
            reasons.append('no_safe_interior')
        if normal and reject_border_touch and item['touch_image_border']:
            normal = False
            reasons.append('image_border_rejected')
        if normal and max_keep is not None and len(accepted) >= max_keep:
            normal = False
            reasons.append('max_keep_limit')
        if not normal and not reasons:
            reasons.append(item['status'])
        tolerance_x, tolerance_y = max(1.,w/ww), max(1.,h/wh)
        sides = [side for side, touched in (
            ('left',points[:,0].min()<tolerance_x), ('right',points[:,0].max()>=w-tolerance_x),
            ('top',points[:,1].min()<tolerance_y), ('bottom',points[:,1].max()>=h-tolerance_y),
        ) if item['touch_image_border'] and touched]
        confidence = float(occupancy_mean * item.get('solidity',0.)) if normal else 0.
        item.update(is_pickable=normal, is_valid_for_compensation=normal,
                    requires_human_validation=not normal, location_valid=normal,
                    safe_point=list(item['center_pixel']) if normal else None,
                    safe_margin_px=float(safe_margin_px), safe_clearance_px=clearance,
                    safe_point_method='accepted_texture_distance' if normal else None,
                    area_px=int(round(cv2.contourArea(points))),
                    confidence=confidence, detection_confidence=confidence,
                    segmentation_status='success' if normal else reasons[-1],
                    detection_source=rule_algorithm, image_border_sides=sides,
                    image_edge_clipped=item['touch_image_border'], truncated=item['touch_image_border'],
                    review_reasons=reasons,
                    quality_assessment={'status':'accepted' if normal else 'excluded',
                                        'method':rule_algorithm, 'label':item['status']})
        if normal:
            accepted.append(item)
            accepted_labels.append(int(item['id'][1:]))
        else:
            reviews.append(item)

    background_processing = base_maps['background_processing']
    metadata = {'algorithm':rule_algorithm, 'coarse_backend':'cpu',
                'requested_backend':texture_backend,
                'stage_backends':{'background_close':background_processing['backend'], 'remaining_stages':'cpu'},
                'fallback_reason':background_processing['fallback_reason'],
                'physical_scale_source':scale_source,
                'ignored_legacy_options':sorted(legacy_options)}
    if rule_algorithm == PREVIOUS_ALGORITHM and texture_backend != 'cpu':
        metadata['fallback_reason'] = 'previous_single_image_algorithm_uses_opencv_cpu'
    result_fields = dict(schema_version=2, objective_name=None, purpose='single_image_rule_detection',
                         review_candidates=reviews, review_candidate_count=len(reviews),
                         fov_mm=evidence['fov_mm'], rule_config=evidence['config'],
                         working_grid=evidence['working_grid'],
                         rejected_candidates=base['rejected_candidates'],
                         quality_assessment={'status':'rule_assessed','method':rule_algorithm})
    if rule_algorithm == RULE_ALGORITHM:
        result_fields.update(
            rule_revision='accepted_ipscell_v4_20260927',
            rule_stage_configs={'segmentation':base['config'], 'refine_v2':v2['config'],
                                'refine_v3':v3['config'], 'refine_v4':evidence['config']},
            suppressed_candidates=evidence['suppressed_candidates'])
    overlay = cv2.cvtColor(gray,cv2.COLOR_GRAY2BGR) if collect_outputs else None
    mask = density = None
    if collect_outputs:
        small_mask = np.where(np.isin(maps['proposal_labels'],accepted_labels),maps['accepted_mask'],0).astype(np.uint8)
        mask = cv2.resize(small_mask,(w,h),interpolation=cv2.INTER_NEAREST)
        for item in accepted:
            cv2.polylines(overlay,[np.asarray(item['contour_points'],np.int32)],True,(0,255,0),max(2,round(w/512)))
            center=tuple(item['center_pixel'])
            cv2.circle(overlay,center,max(3,round(w/200)),(0,0,255),-1)
            cv2.putText(overlay,item['id'],center,cv2.FONT_HERSHEY_SIMPLEX,max(.5,w/3000),(0,0,255),max(1,round(w/1800)))
        if collect_debug:
            density=cv2.resize(np.uint8(np.clip(maps['occupancy'],0,1)*255),(w,h),interpolation=cv2.INTER_LINEAR)
    debug=dict(coarse_flat=np.uint8(np.clip(maps['occupancy'],0,1)*255),
               coarse_binary=base_maps['support'], coarse_scale=max(w/ww,h/wh),
               coarse_density_thresh=base['config']['support_occupancy'],
               coarse_candidate_count=len(evidence['components']),
               overlay=overlay, contour_mask=mask, full_refine_density=density,
               texture_processing=metadata, result_fields=result_fields)
    return accepted, debug


def detect_from_gray(gray, src_path='in_memory', out_dir=None, *, scale_bar=None,
                     save_debug=False, **kwargs):
    return _detect_from_normalized_gray(to_gray_u8(gray),src_path=src_path,out_dir=out_dir,
                                        scale_bar=scale_bar,save_debug=save_debug,**kwargs)


def _detect_from_normalized_gray(gray, src_path='in_memory', out_dir=None, *,
                                 scale_bar=None,save_debug=False, **kwargs):
    if not isinstance(save_debug,bool):
        raise ValueError('save_debug must be a boolean')
    components,debug=detect_and_refine(gray,collect_outputs=out_dir is not None,
                                      collect_debug=save_debug,**kwargs)
    if out_dir is not None:
        return save_outputs(src_path,out_dir,gray,components,debug,scale_bar=scale_bar,
                            save_debug=save_debug,copy_overlay=False)
    from .feature_extract import texture_processing_metadata
    return dict(input_path=str(src_path),input_size={'width':gray.shape[1],'height':gray.shape[0]},
                component_count=len(components), component_ids=[c['id'] for c in components],
                components=components,coarse_density_thresh=debug.get('coarse_density_thresh'),
                coarse_candidate_count=debug.get('coarse_candidate_count',len(components)),
                scale_bar=None,texture_processing=texture_processing_metadata(debug),
                **debug.get('result_fields',{}))


def detect_from_path(image_path,out_dir='outputs_5120_contour_refined_opt',**kwargs):
    return _detect_from_normalized_gray(load_gray_image(image_path),src_path=image_path,out_dir=out_dir,**kwargs)


def process_image(image_path,**kwargs):
    kwargs.setdefault('out_dir',None)
    for key in ('model_dir','provider','allow_cpu_fallback','objective_name'):
        kwargs.pop(key,None)
    return detect_from_path(image_path,**kwargs)

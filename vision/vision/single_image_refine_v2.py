"""Offline, image-only revision of the uncommitted single-frame algorithm.

No filenames, review labels, or manual contours enter inference. All outputs
remain non-pickable until this independent experiment is accepted.
"""
from dataclasses import asdict, dataclass
import math

import cv2
import numpy as np

from .single_image_segment import segment_single_image


@dataclass(frozen=True)
class FeedbackConfig:
    boundary_occupancy: float = .50
    boundary_growth_um: float = 90.
    boundary_close_um: float = 65.
    normal_boundary_growth_um: float = 25.
    normal_boundary_close_um: float = 40.
    fragment_merge_gap_um: float = 180.
    fragment_max_area_mm2: float = .30
    fragment_bridge_occupancy: float = .48
    edge_min_seed_radius_um: float = 90.
    dark_min_area_mm2: float = .025
    dark_median_limit: float = 65.
    dark_margin_um: float = 30.
    rim_min_fine_ratio: float = .32
    rim_min_safe_radius_um: float = 80.
    rim_min_area_mm2: float = .12


def _kernel(radius_um, pitch_um):
    radius = max(1, int(round(radius_um / pitch_um)))
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2*radius+1, 2*radius+1))


def _distance(mask):
    return cv2.distanceTransform(np.pad(mask.astype(np.uint8), 1), cv2.DIST_L2, 5)[1:-1, 1:-1]


def _body(mask):
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    result = np.zeros(mask.shape, np.uint8)
    cv2.drawContours(result, contours, -1, 1, -1)
    return result.astype(bool)


def _raster(item, shape, input_shape):
    ih, iw = input_shape
    h, w = shape
    points = np.rint((np.asarray(item['contour_points'])+.5)*[w/iw, h/ih]-.5).astype(np.int32)
    result = np.zeros(shape, np.uint8)
    cv2.fillPoly(result, [points], 1)
    return result.astype(bool)


def _geometry(mask, input_shape, pixel_area):
    ih, iw = input_shape
    h, w = mask.shape
    contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    contour = max(contours, key=cv2.contourArea)
    x, y, bw, bh = cv2.boundingRect(contour)
    points = np.rint((contour[:, 0].astype(float)+.5)*[iw/w, ih/h]-.5).astype(int)
    points[:, 0] = np.clip(points[:, 0], 0, iw-1)
    points[:, 1] = np.clip(points[:, 1], 0, ih-1)
    area = float(mask.sum()) * pixel_area
    return dict(contour_points=points.tolist(), bbox=[round(x*iw/w), round(y*ih/h), round(bw*iw/w), round(bh*ih/h)],
                touch_image_border=bool(x==0 or y==0 or x+bw==w or y+bh==h), area_mm2=area,
                equivalent_diameter_mm=math.sqrt(4*area/math.pi),
                solidity=float(cv2.contourArea(contour)/max(1., cv2.contourArea(cv2.convexHull(contour)))))


def refine_evidence(base, maps, *, config=None):
    """Refine the frozen geometry; separate colony bodies from exclusion masks."""
    cfg = config or FeedbackConfig()
    if any(not math.isfinite(v) or v <= 0 for v in asdict(cfg).values()):
        raise ValueError('Feedback parameters must be positive and finite')
    if not 0 < cfg.boundary_occupancy < 1 or not 0 < cfg.fragment_bridge_occupancy < 1:
        raise ValueError('Occupancy must lie between zero and one')
    work = maps['gray'].astype(np.float32)
    occupancy = maps['occupancy']
    shape = work.shape
    ih, iw = base['input_size']['height'], base['input_size']['width']
    input_shape = (ih, iw)
    mx = base['working_grid']['mm_per_pixel_x']; my = base['working_grid']['mm_per_pixel_y']
    pixel_area = mx*my; pitch_um = math.sqrt(pixel_area)*1000
    low = maps['low_visibility'] > 0
    old_dark = maps['dark_mask'] > 0
    raw_labels = maps['proposal_labels']
    suppressed = []
    dark_records = []
    exclusion = low.copy()
    edge_evidence = []
    for item in base['dark_regions']:
        mask = _raster(item, shape, input_shape) & old_dark
        median = float(np.median(work[mask])) if mask.any() else 255.
        if item['touch_image_border']:
            edge_evidence.append((item, mask))
            suppressed.append(dict(source_ids=[item['id']], reason='edge_shading_not_a_separate_colony'))
            continue
        if item['area_mm2'] < cfg.dark_min_area_mm2 or median > cfg.dark_median_limit:
            suppressed.append(dict(source_ids=[item['id']], reason='small_or_shallow_dark_spot', median_gray=median))
            continue
        exclusion |= cv2.dilate(mask.astype(np.uint8), _kernel(cfg.dark_margin_um, pitch_um)) > 0
        dark_records.append((item, mask))

    # Only a connected, low-contrast edge region already associated with an
    # ambiguous colony may extend its BODY. It never extends its usable texture.
    bodies = []
    for item in base['components']:
        mask = raw_labels == int(item['id'][1:])
        if not mask.any():
            continue
        if float((mask & exclusion).sum()) / max(1, int(mask.sum())) > .7:
            suppressed.append(dict(source_ids=[item['id']], reason='duplicate_of_dark_exclusion'))
            continue
        body = _body(mask)
        sources = [item['id']]
        if item['status'] == 'dark_edge_review':
            for dark_item, dark_mask in edge_evidence:
                overlap = float((dark_mask & body).sum()) / max(1, int(dark_mask.sum()))
                if overlap >= .20:
                    body |= dark_mask
                    sources.append(dark_item['id'])
        bodies.append(dict(items=[item], sources=sources, mask=mask, body=body))

    # Join separated small pieces only through actual weak texture support.
    # Watershed instances that already shared a boundary are not blindly joined.
    parent = list(range(len(bodies)))
    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]; i = parent[i]
        return i
    for i, a in enumerate(bodies):
        if a['items'][0]['area_mm2'] > cfg.fragment_max_area_mm2:
            continue
        dist = cv2.distanceTransform((~a['mask']).astype(np.uint8), cv2.DIST_L2, 5)
        for j in range(i+1, len(bodies)):
            b = bodies[j]
            if b['items'][0]['area_mm2'] > cfg.fragment_max_area_mm2:
                continue
            gap_um = float(dist[b['mask']].min()) * pitch_um
            adjacent_sparse_interior = (not a['items'][0]['touch_image_border']
                                       and not b['items'][0]['touch_image_border']
                                       and max(a['items'][0]['area_mm2'],b['items'][0]['area_mm2'])<.12)
            if not (gap_um <= cfg.fragment_merge_gap_um and (gap_um>4*pitch_um or adjacent_sparse_interior)):
                continue
            joint = a['body'] | b['body']
            closed = cv2.morphologyEx(joint.astype(np.uint8), cv2.MORPH_CLOSE,
                                     _kernel(cfg.fragment_merge_gap_um/2, pitch_um)) > 0
            bridge = closed & ~joint
            if not bridge.any() or float((occupancy[bridge] >= cfg.fragment_bridge_occupancy).mean()) < .70:
                continue
            n, _ = cv2.connectedComponents(closed.astype(np.uint8))
            if n != 2:
                continue
            parent[root(j)] = root(i)
    groups = {}
    for i, body in enumerate(bodies):
        group = groups.setdefault(root(i), dict(items=[], sources=[], mask=np.zeros(shape,bool), body=np.zeros(shape,bool)))
        group['items'] += body['items']; group['sources'] += body['sources']
        group['mask'] |= body['mask']; group['body'] |= body['body']
    retained = []
    for group in groups.values():
        anchor_radius = max(item['seed_radius_um'] for item in group['items'])
        area = float(group['body'].sum())*pixel_area
        if (all(item['touch_image_border'] for item in group['items'])
                and anchor_radius < cfg.edge_min_seed_radius_um
                and max(item['area_mm2'] for item in group['items']) < cfg.fragment_max_area_mm2):
            suppressed.append(dict(source_ids=group['sources'], reason='weak_truncated_texture_fragment', anchor_seed_radius_um=anchor_radius))
            continue
        retained.append(group)

    # A bounded hysteresis refinement repairs the high-threshold jagged outline.
    # Protect other accepted seeds and existing basin assignments from overgrowth.
    boundary_support = (occupancy >= cfg.boundary_occupancy) & ~low
    output_labels = np.zeros(shape,np.int32)
    components = []
    accepted_mask = np.zeros(shape,np.uint8)
    safe_labels = np.zeros(shape,np.int32)
    fine = abs(work-cv2.GaussianBlur(work,(0,0),max(.6,1.89/pitch_um)))
    broad = abs(work-cv2.GaussianBlur(work,(0,0),max(1.,25.16/pitch_um)))
    for group in retained:
        old = group['mask']; body = group['body']
        source_numbers = [int(item['id'][1:]) for item in group['items']]
        own = np.isin(raw_labels, source_numbers)
        dist = cv2.distanceTransform((~body).astype(np.uint8),cv2.DIST_L2,5)*pitch_um
        was_normal = all(c['status']=='normal_candidate' for c in group['items'])
        needs_boundary_repair = any(c['status'] in {'texture_review','dark_edge_review'} for c in group['items'])
        growth = cfg.boundary_growth_um if needs_boundary_repair else cfg.normal_boundary_growth_um
        closing = cfg.boundary_close_um if needs_boundary_repair else cfg.normal_boundary_close_um
        domain = (boundary_support & (dist <= growth) & ((raw_labels==0)|own)) | body
        closed = cv2.morphologyEx(domain.astype(np.uint8),cv2.MORPH_CLOSE,_kernel(closing,pitch_um)) > 0
        closed &= ((raw_labels==0)|own) | body
        n, labels = cv2.connectedComponents(closed.astype(np.uint8))
        touching = np.unique(labels[old]); touching = touching[touching>0]
        refined = _body(np.isin(labels,touching))
        # Refined shapes must form one body after a proposed merge.
        contours,_ = cv2.findContours(refined.astype(np.uint8),cv2.RETR_EXTERNAL,cv2.CHAIN_APPROX_SIMPLE)
        if len(contours)>1:
            refined = _body(cv2.morphologyEx(refined.astype(np.uint8),cv2.MORPH_CLOSE,
                                            _kernel(cfg.fragment_merge_gap_um/2,pitch_um))>0)
        refined &= (output_labels == 0)
        if not refined.any():
            continue
        geo = _geometry(refined,input_shape,pixel_area)
        safe = refined & boundary_support & ~exclusion & ~old_dark
        clearance = _distance(safe)
        yy,xx = np.unravel_index(int(np.argmax(clearance)),shape)
        safe_radius = float(clearance[yy,xx])*pitch_um
        safe_area = float(safe.sum())*pixel_area
        fine_ratio = float(fine[old & ~exclusion].mean()/max(1e-3,float(broad[old & ~exclusion].mean()))) if (old & ~exclusion).any() else 0.
        owned_dark=[]
        near = cv2.dilate(old.astype(np.uint8),np.ones((5,5),np.uint8))>0
        for item, dark_mask in dark_records:
            ring=(cv2.dilate(dark_mask.astype(np.uint8),np.ones((5,5),np.uint8))>0)&~dark_mask
            if np.count_nonzero(dark_mask&refined)>.5*np.count_nonzero(dark_mask) or np.count_nonzero(ring&near)>.25*max(1,np.count_nonzero(ring)):
                owned_dark.append(item['id'])
        rim_ok = (safe_radius>=cfg.rim_min_safe_radius_um and safe_area>=cfg.rim_min_area_mm2 and fine_ratio>=cfg.rim_min_fine_ratio)
        edge_recovered = (any(c['status']=='dark_edge_review' for c in group['items'])
                          and fine_ratio>=cfg.rim_min_fine_ratio and geo['solidity']>=.72
                          and safe_radius>=cfg.edge_min_seed_radius_um)
        status = ('overgrown' if owned_dark and not rim_ok else
                  'normal_candidate' if (owned_dark and rim_ok) or was_normal or edge_recovered
                  else 'texture_review')
        reasons=[]
        if status=='normal_candidate' and (safe_radius<65. or safe_area<cfg.rim_min_area_mm2):
            status='texture_review'; reasons.append('insufficient_usable_texture')
        if owned_dark: reasons.append('usable_rim_with_excluded_dark_core' if rim_ok else 'overgrown_body')
        if len(group['items'])>1: reasons.append('merged_texture_fragments')
        if any(s.startswith('D') for s in group['sources']): reasons.append('edge_shading_merged_into_body_only')
        if geo['touch_image_border']: reasons.append('image_edge_clipped')
        number=len(components)+1
        c=dict(id=f'C{number:02d}',source_ids=group['sources'],**geo,status=status,
               is_pickable=False,is_valid_for_compensation=False,requires_human_validation=True,
               center_pixel=[round((xx+.5)*iw/shape[1]-.5),round((yy+.5)*ih/shape[0]-.5)] if safe.any() else None,
               center_is_valid=bool(safe.any()), safe_area_mm2=safe_area,safe_radius_um=safe_radius,
               fine_texture_ratio=fine_ratio,excluded_dark_source_ids=owned_dark,
               seed_radius_um=max(v['seed_radius_um'] for v in group['items']),
               review_reasons=reasons, size_is_lower_bound=geo['touch_image_border'])
        components.append(c);output_labels[refined]=number
        if status=='normal_candidate':
            accepted_mask[safe]=255;safe_labels[safe]=number
    dark_components=[]
    for old_item, mask in dark_records:
        dark_components.append(dict(id=f'D{len(dark_components)+1:02d}',source_ids=[old_item['id']],
                                    **_geometry(mask,input_shape,pixel_area),status='overgrown',
                                    is_pickable=False,is_valid_for_compensation=False,requires_human_validation=True))
    result=dict(algorithm='experimental_feedback_rules_v2',experimental=True,production_enabled=False,
                input_size=base['input_size'],fov_mm=base['fov_mm'],working_grid=base['working_grid'],
                config=asdict(cfg),base_config=base['config'],components=components,component_count=len(components),
                dark_regions=dark_components,suppressed_candidates=suppressed,
                limitations=['Offline hypothesis tuned on seven reviewed frames; no independent accuracy estimate.',
                             'No cross-image changes; dark masks are exclusions, not additional colony counts.',
                             'Boundary changes need human contour review; no manual target polygons were supplied.'])
    return result,dict(gray=maps['gray'],occupancy=occupancy,proposal_labels=output_labels,
                       accepted_mask=accepted_mask,safe_labels=safe_labels,exclusion_mask=exclusion.astype(np.uint8)*255,
                       boundary_support=boundary_support.astype(np.uint8)*255)


def analyze(gray, *, fov_mm, config=None):
    base,maps=segment_single_image(gray,fov_mm=fov_mm)
    return refine_evidence(base,maps,config=config)

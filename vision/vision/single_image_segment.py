"""Single-frame segmentation promoted from the accepted 2026-09-24 experiment.

Keep the frozen experiment independent for acceptance comparisons. The pipeline
adapter authorizes only normal_candidate records; this module produces evidence.
"""
from dataclasses import asdict, dataclass
import math

import cv2
import numpy as np


@dataclass(frozen=True)
class SingleImageConfig:
    working_um_per_pixel: float = 3.14453125
    activity_relative_floor: float = .018
    activity_absolute_floor: float = 2.5
    texture_sigma_um: float = 3.46
    density_window_um: float = 78.6
    support_occupancy: float = .64
    seed_occupancy: float = .78
    seed_radius_um: float = 25.0
    instance_core_radius_um: float = 65.0
    min_instance_core_mm2: float = .008
    opening_um: float = 22.0
    min_area_mm2: float = .06
    normal_min_area_mm2: float = .12
    normal_min_solidity: float = .72
    dark_background_window_um: float = 800.0
    dark_relative_deficit: float = .35
    dark_mean_limit: float = 115.0
    min_dark_area_mm2: float = .012
    large_dark_area_mm2: float = .035
    dark_reject_fraction: float = .20
    low_visibility_mean: float = 25.0


def _odd(microns, mpp):
    return max(3, int(round(microns / (mpp * 1000))) // 2 * 2 + 1)


def _regions(mask):
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask.astype(np.uint8), 8)
    return labels, [(i, stats[i]) for i in range(1, n)]


def _distance(mask):
    return cv2.distanceTransform(np.pad(mask.astype(np.uint8), 1), cv2.DIST_L2, 5)[1:-1, 1:-1]


def segment_single_image(gray, *, fov_mm, config=None):
    """Return proposals in input coordinates plus inspectable working-grid maps.

    Explicit FOV is required. Input must be a complete calibrated field; cropped
    images need their own FOV. Physical thresholds use the actual working grid.
    """
    cfg = config or SingleImageConfig()
    if gray.ndim != 2 or gray.dtype != np.uint8 or not gray.size:
        raise ValueError('gray must be a nonempty uint8 grayscale image')
    fw, fh = map(float, fov_mm)
    if not all(math.isfinite(v) and v > 0 for v in (fw, fh)):
        raise ValueError('fov_mm must contain positive finite width and height')
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool)
               and math.isfinite(v) and v > 0 for v in asdict(cfg).values()):
        raise ValueError('single_image_config parameters must be positive and finite')
    if not 0 < cfg.support_occupancy < cfg.seed_occupancy < 1:
        raise ValueError('occupancy thresholds must satisfy 0 < support < seed < 1')
    if any(v > 1 for v in (cfg.normal_min_solidity, cfg.dark_relative_deficit, cfg.dark_reject_fraction)):
        raise ValueError('solidity and dark fractions must not exceed 1')
    if cfg.min_dark_area_mm2 > cfg.large_dark_area_mm2:
        raise ValueError('min_dark_area_mm2 must not exceed large_dark_area_mm2')
    ih, iw = gray.shape
    # Use a nearly isotropic physical grid, never upsample to invent texture.
    pitch = max(cfg.working_um_per_pixel / 1000, fw / iw, fh / ih)
    ww, wh = max(1, round(fw / pitch)), max(1, round(fh / pitch))
    work = cv2.resize(gray, (ww, wh), interpolation=cv2.INTER_AREA).astype(np.float32)
    mx, my = fw / ww, fh / wh
    mpp = math.sqrt(mx * my)
    pixel_area = mx * my
    mean = cv2.GaussianBlur(work, (0, 0), max(1., 9.4 / (mpp * 1000)))
    highpass = np.abs(work - cv2.GaussianBlur(work, (0, 0), cfg.texture_sigma_um / (mpp * 1000)))
    active = highpass > np.maximum(cfg.activity_absolute_floor, mean * cfg.activity_relative_floor)
    win = _odd(cfg.density_window_um, mpp)
    occupancy = cv2.boxFilter(active.astype(np.float32), -1, (win, win))

    # A local upper envelope detects compact dark deficits while preserving broad
    # edge shading. It is NOT a calibrated flat-field or confirmed well-wall mask.
    dark_win = _odd(cfg.dark_background_window_um, mpp)
    background = cv2.morphologyEx(mean, cv2.MORPH_CLOSE,
                                 cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (dark_win, dark_win)))
    deficit = np.clip((background - mean) / np.maximum(background, 25.), 0, 1)
    low_visibility = mean < cfg.low_visibility_mean
    dark = ((deficit > cfg.dark_relative_deficit) & (mean < cfg.dark_mean_limit)
            & ~low_visibility).astype(np.uint8)
    dark = cv2.morphologyEx(dark, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    dl, dark_regions = _regions(dark)
    dark_mask = np.zeros_like(dark, dtype=bool)
    dark_records = []
    for label, stat in dark_regions:
        area_mm2 = float(stat[4]) * pixel_area
        if area_mm2 < cfg.min_dark_area_mm2:
            continue
        mask = dl == label
        dark_mask |= mask
        border = bool(np.any(mask[0]) or np.any(mask[-1]) or np.any(mask[:,0]) or np.any(mask[:,-1]))
        dark_records.append((mask, area_mm2, border))

    support = (occupancy >= cfg.support_occupancy) & ~low_visibility
    # Do not fill dark centers as normal foreground, but keep the uncut support
    # for determining whether a large dark region belongs to the same body.
    kernel_size = _odd(cfg.opening_um, mpp)
    support = cv2.morphologyEx(support.astype(np.uint8), cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel_size, kernel_size))) > 0
    seed = (occupancy >= cfg.seed_occupancy) & support & ~dark_mask
    seed_distance = _distance(seed)
    # Stable cores split texture bridges; watershed grows on this same evidence
    # grid rather than re-estimating background inside every ROI.
    dist = _distance(support)
    core_labels, cores = _regions((dist * mpp * 1000 >= cfg.instance_core_radius_um) & ~dark_mask)
    markers = np.zeros_like(core_labels)
    marker_id = 0
    for label, stat in cores:
        core = core_labels == label
        if stat[4] * pixel_area < cfg.min_instance_core_mm2 or not np.any(seed[core]):
            continue
        marker_id += 1
        markers[core] = marker_id
    for dark_region, area, border in dark_records:
        if area >= cfg.large_dark_area_mm2 and not border:
            marker_id += 1
            markers[dark_region] = marker_id
    if marker_id:
        domain = support | (dark_mask & ~low_visibility)
        flood_markers = markers + 1
        flood_markers[domain & (markers == 0)] = 0
        landscape = np.uint8(np.clip(occupancy, 0, 1)*255)
        landscape = cv2.GaussianBlur(landscape, (5, 5), 0)
        landscape = cv2.copyMakeBorder(landscape, 1, 1, 1, 1, cv2.BORDER_REPLICATE)
        flood_markers = np.pad(flood_markers, 1, constant_values=1)
        cv2.watershed(cv2.cvtColor(landscape, cv2.COLOR_GRAY2BGR), flood_markers)
        flood_markers = flood_markers[1:-1, 1:-1]
        labels = np.where(domain & (flood_markers > 1), flood_markers-1, 0).astype(np.int32)
    else:
        labels = np.zeros_like(markers)
    regions = [(label, None) for label in np.unique(labels) if label]
    components = []
    accepted_mask = np.zeros_like(support, dtype=np.uint8)
    proposal_labels = np.zeros_like(labels)
    rejected = []

    def geometry(mask):
        contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        contour = max(contours, key=cv2.contourArea)
        x, y, w, h = cv2.boundingRect(contour)
        mapped = np.rint((contour[:, 0].astype(np.float64) + .5) * [iw/ww, ih/wh] - .5).astype(int)
        mapped[:, 0] = np.clip(mapped[:, 0], 0, iw-1)
        mapped[:, 1] = np.clip(mapped[:, 1], 0, ih-1)
        return dict(contour_points=mapped.tolist(), bbox=[round(x*iw/ww), round(y*ih/wh), round(w*iw/ww), round(h*ih/wh)],
                    touch_image_border=bool(x==0 or y==0 or x+w==ww or y+h==wh))

    for label, stat in regions:
        mask = labels == label
        area = float(np.count_nonzero(mask)) * pixel_area
        core_radius = float(seed_distance[mask].max()) * mpp * 1000
        if area < cfg.min_area_mm2 or core_radius < cfg.seed_radius_um:
            rejected.append(dict(area_mm2=area, seed_radius_um=core_radius,
                                 reason='small_area' if area < cfg.min_area_mm2 else 'no_sustained_texture_core'))
            continue
        geo = geometry(mask)
        # Fill only for ownership/measurement. The safe mask below retains holes.
        body = np.zeros_like(support, dtype=np.uint8)
        contours, _ = cv2.findContours(mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        cv2.drawContours(body, contours, -1, 1, -1)
        owned_dark = []
        for d, a, border in dark_records:
            if border:
                continue
            ring = (cv2.dilate(d.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0) & ~d
            enclosed = np.count_nonzero(d & (body>0)) / max(1,np.count_nonzero(d)) > .5
            # A dark core split out by watershed still belongs to its enclosing
            # textured rim; otherwise a rejected black center leaves a green ring.
            shared_boundary = np.count_nonzero(ring & mask) / max(1,np.count_nonzero(ring))
            if enclosed or shared_boundary >= .25:
                owned_dark.append((d, a))
        shadow_overlap = sum(np.count_nonzero(d & (body>0)) for d, _, border in dark_records if border)
        shadow_uncertain = (shadow_overlap / max(1,np.count_nonzero(body)) > .08 or any(
            border and a >= cfg.large_dark_area_mm2
            and np.count_nonzero(d & (body>0)) / max(1,np.count_nonzero(d)) > .5
            for d, a, border in dark_records))
        body_area = float(np.count_nonzero(body)) * pixel_area
        max_dark = max((a for _,a in owned_dark), default=0.)
        dark_fraction = float(np.count_nonzero(dark_mask & (body>0))) / max(1,np.count_nonzero(body))
        overgrown = max_dark >= cfg.large_dark_area_mm2 or dark_fraction >= cfg.dark_reject_fraction
        contour = max(contours, key=cv2.contourArea)
        solidity = cv2.contourArea(contour) / max(1., cv2.contourArea(cv2.convexHull(contour)))
        irregular = solidity < cfg.normal_min_solidity or body_area < cfg.normal_min_area_mm2
        status = ('overgrown' if overgrown else 'dark_edge_review' if shadow_uncertain
                  else 'texture_review' if irregular else 'normal_candidate')
        safe = mask & ~dark_mask & ~low_visibility
        distance = _distance(safe)
        yy, xx = np.unravel_index(int(np.argmax(distance)), distance.shape)
        item = dict(id=f'C{len(components)+1:02d}', status=status,
                    is_pickable=False, requires_human_validation=True,
                    **geo, area_mm2=body_area, equivalent_diameter_mm=math.sqrt(4*body_area/math.pi),
                    size_is_lower_bound=geo['touch_image_border'],
                    max_dark_area_mm2=max_dark, dark_fraction=dark_fraction,
                    texture_occupancy_mean=float(occupancy[mask].mean()), seed_radius_um=core_radius,
                    solidity=float(solidity),
                    center_pixel=[round((xx+.5)*iw/ww-.5),round((yy+.5)*ih/wh-.5)],
                    safe_clearance_px=float(distance[yy, xx]) * min(iw/ww, ih/wh),
                    review_reasons=['large_dark_region'] if overgrown else [])
        if geo['touch_image_border']: item['review_reasons'].append('image_edge_clipped')
        if irregular: item['review_reasons'].append('small_or_irregular_texture')
        if shadow_uncertain: item['review_reasons'].append('edge_darkness_ambiguous')
        components.append(item)
        proposal_labels[mask] = len(components)
        if status == 'normal_candidate': accepted_mask[safe] = 255

    # Explicit dark proposals survive even when texture segmentation finds none.
    dark_components = []
    for mask, area, border in dark_records:
        dark_components.append(dict(id=f'D{len(dark_components)+1:02d}', **geometry(mask),
                                    area_mm2=area, status='dark_edge_review' if border else 'overgrown' if area>=cfg.large_dark_area_mm2 else 'dark_review',
                                    is_pickable=False, requires_human_validation=True))
    return dict(algorithm='single_image_rules_v2', input_size={'width':iw,'height':ih},
                fov_mm={'width':fw,'height':fh}, config=asdict(cfg),
                component_count=len(components), components=components,
                dark_regions=dark_components, rejected_candidates=rejected,
                working_grid={'width':ww,'height':wh,'mm_per_pixel_x':mx,'mm_per_pixel_y':my},
                limitations=['FOV assumed from caller; no calibrated well geometry or flat-field.',
                             'Single-frame rule classification; no cross-image processing.']), dict(
                    gray=work.astype(np.uint8), occupancy=occupancy,
                    support=support.astype(np.uint8)*255, seed=seed.astype(np.uint8)*255,
                    dark_mask=dark_mask.astype(np.uint8)*255,
                    low_visibility=low_visibility.astype(np.uint8)*255,
                    accepted_mask=accepted_mask, proposal_labels=proposal_labels)

"""Texture instances and geometry independent of a dark colony center."""
import cv2
import numpy as np

from .gpu_ops import DEFAULT_TEXTURE_BACKEND

from .preprocess import map_bbox, map_points, resize_keep_ratio, texture_signal

MIN_RETAINED_SUPPORT_RATIO = .50


def _distance(mask):
    # Zero padding counts image/ROI edges as unsafe even for clipped instances.
    return cv2.distanceTransform(np.pad((mask > 0).astype(np.uint8), 1), cv2.DIST_L2, 5)[1:-1, 1:-1]


def safe_texture_point(mask, support, density, margin=1.0):
    """Choose an actual interior pixel; averaging nonconvex foreground is unsafe."""
    distance = _distance(cv2.bitwise_and(mask, support))
    allowed = distance >= max(1.0, float(margin))
    if not np.any(allowed):
        return None, 0.0
    max_distance = max(float(distance.max()), 1.0)
    # Distance dominates; texture breaks broad interior plateaus deterministically.
    score = distance / max_distance * .8 + density.astype(np.float32) / 255 * .2
    score[~allowed] = -1
    y, x = np.unravel_index(int(np.argmax(score)), score.shape)
    return [int(x), int(y)], float(distance[y, x])


def _quality(mask, std, noise_floor, window):
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return None
    contour = max(contours, key=cv2.contourArea)
    area = float(cv2.contourArea(contour))
    if area <= 0:
        return None
    filled = np.zeros_like(mask)
    cv2.drawContours(filled, [contour], -1, 255, -1)
    interior = _distance(filled) > window * 1.5
    if np.count_nonzero(interior) < 16:
        return None
    coverage = float(np.mean(std[interior] > noise_floor))
    # Reject a smooth disk with a high-variance outline, or a sparse debris field.
    if coverage < .55:
        return None
    hull_area = float(cv2.contourArea(cv2.convexHull(contour)))
    solidity = min(1.0, area / max(hull_area, 1.0))
    return contour, filled, coverage, solidity


def coarse_texture_rois(gray, *, work_max=1024, min_area=10000, max_keep=None,
                        max_bbox_area_ratio=.30, reject_border_touch=False,
                        border_margin=2, texture_backend=DEFAULT_TEXTURE_BACKEND, texture_noise_floor=1.5,
                        texture_window=7):
    small, scale = resize_keep_ratio(gray, work_max)
    signal = texture_signal(small, backend=texture_backend, window=texture_window,
                            noise_floor=texture_noise_floor)
    binary = signal['support']
    contours, _ = cv2.findContours(binary, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = small.shape
    candidates = []
    for contour in contours:
        x, y, bw, bh = cv2.boundingRect(contour)
        bbox_ratio = bw * bh / float(h * w)
        if cv2.contourArea(contour) < min_area or bbox_ratio > max_bbox_area_ratio:
            continue
        if min(bw, bh) / max(bw, bh) < .15:
            continue
        filled = np.zeros((bh, bw), np.uint8)
        local = contour - np.array([[[x, y]]], np.int32)
        cv2.drawContours(filled, [local], -1, 255, -1)
        quality = _quality(filled, signal['quality_std'][y:y+bh, x:x+bw], signal['noise_floor'], texture_window)
        if quality is None:
            continue
        _, filled, coverage, solidity = quality
        if solidity < .45:
            continue
        support = cv2.bitwise_and(filled, binary[y:y+bh, x:x+bw])
        if not np.any((support > 0) & (signal['seed'][y:y+bh, x:x+bw] > 0)):
            continue
        sides = [side for side, test in [('left', x <= border_margin), ('top', y <= border_margin),
                  ('right', x+bw >= w-border_margin), ('bottom', y+bh >= h-border_margin)] if test]
        if reject_border_touch and sides:
            continue
        heat = signal['density'][y:y+bh, x:x+bw]
        point, _ = safe_texture_point(filled, support, heat)
        if point is None:
            continue
        point_global = np.rint(map_points([point[0]+x, point[1]+y], small.shape, gray.shape)).astype(int).tolist()
        bbox = map_bbox([x, y, bw, bh], small.shape, gray.shape)
        confidence = float(np.clip(coverage * (.5 + .5 * solidity), 0, 1))
        candidates.append({
            'coarse_bbox': bbox, 'coarse_center_pixel': point_global, 'safe_point': point_global,
            'texture_center_pixel': point_global,
            'area_small': int(cv2.contourArea(contour)),
            'bbox_area_ratio': bbox_ratio,
            'texture_coverage': coverage, 'texture_score': confidence, 'solidity': solidity,
            'confidence': confidence, 'detection_source': 'texture',
            'touch_image_border': bool(sides), 'image_border_sides': sides,
            'image_edge_clipped': bool(sides), 'is_valid_for_compensation': confidence >= .25,
            # Private instance support, cropped in coarse coordinates. No dark-source
            # duplicate path and no bbox NMS that could delete a distinct nonconvex instance.
            '_support_small': support, '_rank': float(np.count_nonzero(support)) * confidence,
            'texture_backend': signal['backend'], 'texture_fallback_reason': signal['fallback_reason'],
        })
    candidates.sort(key=lambda c: (-c['_rank'], c['coarse_bbox'][1], c['coarse_bbox'][0]))
    if max_keep is not None and max_keep > 0:
        candidates = candidates[:max_keep]
    return candidates, {'small_gray': small, 'flat': signal['density'], 'dark_seed': signal['seed'],
                        'density_u8': signal['density'], 'binary_small': binary, 'scale': scale,
                        'density_thresh': signal['threshold'],
                        'coarse_candidate_count': len(candidates),
                        'texture_backend': signal['backend'], 'texture_fallback_reason': signal['fallback_reason']}


def refine_texture_roi(roi_gray, center_hint_local, *, max_work=1200,
                       clip_bbox_local=None, clip_pad_ratio=.05, instance_support=None,
                       texture_backend=DEFAULT_TEXTURE_BACKEND, texture_noise_floor=1.5, texture_window=7,
                       safe_margin_px=1.0, edge_refine_method='none', edge_refine_iterations=2,
                       grabcut=None):
    small, scale = resize_keep_ratio(roi_gray, max_work)
    signal = texture_signal(small, backend=texture_backend, window=texture_window,
                            noise_floor=texture_noise_floor)
    support = signal['support'].copy()
    reference = None
    if instance_support is not None and clip_bbox_local is not None:
        reference = np.zeros(small.shape, np.uint8)
        bx, by, bw, bh = map_bbox(clip_bbox_local, roi_gray.shape, small.shape)
        if bw > 0 and bh > 0:
            reference[by:by+bh, bx:bx+bw] = cv2.resize(instance_support, (bw, bh), interpolation=cv2.INTER_NEAREST)
            # Allow refinement beyond coarse evidence while excluding unrelated neighbors.
            radius = max(3, round(min(bw, bh) * .08))
            allowed = cv2.dilate(reference, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (radius*2+1, radius*2+1)))
            support = cv2.bitwise_and(support, allowed)
    hint = map_points(center_hint_local, roi_gray.shape, small.shape)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(support, 8)
    chosen, best = 0, 0
    for label in range(1, n):
        region = labels == label
        if not np.any(region & (signal['seed'] > 0)):
            continue
        if reference is not None:
            overlap = int(np.count_nonzero(region & (reference > 0)))
        else:
            hx, hy = np.rint(hint).astype(int)
            overlap = int(stats[label, cv2.CC_STAT_AREA]) if 0 <= hx < small.shape[1] and 0 <= hy < small.shape[0] and region[hy, hx] else 0
        if overlap > best:
            chosen, best = label, overlap
    debug = {'flat': signal['density'], 'dark_seed': signal['seed'], 'density': signal['density'],
             'mask_small': np.zeros_like(support), 'scale': scale, 'target': signal['threshold'],
             'center_history_small': [hint.tolist()], 'texture_backend': signal['backend'],
             'texture_fallback_reason': signal['fallback_reason'], 'failure_reason': 'no_connected_texture'}
    if not chosen:
        return None, debug
    support = (labels == chosen).astype(np.uint8) * 255
    quality = _quality(support, signal['quality_std'], signal['noise_floor'], texture_window)
    if quality is None:
        debug['failure_reason'] = 'insufficient_interior_texture'
        return None, debug
    _, mask_small, coverage, solidity = quality
    debug['mask_small'] = mask_small
    shape = (roi_gray.shape[1], roi_gray.shape[0])
    mask_full = cv2.resize(mask_small, shape, interpolation=cv2.INTER_NEAREST)
    support_full = cv2.resize(support, shape, interpolation=cv2.INTER_NEAREST)
    density_full = cv2.resize(signal['density'], shape, interpolation=cv2.INTER_LINEAR)
    allowed = np.ones_like(mask_full) * 255
    if clip_bbox_local is not None:
        bx, by, bw, bh = clip_bbox_local
        px, py = round(bw * clip_pad_ratio), round(bh * clip_pad_ratio)
        allowed[:] = 0
        allowed[max(0, by-py):min(shape[1], by+bh+py), max(0, bx-px):min(shape[0], bx+bw+px)] = 255
        mask_full = cv2.bitwise_and(mask_full, allowed)
    edge_meta = {'edge_refine_method': 'none', 'edge_refine_success': False, 'edge_refine_reason': 'disabled'}
    if edge_refine_method in {'grabcut', 'hybrid'}:
        center, _ = safe_texture_point(mask_full, support_full, density_full)
        if center is not None:
            mask_full, success, meta = grabcut(roi_gray, mask_full, center,
                allowed_mask_u8=cv2.bitwise_and(allowed, cv2.dilate(support_full, np.ones((5, 5), np.uint8))),
                iterations=min(2, max(1, int(edge_refine_iterations))))
            edge_meta = {**meta, 'edge_refine_method': 'grabcut', 'edge_refine_success': success}
    contours, _ = cv2.findContours(mask_full, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        debug['failure_reason'] = 'empty_final_contour'
        return None, debug
    contour = max(contours, key=cv2.contourArea)
    contour = cv2.approxPolyDP(contour, max(1., .0005 * cv2.arcLength(contour, True)), True)
    mask_full[:] = 0
    cv2.drawContours(mask_full, [contour], -1, 255, -1)
    point, clearance = safe_texture_point(mask_full, support_full, density_full, safe_margin_px)
    valid = point is not None
    # Compare the FINAL mask with the same coarse instance in the same pixel
    # grid. Counting pre-clipping support could authorize a collapsed fragment.
    final_small = cv2.resize(mask_full, (small.shape[1], small.shape[0]), interpolation=cv2.INTER_NEAREST)
    retained_ratio = (float(np.count_nonzero((final_small > 0) & (reference > 0)))
                      / max(1, np.count_nonzero(reference)) if reference is not None else 1.0)
    segmentation_status = 'accepted' if valid else 'no_safe_interior'
    if retained_ratio < MIN_RETAINED_SUPPORT_RATIO:
        valid = False
        segmentation_status = 'insufficient_instance_support'
    if point is None:
        point, _ = safe_texture_point(mask_full, mask_full, density_full)
    if point is None or cv2.contourArea(contour) <= 0:
        debug['failure_reason'] = 'degenerate_contour'
        return None, debug
    moments = cv2.moments(contour)
    centroid = [round(moments['m10']/moments['m00']), round(moments['m01']/moments['m00'])]
    return {'contour_local': contour[:, 0].astype(int).tolist(), 'center_local': point,
            'safe_point_local': point, 'contour_center_local': centroid,
            'bbox_local': list(cv2.boundingRect(contour)), 'area_px': int(cv2.contourArea(contour)),
            'mask_full': mask_full, 'center_history_small': debug['center_history_small'],
            'refine_method': edge_meta['edge_refine_method'], **edge_meta,
            'is_valid_for_compensation': valid, 'safe_point_method': 'texture_distance',
            'safe_clearance_px': clearance, 'safe_margin_px': float(safe_margin_px),
            'segmentation_status': segmentation_status,
            'retained_support_ratio': retained_ratio,
            'texture_coverage': coverage, 'solidity': solidity,
            'texture_score': float(coverage * (.5 + .5 * solidity)),
            'texture_backend': signal['backend'], 'texture_fallback_reason': signal['fallback_reason']}, {**debug, **edge_meta}

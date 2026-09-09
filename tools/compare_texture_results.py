"""Compare CPU/CUDA benchmark JSON, including rejected instances and safe points."""
import argparse
import json
from pathlib import Path

import cv2
import numpy as np


def compare(left, right):
    rows = []
    paths = sorted(left.glob('B3*.json'))
    if not paths or {p.name for p in paths} != {p.name for p in right.glob('B3*.json')}:
        raise ValueError('Matching nonempty B3 result sets are required')
    for path in paths:
        a = json.loads(path.read_text(encoding='utf-8'))
        b = json.loads((right / path.name).read_text(encoding='utf-8'))
        if a['component_ids'] != b['component_ids']:
            rows.append({'image': path.stem, 'pass': False, 'reason': 'instance_ids_differ'})
            continue
        for c, g in zip(a['components'], b['components']):
            row = {'image': path.stem, 'id': c['id']}
            if not c['contour_points'] or not g['contour_points']:
                equal = (c['contour_points'] == g['contour_points'] == []
                         and not c['is_pickable'] and not g['is_pickable']
                         and c['segmentation_status'] == g['segmentation_status'])
                rows.append({**row, 'both_rejected': equal, 'pass': equal})
                continue
            points = np.array(c['contour_points'] + g['contour_points'], np.int32)
            x, y, w, h = cv2.boundingRect(points)
            masks = []
            for item in (c, g):
                mask = np.zeros((h, w), np.uint8)
                cv2.fillPoly(mask, [np.array(item['contour_points'], np.int32) - [x, y]], 1)
                masks.append(mask)
            iou = float(np.count_nonzero(masks[0] & masks[1]) / max(1, np.count_nonzero(masks[0] | masks[1])))
            shift = int(np.max(np.abs(np.array(c['safe_point']) - g['safe_point'])))
            inside = all(cv2.pointPolygonTest(np.array(item['contour_points'], np.int32),
                         tuple(float(v) for v in item['safe_point']), False) >= 0 for item in (c, g))
            equal = c['is_pickable'] == g['is_pickable']
            rows.append({**row, 'iou': iou, 'max_axis_center_difference_px': shift,
                         'both_centers_inside': inside, 'pickability_equal': equal,
                         'is_pickable': c['is_pickable'],
                         'pass': iou >= .995 and shift <= 1 and inside and equal})
    return {'image_count': len(paths), 'instances': rows, 'all_pass': all(row['pass'] for row in rows)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--cpu', type=Path, required=True)
    parser.add_argument('--cuda', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    report = compare(args.cpu, args.cuda)
    args.out.write_text(json.dumps(report, indent=2), encoding='utf-8')
    print(json.dumps({'image_count': report['image_count'], 'all_pass': report['all_pass']}))
    raise SystemExit(0 if report['all_pass'] else 1)

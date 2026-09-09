"""Benchmark one frozen rule implementation in a fresh process; no hardware motion."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import statistics
import sys
import time


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--images', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--backend', choices=['baseline', 'cpu', 'cuda', 'auto'], required=True)
    parser.add_argument('--repeats', type=int, default=5)
    parser.add_argument('--write-outputs', action='store_true')
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=False)
    # Configure before importing CuPy: both NVRTC source and cache need ASCII paths
    # on affected Windows installations. Never override an existing user setting.
    if os.name == 'nt':
        tmp = args.out.resolve() / 'cuda_temp'
        if str(tmp).isascii():
            tmp.mkdir()
            os.environ['TEMP'] = str(tmp)
            os.environ['TMP'] = str(tmp)
            os.environ.setdefault('CUPY_CACHE_DIR', str(tmp / 'cache'))
    sys.path.insert(0, str(args.root.resolve()))
    import cv2
    import numpy as np
    from vision.vision.detect_pipeline import process_image
    from vision.vision.image_loader import load_gray_image

    files = sorted(args.images.glob('*.bmp'))
    if not files:
        raise ValueError('No BMP input images')
    kwargs = {} if args.backend == 'baseline' else {'texture_backend': args.backend}
    report = {'root': str(args.root.resolve()), 'backend': args.backend,
              'python': sys.version, 'opencv': cv2.__version__, 'numpy': np.__version__,
              'write_outputs': args.write_outputs, 'inputs': [
                  {'name': p.name, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()}
                  for p in files], 'rounds': []}
    start = time.perf_counter()
    process_image(files[0], **kwargs)
    report['cold_first_image_s'] = time.perf_counter() - start
    tiles = []
    for iteration in range(args.repeats):
        entries = []
        for path in files:
            out = args.out / 'images' / path.stem if args.write_outputs else None
            start = time.perf_counter()
            result = process_image(path, out_dir=out, **kwargs)
            elapsed = time.perf_counter() - start
            entries.append({'image': path.name, 'elapsed_s': elapsed,
                            'component_count': result['component_count']})
            if iteration == 0:
                (args.out / (path.stem + '.json')).write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
                gray = load_gray_image(path)
                preview = cv2.cvtColor(cv2.resize(gray, (640, 640)), cv2.COLOR_GRAY2BGR)
                for component in result['components']:
                    pts = np.asarray(component['contour_points'], np.float32).reshape(-1, 2)
                    if len(pts):
                        pts *= [640 / gray.shape[1], 640 / gray.shape[0]]
                        cv2.polylines(preview, [np.rint(pts).astype(np.int32)], True, (0, 255, 0), 2)
                    x, y = component['center_pixel']
                    cv2.circle(preview, (round(x * 640 / gray.shape[1]), round(y * 640 / gray.shape[0])), 4, (0, 0, 255), -1)
                cv2.putText(preview, path.stem.split('_row')[0], (15, 30), cv2.FONT_HERSHEY_SIMPLEX, .8, (0, 0, 255), 2)
                tiles.append(preview)
        report['rounds'].append(entries)
        print(f"round {iteration + 1}: {sum(e['elapsed_s'] for e in entries):.3f}s counts={[e['component_count'] for e in entries]}", flush=True)
    report['median_batch_s'] = statistics.median(sum(e['elapsed_s'] for e in r) for r in report['rounds'])
    report['p95_image_s'] = float(np.percentile([e['elapsed_s'] for r in report['rounds'] for e in r], 95))
    if args.backend != 'baseline':
        from vision.vision.gpu_ops import backend_status
        report['gpu'] = backend_status()
    (args.out / 'timing.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    rows = []
    for i in range(0, len(tiles), 5):
        row = tiles[i:i+5]
        row += [np.zeros_like(tiles[0])] * (5 - len(row))
        rows.append(np.hstack(row))
    cv2.imencode('.jpg', np.vstack(rows))[1].tofile(str(args.out / 'contact_sheet.jpg'))


if __name__ == '__main__':
    main()

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Iterable

import cv2
import numpy as np


def parse_float_list(text: str) -> list[float]:
    return [float(x.strip()) for x in text.split(',') if x.strip()]


def make_blob_polygon(cx: float, cy: float, r_px: float, rng: np.random.Generator, n: int = 96) -> np.ndarray:
    angles = np.linspace(0.0, 2.0 * np.pi, n, endpoint=False)
    # 不规则但连续的半径扰动，避免过于理想的圆。
    phase1 = rng.uniform(0, 2 * np.pi)
    phase2 = rng.uniform(0, 2 * np.pi)
    noise = rng.normal(0.0, 0.035, size=n)
    rr = r_px * (
        1.0
        + 0.10 * np.sin(3.0 * angles + phase1)
        + 0.06 * np.sin(7.0 * angles + phase2)
        + noise
    )
    rr = np.clip(rr, r_px * 0.78, r_px * 1.25)
    pts = np.stack([cx + rr * np.cos(angles), cy + rr * np.sin(angles)], axis=1)
    return np.round(pts).astype(np.int32)


def draw_colony_blob(img: np.ndarray, center_xy: tuple[float, float], diameter_px: float,
                     rng: np.random.Generator, gray_value: int = 80,
                     blur_sigma: float = 1.2) -> None:
    cx, cy = center_xy
    r = max(2.0, diameter_px / 2.0)
    pts = make_blob_polygon(cx, cy, r, rng)
    cv2.fillPoly(img, [pts], int(gray_value))
    if blur_sigma > 0:
        x, y, w, h = cv2.boundingRect(pts.reshape(-1, 1, 2))
        pad = int(max(8, round(4 * blur_sigma)))
        x0, y0 = max(0, x - pad), max(0, y - pad)
        x1, y1 = min(img.shape[1], x + w + pad), min(img.shape[0], y + h + pad)
        roi = img[y0:y1, x0:x1]
        img[y0:y1, x0:x1] = cv2.GaussianBlur(roi, (0, 0), blur_sigma)


def add_low_frequency_background(img: np.ndarray) -> np.ndarray:
    h, w = img.shape
    gx = np.linspace(-8, 8, w, dtype=np.float32)
    gy = np.linspace(-6, 6, h, dtype=np.float32)[:, None]
    out = img.astype(np.float32) + gx + gy
    return np.clip(out, 0, 255).astype(np.uint8)


def make_camera_sim(out_dir: Path, width_px: int, height_px: int, fov_mm: float,
                    diameters_mm: Iterable[float], seed: int, camera_targets: str = 'single') -> dict:
    rng = np.random.default_rng(seed)
    img = np.full((height_px, width_px), 215, dtype=np.uint8)
    img = add_low_frequency_background(img)

    px_per_mm = width_px / float(fov_mm)
    # 默认只放 1 个目标，适合补偿闭环；需要多目标时用 --camera_targets all。
    positions_norm = [(0.34, 0.58), (0.68, 0.30), (0.74, 0.74), (0.25, 0.28)]
    diameters = list(diameters_mm)
    if camera_targets == 'single':
        diameters = [diameters[min(1, len(diameters) - 1)]]  # 默认取 0.6 mm；若只有一个值则取它。
        positions_norm = [(0.34, 0.58)]
    records = []
    for i, (dia_mm, pos) in enumerate(zip(diameters, positions_norm), start=1):
        cx = pos[0] * width_px
        cy = pos[1] * height_px
        dia_px = dia_mm * px_per_mm
        draw_colony_blob(
            img,
            (cx, cy),
            dia_px,
            rng,
            gray_value=int(rng.integers(65, 105)),
            blur_sigma=max(1.5, dia_px * 0.006),
        )
        records.append({
            'id': f'T{i:02d}',
            'center_pixel_design': [round(float(cx), 2), round(float(cy), 2)],
            'diameter_mm': float(dia_mm),
            'diameter_px_design': round(float(dia_px), 2),
        })

    noise = rng.normal(0, 2.0, img.shape).astype(np.int16)
    img = np.clip(img.astype(np.int16) + noise, 0, 255).astype(np.uint8)

    out_path = out_dir / f'target_camera_sim_{width_px}x{height_px}.bmp'
    cv2.imwrite(str(out_path), img)
    return {
        'camera_sim_path': str(out_path),
        'width_px': width_px,
        'height_px': height_px,
        'fov_mm': fov_mm,
        'px_per_mm': px_per_mm,
        'targets': records,
    }


def make_print_sheet(out_dir: Path, sheet_width_mm: float, sheet_height_mm: float, dpi: int,
                     diameters_mm: Iterable[float], seed: int) -> dict:
    rng = np.random.default_rng(seed + 100)
    px_per_mm = dpi / 25.4
    w = int(round(sheet_width_mm * px_per_mm))
    h = int(round(sheet_height_mm * px_per_mm))
    img = np.full((h, w), 255, dtype=np.uint8)

    diameters = list(diameters_mm)
    # 靶标点阵：间距足够大，避免同一视野内过多目标互相干扰。
    margin_mm = 8.0
    step_x_mm = max(8.0, (sheet_width_mm - 2 * margin_mm) / max(1, len(diameters) - 1))
    y_rows_mm = [sheet_height_mm * 0.35, sheet_height_mm * 0.65]
    records = []
    idx = 1
    for row, y_mm in enumerate(y_rows_mm):
        for col, dia_mm in enumerate(diameters):
            x_mm = margin_mm + col * step_x_mm
            # 第二行错开一点，方便采到不同偏心位置。
            if row == 1:
                x_mm = min(sheet_width_mm - margin_mm, x_mm + step_x_mm * 0.35)
            cx = x_mm * px_per_mm
            cy = y_mm * px_per_mm
            dia_px = dia_mm * px_per_mm
            draw_colony_blob(
                img,
                (cx, cy),
                dia_px,
                rng,
                gray_value=35,
                blur_sigma=max(0.6, dia_px * 0.03),
            )
            records.append({
                'id': f'P{idx:02d}',
                'center_mm_design': [round(float(x_mm), 3), round(float(y_mm), 3)],
                'diameter_mm': float(dia_mm),
            })
            idx += 1

    out_path = out_dir / f'target_print_sheet_{int(sheet_width_mm)}x{int(sheet_height_mm)}mm_{dpi}dpi.png'
    cv2.imwrite(str(out_path), img)
    return {
        'print_sheet_path': str(out_path),
        'sheet_width_mm': sheet_width_mm,
        'sheet_height_mm': sheet_height_mm,
        'dpi': dpi,
        'px_per_mm': px_per_mm,
        'targets': records,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description='Generate colony-like target images for vision/compensation chain tests.')
    parser.add_argument('--out_dir', default='C:/colony_system/data/target_images', help='output directory')
    parser.add_argument('--mode', choices=['all', 'camera_sim', 'print_sheet'], default='all')
    parser.add_argument('--width_px', type=int, default=5120)
    parser.add_argument('--height_px', type=int, default=5120)
    parser.add_argument('--fov_mm', type=float, default=3.0, help='camera field of view width in mm, e.g. 3.0 for 4x')
    parser.add_argument('--dpi', type=int, default=1200, help='print resolution; print the PNG at 100 percent scale')
    parser.add_argument('--sheet_width_mm', type=float, default=60.0)
    parser.add_argument('--sheet_height_mm', type=float, default=40.0)
    parser.add_argument('--diameters_mm', default='0.4,0.6,0.8,1.0', help='comma-separated target diameters in mm')
    parser.add_argument('--seed', type=int, default=7)
    parser.add_argument('--camera_targets', choices=['single', 'all'], default='single', help='camera_sim target count')
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    diameters_mm = parse_float_list(args.diameters_mm)

    meta = {'outputs': []}
    if args.mode in ('all', 'camera_sim'):
        meta['outputs'].append(make_camera_sim(out_dir, args.width_px, args.height_px, args.fov_mm, diameters_mm, args.seed, args.camera_targets))
    if args.mode in ('all', 'print_sheet'):
        meta['outputs'].append(make_print_sheet(out_dir, args.sheet_width_mm, args.sheet_height_mm, args.dpi, diameters_mm, args.seed))

    meta_path = out_dir / 'target_metadata.json'
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()

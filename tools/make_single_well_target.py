from __future__ import annotations

import argparse
import json
import math
import random
from pathlib import Path
from typing import List, Tuple

import numpy as np
from PIL import Image, ImageDraw, ImageFilter


def mm_to_px(mm: float, dpi: int) -> int:
    return int(round(mm / 25.4 * dpi))


def irregular_blob_points(
    center_px: Tuple[float, float],
    radius_px: float,
    *,
    n: int = 96,
    jitter: float = 0.22,
    seed: int = 1,
) -> List[Tuple[float, float]]:
    rng = random.Random(seed)
    points: List[Tuple[float, float]] = []
    cx, cy = center_px
    phase1 = rng.random() * math.pi * 2
    phase2 = rng.random() * math.pi * 2

    for i in range(n):
        a = 2.0 * math.pi * i / n
        smooth = 0.10 * math.sin(3 * a + phase1) + 0.07 * math.sin(7 * a + phase2)
        rand = rng.uniform(-jitter, jitter) * 0.35
        r = radius_px * max(0.65, 1.0 + smooth + rand)
        points.append((cx + r * math.cos(a), cy + r * math.sin(a)))
    return points


def draw_blob(draw: ImageDraw.ImageDraw, center_px: Tuple[float, float], diameter_px: float, *, fill, seed: int) -> None:
    pts = irregular_blob_points(center_px, diameter_px / 2.0, seed=seed)
    draw.polygon(pts, fill=fill)


def make_print_target(
    out_path: Path,
    *,
    sheet_mm: float,
    well_diameter_mm: float,
    clone_diameter_mm: float,
    clone_offset_x_mm: float,
    clone_offset_y_mm: float,
    dpi: int,
    transparent_outside: bool,
    seed: int,
) -> dict:
    sheet_px = mm_to_px(sheet_mm, dpi)
    well_d_px = mm_to_px(well_diameter_mm, dpi)
    clone_d_px = mm_to_px(clone_diameter_mm, dpi)

    mode = "RGBA" if transparent_outside else "RGB"
    bg = (255, 255, 255, 0) if transparent_outside else (255, 255, 255)
    image = Image.new(mode, (sheet_px, sheet_px), bg)
    draw = ImageDraw.Draw(image)

    cx = cy = sheet_px / 2.0
    well_r = well_d_px / 2.0

    if transparent_outside:
        well_fill = (245, 245, 245, 35)
        border = (170, 170, 170, 160)
        clone_fill = (25, 25, 25, 255)
    else:
        well_fill = (245, 245, 245)
        border = (175, 175, 175)
        clone_fill = (25, 25, 25)

    # 圆形孔底区域：边界必须浅，避免识别算法把孔边误识别为克隆。
    draw.ellipse(
        [cx - well_r, cy - well_r, cx + well_r, cy + well_r],
        fill=well_fill,
        outline=border,
        width=max(1, mm_to_px(0.10, dpi)),
    )

    clone_cx = cx + mm_to_px(clone_offset_x_mm, dpi)
    clone_cy = cy + mm_to_px(clone_offset_y_mm, dpi)

    # 深色不规则实心斑块：这是当前 OpenCV 代码真正要识别的目标。
    draw_blob(draw, (clone_cx, clone_cy), clone_d_px, fill=clone_fill, seed=seed)

    # 轻微模糊，让边缘更接近真实成像，不要过于理想。
    image = image.filter(ImageFilter.GaussianBlur(radius=max(0.1, mm_to_px(0.01, dpi))))

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)

    return {
        "print_target": str(out_path),
        "sheet_mm": sheet_mm,
        "dpi": dpi,
        "image_px": [sheet_px, sheet_px],
        "well_diameter_mm": well_diameter_mm,
        "clone_diameter_mm": clone_diameter_mm,
        "clone_offset_from_well_center_mm": [clone_offset_x_mm, clone_offset_y_mm],
        "transparent_outside": transparent_outside,
    }


def make_camera_sim(
    out_path: Path,
    *,
    image_size: int,
    fov_mm: float,
    clone_diameter_mm: float,
    clone_offset_x_mm: float,
    clone_offset_y_mm: float,
    seed: int,
    add_noise: bool,
) -> dict:
    mm_per_px = fov_mm / image_size

    image = Image.new("L", (image_size, image_size), 232)
    cx = (image_size - 1) / 2.0
    cy = (image_size - 1) / 2.0

    # 模拟局部视野轻微背景不均匀。
    yy, xx = np.mgrid[0:image_size, 0:image_size]
    vignette = 8 * (((xx - cx) / image_size) ** 2 + ((yy - cy) / image_size) ** 2)
    arr = np.array(image, dtype=np.float32) - vignette
    arr = np.clip(arr, 0, 255).astype(np.uint8)
    image = Image.fromarray(arr, mode="L")
    draw = ImageDraw.Draw(image)

    clone_cx = cx + clone_offset_x_mm / mm_per_px
    clone_cy = cy + clone_offset_y_mm / mm_per_px
    clone_d_px = clone_diameter_mm / mm_per_px

    draw_blob(draw, (clone_cx, clone_cy), clone_d_px, fill=35, seed=seed)
    image = image.filter(ImageFilter.GaussianBlur(radius=2.0))

    if add_noise:
        arr = np.array(image, dtype=np.float32)
        rng = np.random.default_rng(seed)
        arr += rng.normal(0, 2.0, arr.shape)
        arr = np.clip(arr, 0, 255).astype(np.uint8)
        image = Image.fromarray(arr, mode="L")

    out_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(out_path)

    return {
        "camera_sim": str(out_path),
        "image_size": [image_size, image_size],
        "fov_mm": [fov_mm, fov_mm],
        "mm_per_pixel": mm_per_px,
        "image_center_pixel": [cx, cy],
        "clone_center_pixel": [clone_cx, clone_cy],
        "clone_diameter_mm": clone_diameter_mm,
        "clone_diameter_px": clone_d_px,
        "clone_offset_from_image_center_mm": [clone_offset_x_mm, clone_offset_y_mm],
        "clone_offset_from_image_center_px": [clone_offset_x_mm / mm_per_px, clone_offset_y_mm / mm_per_px],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate a single-well circular colony target.")
    parser.add_argument("--out_dir", default="data/target_images/single_well", help="输出目录")
    parser.add_argument("--well_diameter_mm", type=float, default=19.5, help="单孔内径/可观察孔底直径，默认 19.5 mm")
    parser.add_argument("--sheet_mm", type=float, default=30.0, help="打印图画布边长，默认 30 mm")
    parser.add_argument("--dpi", type=int, default=1200, help="打印图 DPI，默认 1200")
    parser.add_argument("--fov_mm", type=float, default=3.0, help="相机局部视野边长，默认 3 mm")
    parser.add_argument("--image_size", type=int, default=5120, help="相机模拟图尺寸，默认 5120")
    parser.add_argument("--clone_diameter_mm", type=float, default=0.8, help="模拟克隆直径，默认 0.8 mm")
    parser.add_argument("--clone_offset_x_mm", type=float, default=0.70, help="克隆相对中心 x 偏移，向右为正，默认 0.70 mm")
    parser.add_argument("--clone_offset_y_mm", type=float, default=0.45, help="克隆相对中心 y 偏移，向下为正，默认 0.45 mm")
    parser.add_argument("--seed", type=int, default=7, help="随机种子，用于生成稳定的不规则斑块")
    parser.add_argument("--transparent_outside", action="store_true", help="打印图圆孔外部透明")
    parser.add_argument("--no_noise", action="store_true", help="相机模拟图不加噪声")
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    print_path = out_dir / "single_well_target_print.png"
    camera_path = out_dir / "single_well_camera_sim_5120.bmp"
    metadata_path = out_dir / "single_well_target_metadata.json"

    meta = {
        "print": make_print_target(
            print_path,
            sheet_mm=args.sheet_mm,
            well_diameter_mm=args.well_diameter_mm,
            clone_diameter_mm=args.clone_diameter_mm,
            clone_offset_x_mm=args.clone_offset_x_mm,
            clone_offset_y_mm=args.clone_offset_y_mm,
            dpi=args.dpi,
            transparent_outside=args.transparent_outside,
            seed=args.seed,
        ),
        "camera_sim": make_camera_sim(
            camera_path,
            image_size=args.image_size,
            fov_mm=args.fov_mm,
            clone_diameter_mm=args.clone_diameter_mm,
            clone_offset_x_mm=args.clone_offset_x_mm,
            clone_offset_y_mm=args.clone_offset_y_mm,
            seed=args.seed,
            add_noise=not args.no_noise,
        ),
    }

    metadata_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

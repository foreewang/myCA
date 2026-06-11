import cv2
import json
import argparse
import numpy as np
from pathlib import Path


def to_gray_u8(img):
    if img.ndim == 3:
        if img.shape[2] == 4:
            img = img[:, :, :3]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    else:
        gray = img

    if gray.dtype == np.uint16:
        lo, hi = np.percentile(gray, (1, 99))
        hi = max(hi, lo + 1.0)
        x = np.clip((gray.astype(np.float32) - lo) / (hi - lo), 0, 1)
        gray = (x * 255).astype(np.uint8)
    elif gray.dtype != np.uint8:
        gray = cv2.normalize(gray, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    return gray


def fill_holes(binary_u8):
    h, w = binary_u8.shape
    inv = cv2.bitwise_not(binary_u8)
    mask = np.zeros((h + 2, w + 2), np.uint8)
    flood = inv.copy()
    cv2.floodFill(flood, mask, (0, 0), 255)
    holes = cv2.bitwise_not(flood)
    return cv2.bitwise_or(binary_u8, holes)


def centroid_from_mask(mask_u8):
    M = cv2.moments(mask_u8, binaryImage=True)
    if M["m00"] <= 1e-6:
        return None
    return float(M["m10"] / M["m00"]), float(M["m01"] / M["m00"])


def safe_point_from_mask(mask_u8, border_margin=10):
    m = (mask_u8 > 0).astype(np.uint8)
    if m.sum() == 0:
        return None, 0.0

    dist = cv2.distanceTransform(m, cv2.DIST_L2, 5)
    h, w = dist.shape
    bm = int(max(0, border_margin))

    if bm > 0:
        dist[:bm, :] = 0
        dist[-bm:, :] = 0
        dist[:, :bm] = 0
        dist[:, -bm:] = 0

    _, max_val, _, max_loc = cv2.minMaxLoc(dist)
    if max_val <= 0:
        c = centroid_from_mask(mask_u8)
        if c is None:
            return None, 0.0
        return (int(round(c[0])), int(round(c[1]))), 0.0

    return (int(max_loc[0]), int(max_loc[1])), float(max_val)


def segment_binary(gray_u8, bg_blur_ksize=61, coarse_blur_ksize=101, close_ksize=51):
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    g = clahe.apply(gray_u8)

    k = int(bg_blur_ksize) | 1
    bg = cv2.GaussianBlur(g, (k, k), 0)
    pre = cv2.addWeighted(g, 1.0, bg, -1.0, 0)
    pre = cv2.normalize(pre, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    k2 = int(coarse_blur_ksize) | 1
    smooth = cv2.GaussianBlur(pre, (k2, k2), 0)
    smooth = cv2.normalize(smooth, None, 0, 255, cv2.NORM_MINMAX).astype(np.uint8)

    _, bw = cv2.threshold(smooth, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)

    h, w = bw.shape

    def foreground_ratio(x):
        return float((x > 0).sum()) / float(h * w)

    r1 = foreground_ratio(bw)
    r2 = foreground_ratio(cv2.bitwise_not(bw))

    def score(r):
        pen = 0.0
        if r < 0.001:
            pen += (0.001 - r) * 5000
        if r > 0.85:
            pen += (r - 0.85) * 5000
        return -pen

    bw_use = bw if score(r1) >= score(r2) else cv2.bitwise_not(bw)

    ck = int(close_ksize) | 1
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (ck, ck))
    bw_use = cv2.morphologyEx(bw_use, cv2.MORPH_CLOSE, kernel, iterations=1)
    bw_use = fill_holes(bw_use)
    return bw_use


def extract_components(binary_u8, min_area=5000000, max_area=None):
    lab = (binary_u8 > 0).astype(np.uint8)
    n, labels, stats, _ = cv2.connectedComponentsWithStats(lab, connectivity=8)

    components = []
    for idx in range(1, n):
        area = int(stats[idx, cv2.CC_STAT_AREA])
        if area < min_area:
            continue
        if max_area is not None and area > max_area:
            continue

        comp_mask = (labels == idx).astype(np.uint8) * 255
        components.append(comp_mask)

    return components


def compute_roundness(mask_u8):
    contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    if not contours:
        return 0.0

    cnt = max(contours, key=cv2.contourArea)
    area = cv2.contourArea(cnt)
    perimeter = cv2.arcLength(cnt, True)

    if perimeter <= 1e-6:
        return 0.0

    roundness = 4.0 * np.pi * area / (perimeter * perimeter)
    return float(roundness)


def bbox_from_mask(mask_u8):
    ys, xs = np.where(mask_u8 > 0)
    if len(xs) == 0 or len(ys) == 0:
        return None
    x0, x1 = int(xs.min()), int(xs.max())
    y0, y1 = int(ys.min()), int(ys.max())
    return [x0, y0, x1 - x0 + 1, y1 - y0 + 1]


def draw_overlay(gray_u8, targets):
    vis = cv2.cvtColor(gray_u8, cv2.COLOR_GRAY2BGR)
    contour_thickness = 10
    center_radius = 26
    font_scale = 2.0
    font_thickness = 6

    for t in targets:
        mask_u8 = t["mask"]
        contours, _ = cv2.findContours(mask_u8, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        if contours:
            cv2.drawContours(vis, contours, -1, (0, 255, 0), contour_thickness, cv2.LINE_AA)

        c = t["center_pixel"]
        # sp = t["safe_point"]
        tid = t["target_id"]

        if c is not None:
            cx, cy = int(round(c[0])), int(round(c[1]))
            cv2.circle(vis, (cx, cy), center_radius, (0, 0, 255), -1, cv2.LINE_AA)
            cv2.circle(vis, (cx, cy), center_radius + 4, (255, 255, 255), 2, cv2.LINE_AA)
            text_org = (cx + 36, cy - 36)
            cv2.putText(
                vis, tid, text_org,
                cv2.FONT_HERSHEY_SIMPLEX, font_scale, (255, 255, 255), font_thickness + 3, cv2.LINE_AA
            )
            cv2.putText(
                vis, tid, text_org,
                cv2.FONT_HERSHEY_SIMPLEX, font_scale, (0, 0, 255), font_thickness, cv2.LINE_AA
            )

        # if sp is not None:
        #     cv2.circle(vis, (int(sp[0]), int(sp[1])), 12, (0, 255, 255), -1, cv2.LINE_AA)

    return vis


def build_targets(component_masks, border_margin=10):
    targets = []

    for i, mask in enumerate(component_masks, start=1):
        area = int(mask.sum() // 255)
        centroid = centroid_from_mask(mask)
        safe_point, safe_radius = safe_point_from_mask(mask, border_margin=border_margin)
        bbox = bbox_from_mask(mask)
        roundness = compute_roundness(mask)

        targets.append({
            "target_id": f"C{i:02d}",
            "mask": mask,
            "area": area,
            "center_pixel": None if centroid is None else [float(centroid[0]), float(centroid[1])],
            "safe_point": None if safe_point is None else [int(safe_point[0]), int(safe_point[1])],
            "safe_radius_px": float(safe_radius),
            "bbox": bbox,
            "roundness": roundness,
        })

    # 按面积降序排序
    targets.sort(key=lambda x: x["area"], reverse=True)

    # 重新编号和排名
    for rank, t in enumerate(targets, start=1):
        t["rank"] = rank
        t["target_id"] = f"C{rank:02d}"

    return targets


def save_result_json(json_path, image_name, image_shape, targets, overlay_path):
    h, w = image_shape[:2]

    # 图像中心（像素）
    image_center = [round((w-1) / 2.0, 3), round((h-1) / 2.0, 3)]

    # 已知视野尺寸：6.5 mm × 6.5 mm
    fov_width_mm = 6.5
    fov_height_mm = 6.5

    mm_per_pixel = {
        "x": round(fov_width_mm / float(w), 6),
        "y": round(fov_height_mm / float(h), 6)
    }

    payload = {
        "image_id": Path(image_name).stem,
        "image_name": image_name,
        "image_size": {
            "width": int(w),
            "height": int(h),
        },
        "fov_size_mm": {
            "width": fov_width_mm,
            "height": fov_height_mm
        },
        "image_center": image_center,
        "mm_per_pixel": mm_per_pixel,
        "candidate_count": len(targets),
        "overlay_path": str(overlay_path),
        "candidates": []
    }

    for t in targets:
        center_pixel = None
        offset_from_image_center_px = None
        offset_from_image_center_mm = None

        if t["center_pixel"] is not None:
            cx, cy = t["center_pixel"]
            center_pixel = [round(cx, 3), round(cy, 3)]

            dx_px = cx - image_center[0]
            dy_px = cy - image_center[1]
            offset_from_image_center_px = [
                round(dx_px, 3),
                round(dy_px, 3)
            ]

            offset_from_image_center_mm = [
                round(dx_px * mm_per_pixel["x"], 6),
                round(dy_px * mm_per_pixel["y"], 6)
            ]

        payload["candidates"].append({
            "target_id": t["target_id"],
            "rank": t["rank"],
            "area": t["area"],
            "bbox": t["bbox"],
            "roundness": round(t["roundness"], 6),
            "center_pixel": center_pixel,
            "offset_from_image_center_px": offset_from_image_center_px,
            "offset_from_image_center_mm": offset_from_image_center_mm,
            "safe_point": t["safe_point"],
            "safe_radius_px": round(t["safe_radius_px"], 3),
            "mask_path": t["mask_path"],
        })

    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inputs", nargs="+", required=True, help="输入图像路径列表")
    ap.add_argument("--out", required=True, help="输出目录")
    ap.add_argument("--border_margin", type=int, default=10, help="安全点边界留白像素")
    ap.add_argument("--min_area", type=int, default=2000000, help="最小目标面积阈值")
    ap.add_argument("--max_area", type=int, default=0, help="最大目标面积阈值，0表示不限制")
    args = ap.parse_args()

    out_dir = Path(args.out)
    masks_dir = out_dir / "masks"
    overlays_dir = out_dir / "overlays"
    json_dir = out_dir / "json"

    masks_dir.mkdir(parents=True, exist_ok=True)
    overlays_dir.mkdir(parents=True, exist_ok=True)
    json_dir.mkdir(parents=True, exist_ok=True)

    max_area = None if args.max_area <= 0 else args.max_area

    for p in args.inputs:
        p = Path(p)
        img = cv2.imread(str(p), cv2.IMREAD_UNCHANGED)
        if img is None:
            raise RuntimeError(f"Failed to read: {p}")

        gray = to_gray_u8(img)
        binary = segment_binary(gray)
        component_masks = extract_components(binary, min_area=args.min_area, max_area=max_area)
        targets = build_targets(component_masks, border_margin=args.border_margin)

        # 保存每个目标的单独 mask
        for t in targets:
            mask_path = masks_dir / f"{p.stem}_{t['target_id']}_mask.png"
            cv2.imwrite(str(mask_path), t["mask"])
            t["mask_path"] = str(mask_path)

        overlay = draw_overlay(gray, targets)
        overlay_path = overlays_dir / f"{p.stem}_overlay.png"
        cv2.imwrite(str(overlay_path), overlay)

        json_path = json_dir / f"{p.stem}_result.json"
        save_result_json(json_path, p.name, gray.shape, targets, overlay_path)

        print(f"[OK] {p.name}")
        print(f"     targets: {len(targets)}")
        print(f"     overlay: {overlay_path}")
        print(f"     json:    {json_path}")

    print("\nDone.")


if __name__ == "__main__":
    main()

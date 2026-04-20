#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
圆形区域扫描方案生成脚本
--------------------------------
功能：
1. 输入圆直径 D（mm）和扫描视野尺寸 w x h（mm）
2. 自动生成不同扫描方案的扫描中心坐标
3. 输出 CSV 坐标表
4. 绘制示意图 PNG

默认示例：
- 圆直径：20 mm
- 视野尺寸：3 mm x 3 mm

坐标系定义：
- 以圆心为原点 (0, 0)
- x 向右为正，y 向上为正
- 输出单位均为 mm

支持的方案：
- full          : 外接矩形全覆盖
- intersect     : 仅保留与圆相交的扫描窗口（推荐）
- center_inside : 仅保留中心点落在圆内的窗口
- fully_inside  : 仅保留整个窗口完全落在圆内的窗口

支持的路径排序：
- snake     : 蛇形扫描
- row_major : 按行从左到右

使用示例：
python generate_circle_scan_plan.py
python generate_circle_scan_plan.py --diameter 20 --grid-w 3 --grid-h 3
python generate_circle_scan_plan.py --diameter 20 --grid-w 3 --grid-h 3 --mode intersect
python generate_circle_scan_plan.py --diameter 20 --grid-w 3 --grid-h 3 --all-modes
"""

import argparse
import csv
import math
from pathlib import Path

import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Rectangle


def rect_intersects_circle(cx: float, cy: float, half_w: float, half_h: float, radius: float) -> bool:
    """
    判断以 (cx, cy) 为中心的矩形窗口是否与圆相交。
    圆心固定为 (0, 0)，半径为 radius。
    """
    dx = max(abs(cx) - half_w, 0.0)
    dy = max(abs(cy) - half_h, 0.0)
    return dx * dx + dy * dy <= radius * radius


def rect_fully_inside_circle(cx: float, cy: float, half_w: float, half_h: float, radius: float) -> bool:
    """
    判断矩形窗口是否完全位于圆内。
    只需检查四个角点是否都在圆内。
    """
    corners = [
        (cx - half_w, cy - half_h),
        (cx - half_w, cy + half_h),
        (cx + half_w, cy - half_h),
        (cx + half_w, cy + half_h),
    ]
    r2 = radius * radius
    return all(x * x + y * y <= r2 for x, y in corners)


def compute_axis_positions(diameter: float, window_size: float, step: float):
    """
    计算单轴上的扫描中心位置，使其在外接矩形内对称分布。
    """
    if step <= 0:
        raise ValueError("扫描步长必须大于 0")
    if window_size <= 0:
        raise ValueError("扫描窗口尺寸必须大于 0")
    if diameter <= 0:
        raise ValueError("圆直径必须大于 0")

    if diameter <= window_size:
        n = 1
    else:
        n = math.ceil((diameter - window_size) / step) + 1

    positions = [(i - (n - 1) / 2.0) * step for i in range(n)]
    return positions


def group_rows(points, tol=1e-9):
    """
    按 y 值分组，返回从上到下的行。
    """
    y_values = sorted({round(p["center_y_mm"], 9) for p in points}, reverse=True)
    rows = []
    for y in y_values:
        row = [p for p in points if abs(p["center_y_mm"] - y) <= tol]
        rows.append((y, row))
    return rows


def apply_order(points, order="snake"):
    """
    对保留下来的扫描点进行排序。
    """
    rows = group_rows(points)

    ordered = []
    for row_idx, (y, row) in enumerate(rows):
        row_sorted = sorted(row, key=lambda p: p["center_x_mm"])
        if order == "snake" and row_idx % 2 == 1:
            row_sorted.reverse()
        elif order == "row_major":
            pass
        else:
            if order not in ("snake", "row_major"):
                raise ValueError(f"未知排序方式: {order}")
        ordered.extend(row_sorted)

    for i, p in enumerate(ordered, start=1):
        p["scan_order"] = i
    return ordered


def generate_scan_plan(
    diameter: float,
    grid_w: float,
    grid_h: float,
    overlap_x: float = 0.0,
    overlap_y: float = 0.0,
    mode: str = "intersect",
    order: str = "snake",
):
    """
    生成扫描方案。
    """
    if not (0.0 <= overlap_x < 1.0 and 0.0 <= overlap_y < 1.0):
        raise ValueError("重叠率 overlap_x / overlap_y 必须满足 0 <= overlap < 1")

    radius = diameter / 2.0
    half_w = grid_w / 2.0
    half_h = grid_h / 2.0

    step_x = grid_w * (1.0 - overlap_x)
    step_y = grid_h * (1.0 - overlap_y)

    xs = compute_axis_positions(diameter, grid_w, step_x)
    ys = compute_axis_positions(diameter, grid_h, step_y)

    kept = []
    for row_index_from_top, y in enumerate(sorted(ys, reverse=True), start=1):
        for col_index_from_left, x in enumerate(sorted(xs), start=1):
            if mode == "full":
                keep = True
            elif mode == "intersect":
                keep = rect_intersects_circle(x, y, half_w, half_h, radius)
            elif mode == "center_inside":
                keep = x * x + y * y <= radius * radius
            elif mode == "fully_inside":
                keep = rect_fully_inside_circle(x, y, half_w, half_h, radius)
            else:
                raise ValueError(f"未知方案模式: {mode}")

            if keep:
                kept.append(
                    {
                        "row_index_from_top": row_index_from_top,
                        "col_index_from_left": col_index_from_left,
                        "center_x_mm": round(x, 6),
                        "center_y_mm": round(y, 6),
                        "left_mm": round(x - half_w, 6),
                        "right_mm": round(x + half_w, 6),
                        "bottom_mm": round(y - half_h, 6),
                        "top_mm": round(y + half_h, 6),
                    }
                )

    ordered = apply_order(kept, order=order)
    return {
        "diameter_mm": diameter,
        "radius_mm": radius,
        "grid_w_mm": grid_w,
        "grid_h_mm": grid_h,
        "step_x_mm": step_x,
        "step_y_mm": step_y,
        "overlap_x": overlap_x,
        "overlap_y": overlap_y,
        "mode": mode,
        "order": order,
        "count": len(ordered),
        "points": ordered,
        "candidate_x_count": len(xs),
        "candidate_y_count": len(ys),
    }


def save_csv(plan: dict, csv_path: Path):
    """
    保存坐标表为 CSV。
    """
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    fieldnames = [
        "scan_order",
        "row_index_from_top",
        "col_index_from_left",
        "center_x_mm",
        "center_y_mm",
        "left_mm",
        "right_mm",
        "bottom_mm",
        "top_mm",
    ]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in plan["points"]:
            writer.writerow({k: p[k] for k in fieldnames})


def plot_plan(plan: dict, png_path: Path, show_order: bool = True):
    """
    绘制示意图。
    """
    png_path.parent.mkdir(parents=True, exist_ok=True)

    diameter = plan["diameter_mm"]
    radius = plan["radius_mm"]
    grid_w = plan["grid_w_mm"]
    grid_h = plan["grid_h_mm"]

    fig, ax = plt.subplots(figsize=(8, 8))

    circle = Circle((0, 0), radius, fill=False, linewidth=2)
    ax.add_patch(circle)

    for p in plan["points"]:
        rect = Rectangle(
            (p["center_x_mm"] - grid_w / 2.0, p["center_y_mm"] - grid_h / 2.0),
            grid_w,
            grid_h,
            fill=False,
            linewidth=1,
        )
        ax.add_patch(rect)
        ax.plot(p["center_x_mm"], p["center_y_mm"], marker="o", markersize=3)

        if show_order:
            ax.text(
                p["center_x_mm"],
                p["center_y_mm"],
                str(p["scan_order"]),
                fontsize=7,
                ha="center",
                va="center",
            )

    pad = max(grid_w, grid_h)
    lim = radius + pad
    ax.set_xlim(-lim, lim)
    ax.set_ylim(-lim, lim)
    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("X (mm)")
    ax.set_ylabel("Y (mm)")
    ax.set_title(
        f'D={diameter} mm, grid={grid_w}x{grid_h} mm, mode={plan["mode"]}, '
        f'count={plan["count"]}, order={plan["order"]}'
    )
    ax.grid(True)
    fig.tight_layout()
    fig.savefig(png_path, dpi=200)
    plt.close(fig)


def print_summary(plan: dict):
    print("=" * 72)
    print(f'方案模式      : {plan["mode"]}')
    print(f'路径排序      : {plan["order"]}')
    print(f'圆直径        : {plan["diameter_mm"]} mm')
    print(f'扫描窗口      : {plan["grid_w_mm"]} x {plan["grid_h_mm"]} mm')
    print(f'扫描步长      : {plan["step_x_mm"]:.6f} x {plan["step_y_mm"]:.6f} mm')
    print(f'候选网格数    : {plan["candidate_x_count"]} x {plan["candidate_y_count"]}')
    print(f'保留扫描点数  : {plan["count"]}')
    print("=" * 72)


def main():
    parser = argparse.ArgumentParser(description="圆形区域扫描方案生成器（单位：mm）")
    parser.add_argument("--diameter", type=float, default=20.0, help="圆直径，默认 20 mm")
    parser.add_argument("--grid-w", type=float, default=3.0, help="扫描窗口宽度，默认 3 mm")
    parser.add_argument("--grid-h", type=float, default=3.0, help="扫描窗口高度，默认 3 mm")
    parser.add_argument("--overlap-x", type=float, default=0.0, help="x 方向重叠率，默认 0")
    parser.add_argument("--overlap-y", type=float, default=0.0, help="y 方向重叠率，默认 0")
    parser.add_argument(
        "--mode",
        type=str,
        default="intersect",
        choices=["full", "intersect", "center_inside", "fully_inside"],
        help="扫描方案模式，默认 intersect",
    )
    parser.add_argument(
        "--order",
        type=str,
        default="snake",
        choices=["snake", "row_major"],
        help="扫描顺序，默认 snake",
    )
    parser.add_argument(
        "--all-modes",
        action="store_true",
        help="一次性输出 4 种模式的结果",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="scan_plan_output",
        help="输出目录，默认 scan_plan_output",
    )
    parser.add_argument(
        "--no-order-text",
        action="store_true",
        help="示意图中不显示扫描顺序编号",
    )

    args = parser.parse_args()

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    modes = ["full", "intersect", "center_inside", "fully_inside"] if args.all_modes else [args.mode]

    for mode in modes:
        plan = generate_scan_plan(
            diameter=args.diameter,
            grid_w=args.grid_w,
            grid_h=args.grid_h,
            overlap_x=args.overlap_x,
            overlap_y=args.overlap_y,
            mode=mode,
            order=args.order,
        )

        base_name = (
            f'D{args.diameter:g}_W{args.grid_w:g}_H{args.grid_h:g}'
            f'_OX{args.overlap_x:g}_OY{args.overlap_y:g}_{mode}_{args.order}'
        )

        csv_path = output_dir / f"{base_name}.csv"
        png_path = output_dir / f"{base_name}.png"

        save_csv(plan, csv_path)
        plot_plan(plan, png_path, show_order=not args.no_order_text)
        print_summary(plan)
        print(f"CSV 已保存: {csv_path}")
        print(f"PNG 已保存: {png_path}")
        print()

    print("处理完成。")


if __name__ == "__main__":
    main()

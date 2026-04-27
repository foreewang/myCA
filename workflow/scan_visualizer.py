from __future__ import annotations

import csv
from pathlib import Path
from typing import Dict, Any, List, Optional

import matplotlib.pyplot as plt
from matplotlib.patches import Circle, Rectangle


def _ensure_parent(path: str | Path) -> Path:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def _get_points(plan: Dict[str, Any]) -> List[Dict[str, Any]]:
    points = plan.get("points", [])
    if not isinstance(points, list) or not points:
        raise ValueError("plan 中缺少有效的 points 列表")
    return points


def export_plan_points_csv(plan: Dict[str, Any], csv_path: str | Path) -> str:
    """
    导出扫描点表。

    输出字段：
    - index
    - row_index
    - col_index
    - view_down_mm
    - view_right_mm
    - stage_x_target
    - stage_y_target
    """
    points = _get_points(plan)
    out_path = _ensure_parent(csv_path)

    fieldnames = [
        "index",
        "row_index",
        "col_index",
        "view_down_mm",
        "view_right_mm",
        "stage_x_target",
        "stage_y_target",
    ]

    with out_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for p in points:
            writer.writerow(
                {
                    "index": int(p["index"]),
                    "row_index": int(p["row_index"]),
                    "col_index": int(p["col_index"]),
                    "view_down_mm": float(p["view_down_mm"]),
                    "view_right_mm": float(p["view_right_mm"]),
                    "stage_x_target": int(p["stage_x_target"]),
                    "stage_y_target": int(p["stage_y_target"]),
                }
            )

    return str(out_path)


def visualize_plan_local(
    plan: Dict[str, Any],
    png_path: str | Path,
    show_index: bool = True,
    show_rectangles: bool = True,
) -> str:
    """
    画孔内局部坐标路径图。

    坐标系与 scan_planner.py 一致：
    - 横轴: view_right_mm
    - 纵轴: view_down_mm
    - 为了和变量语义一致，图中 y 轴向下为正（会 invert_yaxis）
    - 圆心位于 (radius, 0)，因为当前 planner 的横向坐标不是圆心坐标，而是孔内左边界起算
    """
    points = _get_points(plan)
    out_path = _ensure_parent(png_path)

    reference = plan["reference"]
    scan_config = plan["scan_config"]

    well_diameter_mm = float(reference["well_diameter_mm"])
    radius = well_diameter_mm / 2.0
    fov_w = float(scan_config["fov_mm"]["width"])
    fov_h = float(scan_config["fov_mm"]["height"])

    fig, ax = plt.subplots(figsize=(9, 9))

    # 当前 planner 的局部几何坐标：
    # x = view_right_mm, 范围大致 [0, 2R]
    # y = view_down_mm, 范围大致 [-R, R]
    # 对应圆心为 (R, 0)
    circle = Circle((radius, 0.0), radius, fill=False, linewidth=2)
    ax.add_patch(circle)

    xs = []
    ys = []

    for p in points:
        x = float(p["view_right_mm"])
        y = float(p["view_down_mm"])
        xs.append(x)
        ys.append(y)

        if show_rectangles:
            rect = Rectangle(
                (x - fov_w / 2.0, y - fov_h / 2.0),
                fov_w,
                fov_h,
                fill=False,
                linewidth=0.8,
            )
            ax.add_patch(rect)

        ax.plot(x, y, marker="o", markersize=3)

        if show_index:
            ax.text(
                x,
                y,
                str(int(p["index"])),
                fontsize=7,
                ha="center",
                va="center",
            )

    # 按执行顺序连线
    ax.plot(xs, ys, linewidth=1)

    # 标注参考起点：well_start 对应 planner 中的 (view_down_mm=0, view_right_mm=0)
    ax.plot([0.0], [0.0], marker="x", markersize=8)
    ax.text(0.0, 0.0, "well_start", fontsize=8, ha="left", va="bottom")

    ax.set_aspect("equal", adjustable="box")
    ax.set_xlim(-2, well_diameter_mm + 2)
    ax.set_ylim(-(radius + 2), radius + 2)
    ax.invert_yaxis()

    ax.set_xlabel("view_right_mm")
    ax.set_ylabel("view_down_mm")
    ax.set_title(
        f"Local scan path | well={plan['well_name']} | "
        f"D={well_diameter_mm} mm | "
        f"FOV={fov_w}x{fov_h} mm | "
        f"points={len(points)}"
    )
    ax.grid(True)

    fig.tight_layout()
    fig.savefig(out_path, dpi=220)
    plt.close(fig)
    return str(out_path)


def visualize_plan_stage(
    plan: Dict[str, Any],
    png_path: str | Path,
    show_index: bool = True,
) -> str:
    """
    画位移台脉冲坐标路径图。

    横轴：
    - stage_x_target

    纵轴：
    - stage_y_target

    图中按 plan["points"] 的顺序连线，这个顺序就是 scan_executor.py 的实际执行顺序。
    """
    points = _get_points(plan)
    out_path = _ensure_parent(png_path)

    reference = plan["reference"]

    well_start_x = int(reference["well_start"]["x"])
    well_start_y = int(reference["well_start"]["y"])
    ppm = float(reference["pulses_per_mm"])
    x_sign = int(reference["x_stage_sign_for_view_down"])
    y_sign = int(reference["y_stage_sign_for_view_right"])

    xs = [int(p["stage_x_target"]) for p in points]
    ys = [int(p["stage_y_target"]) for p in points]

    fig, ax = plt.subplots(figsize=(9, 9))
    ax.plot(xs, ys, marker="o", linewidth=1)

    for p in points:
        x = int(p["stage_x_target"])
        y = int(p["stage_y_target"])
        if show_index:
            ax.text(
                x,
                y,
                str(int(p["index"])),
                fontsize=7,
                ha="center",
                va="center",
            )

    ax.plot([well_start_x], [well_start_y], marker="x", markersize=8)
    ax.text(
        well_start_x,
        well_start_y,
        "well_start",
        fontsize=8,
        ha="left",
        va="bottom",
    )

    ax.set_aspect("equal", adjustable="box")
    ax.set_xlabel("stage_x_target (pulse)")
    ax.set_ylabel("stage_y_target (pulse)")
    ax.set_title(
        f"Stage scan path | well={plan['well_name']} | "
        f"points={len(points)} | ppm={ppm} | x_sign={x_sign} | y_sign={y_sign}"
    )
    ax.grid(True)

    fig.tight_layout()
    fig.savefig(out_path, dpi=220)
    plt.close(fig)
    return str(out_path)


def export_plan_visualizations(
    plan: Dict[str, Any],
    output_dir: str | Path,
    prefix: Optional[str] = None,
    show_index: bool = True,
    show_rectangles: bool = True,
) -> Dict[str, str]:
    """
    一次性导出：
    - CSV 点表
    - 局部坐标路径图
    - 位移台脉冲路径图

    返回：
    {
        "points_csv": "...",
        "local_png": "...",
        "stage_png": "..."
    }
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if not prefix:
        prefix = f"{plan['task_id']}_{plan['well_name']}"

    points_csv = output_dir / f"{prefix}_plan_points.csv"
    local_png = output_dir / f"{prefix}_plan_local.png"
    stage_png = output_dir / f"{prefix}_plan_stage.png"

    result = {
        "points_csv": export_plan_points_csv(plan, points_csv),
        "local_png": visualize_plan_local(
            plan,
            local_png,
            show_index=show_index,
            show_rectangles=show_rectangles,
        ),
        "stage_png": visualize_plan_stage(
            plan,
            stage_png,
            show_index=show_index,
        ),
    }
    return result
"""
为单个培养孔生成扫描路径点位
"""
from __future__ import annotations

from math import sqrt
from typing import Dict, List

from workflow.plate_geometry import (
    compute_well_start,
    get_a1_start,
    get_plate_pitch_mm,
    get_pulses_per_mm,
    get_view_signs,
)


def _row_values(step_y: float, radius: float) -> List[float]:
    """
    生成扫描时各行对应的“视野向下偏移量”列表。

    参数
    ----
    step_y : float
        相邻扫描行在视野坐标系中的纵向步长，单位 mm。
    radius : float
        培养孔半径，单位 mm。

    返回
    ----
    List[float]
        行中心的纵向偏移列表，顺序为：
        [0, -step_y, +step_y, -2*step_y, +2*step_y, ...]

    说明
    ----
    这里以孔中心水平线为起点，向上下两侧对称扩展扫描行。
    这样做的目的有两个：

    1. 优先从中间区域开始覆盖，通常中部是最稳定、最有代表性的区域；
    2. 生成的行分布天然关于中心对称，便于后续圆形孔内裁剪。

    注意
    ----
    返回的是“视野向下方向”的相对偏移值，不是位移台坐标。
    后续还需要结合孔起点、坐标映射方向和脉冲换算，才能得到最终电机目标位置。
    """
    vals = [0.0]
    k = 1
    while True:
        y = round(k * step_y, 6)
        if y > radius:
            break
        vals.append(-y)
        vals.append(+y)
        k += 1
    return vals


def _x_positions_for_row(abs_vdown_mm: float, step_x: float, radius: float) -> List[float]:
    """
    计算某一扫描行内，所有有效的横向扫描位置。

    参数
    ----
    abs_vdown_mm : float
        当前扫描行相对孔中心的纵向偏移绝对值，单位 mm。
    step_x : float
        相邻扫描点在横向的步长，单位 mm。
    radius : float
        培养孔半径，单位 mm。

    返回
    ----
    List[float]
        当前行内所有横向扫描位置，表示“视野向右偏移量”，单位 mm。

    说明
    ----
    培养孔是圆形，因此某一固定纵向偏移位置上，可扫描的横向范围
    实际上是圆的一条弦。

    这里先根据圆方程计算该弦左右边界：
        x_left  = radius - half_chord
        x_right = radius + half_chord

    然后按 step_x 在该范围内布点，并强制把最右端边界也补进去，
    以尽量避免最后一段遗漏。

    注意
    ----
    这里的横向坐标仍然是“孔内相对坐标”，还不是位移台脉冲坐标。
    """
    half_chord = sqrt(max(radius * radius - abs_vdown_mm * abs_vdown_mm, 0.0))
    x_left = radius - half_chord
    x_right = radius + half_chord

    xs = [round(x_left, 6)]
    x = x_left + step_x
    while x < x_right - 1e-6:
        xs.append(round(x, 6))
        x += step_x

    if abs(xs[-1] - x_right) > 1e-6:
        xs.append(round(x_right, 6))

    return xs


def plan_single_well_scan(ctx: Dict, params: Dict) -> Dict:
    """
    为单个培养孔生成完整扫描计划。

    参数
    ----
    ctx : Dict
        运行时上下文，通常包含 plate 配置等信息。
    params : Dict
        当前采集任务参数，通常包含：
        - well_name
        - fov_mm
        - overlap
        - objective_name
        - task_id
        等字段。

    返回
    ----
    Dict
        单孔扫描计划，包含：
        - reference: 参考坐标与几何参数
        - scan_config: 本次扫描配置摘要
        - points: 每个扫描点的视野坐标与位移台目标坐标

    处理流程
    --------
    1. 根据 plate 配置和孔名，计算该孔的起始观测点；
    2. 读取脉冲换算系数和视野-位移台方向映射；
    3. 根据视野尺寸和 overlap 计算横纵向扫描步长；
    4. 先生成每一行的纵向偏移；
    5. 再为每一行生成横向扫描点；
    6. 将孔内视野偏移量转换为位移台脉冲坐标；
    7. 输出完整扫描计划。

    说明
    ----
    这个函数的职责是“规划”，不是“执行”。
    它只决定去哪些位置拍照，以及每个位置对应的目标坐标；
    真正的位移和采图由后续执行模块完成。
    """
    plate = ctx['plate']
    well_name = params['well_name']

    # 计算当前孔的观测起始点，并读取与整板相关的基础参数
    well_start = compute_well_start(plate, well_name)
    a1_start = get_a1_start(plate)
    ppm = get_pulses_per_mm(plate)
    x_sign, y_sign = get_view_signs(plate)

    # 读取当前培养板的几何参数
    well_diameter_mm = float(plate['well_diameter_mm'])
    well_gap_mm = float(plate['well_gap_mm'])
    pitch_mm = get_plate_pitch_mm(plate)

    # 读取当前任务的视野尺寸与重叠率
    fov_w = float(params['fov_mm']['width'])
    fov_h = float(params['fov_mm']['height'])
    overlap = float(params['overlap'])

    # 扫描步长 = 视野尺寸 * (1 - 重叠率)
    # overlap 越大，步长越小，扫描点越密。
    step_x = fov_w * (1.0 - overlap)
    step_y = fov_h * (1.0 - overlap)

    radius = well_diameter_mm / 2.0

    # 先确定每一行的纵向偏移，再为每一行生成横向点位
    row_vals = _row_values(step_y, radius)

    points = []
    idx = 1

    for row_index, vdown in enumerate(row_vals):
        xs = _x_positions_for_row(
            abs_vdown_mm=abs(vdown),
            step_x=step_x,
            radius=radius,
        )

        # 奇数行反向，形成蛇形扫描路径。
        # 这样可以减少行与行之间回程跳跃，提高执行效率。
        if row_index % 2 == 1:
            xs = list(reversed(xs))

        for col_index, vright in enumerate(xs):
            # 将“视野相对位移(mm)”转换为“位移台目标坐标(脉冲)”
            # 方向由 x_sign / y_sign 控制，脉冲比例由 ppm 控制。
            stage_x = int(round(well_start['x'] + x_sign * vdown * ppm))
            stage_y = int(round(well_start['y'] + y_sign * vright * ppm))

            points.append({
                'index': idx,
                'row_index': row_index,
                'col_index': col_index,
                'view_down_mm': float(vdown),
                'view_right_mm': float(vright),
                'stage_x_target': stage_x,
                'stage_y_target': stage_y,
            })
            idx += 1

    return {
        'task_id': params['task_id'],
        'task_type': params['task_type'],
        'plate_type': params['plate_type'],
        'well_name': well_name,
        'objective_name': params['objective_name'],
        'reference': {
            'meaning': f'{well_name}孔左侧观测起始点',
            'a1_start': {
                'x': int(a1_start['x']),
                'y': int(a1_start['y']),
            },
            'well_start': {
                'x': int(well_start['x']),
                'y': int(well_start['y']),
            },
            'well_diameter_mm': well_diameter_mm,
            'well_gap_mm': well_gap_mm,
            'pitch_mm': pitch_mm,
            'pulses_per_mm': ppm,
            'x_stage_sign_for_view_down': x_sign,
            'y_stage_sign_for_view_right': y_sign,
        },
        'scan_config': {
            'fov_mm': {
                'width': fov_w,
                'height': fov_h,
            },
            'overlap': overlap,
            'step_mm': {
                'width': step_x,
                'height': step_y,
            },
            'point_count': len(points),
        },
        'points': points,
    }
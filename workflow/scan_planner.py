from __future__ import annotations
from math import sqrt
from typing import Dict, List
from workflow.plate_geometry import compute_well_start, get_a1_start, get_plate_pitch_mm, get_pulses_per_mm, get_view_signs

def _row_values(step_y: float, radius: float) -> List[float]:
    vals=[0.0]; k=1
    while True:
        y=round(k*step_y,6)
        if y>radius: break
        vals.append(-y); vals.append(+y); k+=1
    return vals

def _x_positions_for_row(abs_vdown_mm: float, step_x: float, radius: float) -> List[float]:
    half_chord = sqrt(max(radius*radius - abs_vdown_mm*abs_vdown_mm, 0.0))
    x_left = radius - half_chord; x_right = radius + half_chord
    xs=[round(x_left,6)]; x=x_left+step_x
    while x < x_right - 1e-6:
        xs.append(round(x,6)); x += step_x
    if abs(xs[-1] - x_right) > 1e-6: xs.append(round(x_right,6))
    return xs

def plan_single_well_scan(ctx: Dict, params: Dict) -> Dict:
    plate=ctx['plate']; well_name=params['well_name']
    well_start=compute_well_start(plate, well_name); a1_start=get_a1_start(plate); ppm=get_pulses_per_mm(plate); x_sign, y_sign=get_view_signs(plate)
    well_diameter_mm=float(plate['well_diameter_mm']); well_gap_mm=float(plate['well_gap_mm']); pitch_mm=get_plate_pitch_mm(plate)
    fov_w=float(params['fov_mm']['width']); fov_h=float(params['fov_mm']['height']); overlap=float(params['overlap'])
    step_x=fov_w*(1.0-overlap); step_y=fov_h*(1.0-overlap); radius=well_diameter_mm/2.0
    row_vals=_row_values(step_y, radius)
    points=[]; idx=1
    for row_index, vdown in enumerate(row_vals):
        xs=_x_positions_for_row(abs_vdown_mm=abs(vdown), step_x=step_x, radius=radius)
        if row_index % 2 == 1: xs=list(reversed(xs))
        for col_index, vright in enumerate(xs):
            stage_x=int(round(well_start['x'] + x_sign*vdown*ppm)); stage_y=int(round(well_start['y'] + y_sign*vright*ppm))
            points.append({'index': idx, 'row_index': row_index, 'col_index': col_index, 'view_down_mm': float(vdown), 'view_right_mm': float(vright), 'stage_x_target': stage_x, 'stage_y_target': stage_y})
            idx += 1
    return {'task_id': params['task_id'], 'task_type': params['task_type'], 'plate_type': params['plate_type'], 'well_name': well_name, 'objective_name': params['objective_name'], 'reference': {'meaning': f'{well_name}孔左侧观测起始点', 'a1_start': {'x': int(a1_start['x']), 'y': int(a1_start['y'])}, 'well_start': {'x': int(well_start['x']), 'y': int(well_start['y'])}, 'well_diameter_mm': well_diameter_mm, 'well_gap_mm': well_gap_mm, 'pitch_mm': pitch_mm, 'pulses_per_mm': ppm, 'x_stage_sign_for_view_down': x_sign, 'y_stage_sign_for_view_right': y_sign}, 'scan_config': {'fov_mm': {'width': fov_w, 'height': fov_h}, 'overlap': overlap, 'step_mm': {'width': step_x, 'height': step_y}, 'point_count': len(points)}, 'points': points}

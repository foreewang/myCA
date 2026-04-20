from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

_WELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def require_number(value: Any, name: str) -> float:
    if value is None:
        raise ValueError(f"配置项 {name} 不能为空")
    try:
        return float(value)
    except Exception as exc:
        raise ValueError(f"配置项 {name} 不是有效数字: {value!r}") from exc


def require_sign(value: Any, name: str) -> int:
    if value is None:
        raise ValueError(f"配置项 {name} 不能为空，必须显式配置为 +1 或 -1")
    try:
        sign = int(value)
    except Exception as exc:
        raise ValueError(f"配置项 {name} 不是有效符号: {value!r}") from exc
    if sign not in (-1, 1):
        raise ValueError(f"配置项 {name} 必须为 +1 或 -1，当前为 {value!r}")
    return sign


def parse_well_name(well_name: str) -> Tuple[int, int]:
    m = _WELL_RE.match(str(well_name).strip())
    if not m:
        raise ValueError(f"无效孔位名称: {well_name!r}，期望如 A1、B3、C12")
    row_letters = m.group(1).upper()
    col = int(m.group(2))
    row = 0
    for ch in row_letters:
        row = row * 26 + (ord(ch) - ord('A') + 1)
    row -= 1
    return row, col - 1


def well_name_from_index(row_idx: int, col_idx: int) -> str:
    n = row_idx + 1
    letters: List[str] = []
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord('A') + rem))
    return ''.join(reversed(letters)) + str(col_idx + 1)


def get_rows_cols(plate_cfg: Dict[str, Any]) -> Tuple[int, int]:
    return int(plate_cfg['rows']), int(plate_cfg['cols'])


def validate_well_name(plate_cfg: Dict[str, Any], well_name: str) -> None:
    row_idx, col_idx = parse_well_name(well_name)
    rows, cols = get_rows_cols(plate_cfg)
    if not (0 <= row_idx < rows and 0 <= col_idx < cols):
        raise ValueError(f"孔位 {well_name} 超出当前板型范围：rows={rows}, cols={cols}")


def all_well_names(plate_cfg: Dict[str, Any]) -> List[str]:
    rows, cols = get_rows_cols(plate_cfg)
    return [well_name_from_index(r, c) for r in range(rows) for c in range(cols)]


def get_plate_pitch_mm(plate_cfg: Dict[str, Any]) -> float:
    return require_number(plate_cfg.get('well_diameter_mm'), 'well_diameter_mm') + require_number(plate_cfg.get('well_gap_mm'), 'well_gap_mm')


def get_pulses_per_mm(plate_cfg: Dict[str, Any]) -> float:
    if 'pulses_per_mm' in plate_cfg and plate_cfg.get('pulses_per_mm') is not None:
        return require_number(plate_cfg.get('pulses_per_mm'), 'pulses_per_mm')
    if 'rpm_mm' in plate_cfg and plate_cfg.get('rpm_mm') is not None:
        return require_number(plate_cfg.get('rpm_mm'), 'rpm_mm') * 10.0
    raise KeyError('plates 配置缺少 pulses_per_mm（或旧字段 rpm_mm）')


def get_a1_start(plate_cfg: Dict[str, Any]) -> Dict[str, int]:
    if 'a1_start' in plate_cfg and plate_cfg['a1_start'] is not None:
        a1 = plate_cfg['a1_start']
        return {'x': int(a1['x']), 'y': int(a1['y'])}
    raise KeyError('plates 配置缺少 a1_start')


def get_view_signs(plate_cfg: Dict[str, Any]) -> Tuple[int, int]:
    # 显式要求配置，避免再隐式回退到危险默认值。
    return (
        require_sign(plate_cfg.get('x_stage_sign_for_view_down'), 'x_stage_sign_for_view_down'),
        require_sign(plate_cfg.get('y_stage_sign_for_view_right'), 'y_stage_sign_for_view_right'),
    )


def get_grid_signs(plate_cfg: Dict[str, Any]) -> Tuple[int, int]:
    # 这两个符号直接决定 A1->B1、A1->A2 的换孔方向，必须显式配置。
    return (
        require_sign(plate_cfg.get('row_stage_sign'), 'row_stage_sign'),
        require_sign(plate_cfg.get('col_stage_sign'), 'col_stage_sign'),
    )


def compute_well_start(plate_cfg: Dict[str, Any], well_name: str) -> Dict[str, int]:
    validate_well_name(plate_cfg, well_name)
    a1_start = get_a1_start(plate_cfg)
    row_idx, col_idx = parse_well_name(well_name)
    pitch_mm = get_plate_pitch_mm(plate_cfg)
    ppm = get_pulses_per_mm(plate_cfg)
    row_sign, col_sign = get_grid_signs(plate_cfg)

    dx = round(row_idx * pitch_mm * ppm * row_sign)
    dy = round(col_idx * pitch_mm * ppm * col_sign)

    return {
        'x': int(a1_start['x'] + dx),
        'y': int(a1_start['y'] + dy),
        'row_index': int(row_idx),
        'col_index': int(col_idx),
        'well_name': str(well_name),
    }

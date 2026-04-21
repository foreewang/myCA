"""
培养板几何与坐标换算模块
输入 "B3"
输出 "B3 这个孔在程序里是第几行第几列，以及它对应的电机起始坐标是多少"
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Tuple

# 孔位名称正则：
# - 前半部分为一个或多个字母，表示行，如 A、B、AA
# - 后半部分为数字，表示列，如 1、3、12
# 例如：A1、B3、C12、AA5
_WELL_RE = re.compile(r"^([A-Za-z]+)(\d+)$")


def require_number(value: Any, name: str) -> float:
    """
    校验配置值是否为有效数字，并统一转换为 float。

    参数
    ----
    value : Any
        待校验的配置值。
    name : str
        配置项名称，用于报错提示。

    返回
    ----
    float
        转换后的数值。

    异常
    ----
    ValueError
        当值为空，或无法转换为数字时抛出。

    说明
    ----
    该函数用于统一处理 plates / objectives / task 等配置中的数值项，
    让上层逻辑不必反复写空值判断和类型转换。
    """
    if value is None:
        raise ValueError(f"配置项 {name} 不能为空")
    try:
        return float(value)
    except Exception as exc:
        raise ValueError(f"配置项 {name} 不是有效数字: {value!r}") from exc


def require_sign(value: Any, name: str) -> int:
    """
    校验配置值是否为有效方向符号，并限制为 +1 或 -1。

    参数
    ----
    value : Any
        待校验的符号值。
    name : str
        配置项名称，用于报错提示。

    返回
    ----
    int
        合法方向符号，仅可能为 +1 或 -1。

    异常
    ----
    ValueError
        当值为空、不可转为整数，或不在 {+1, -1} 中时抛出。

    说明
    ----
    在你的培养板扫描系统中，方向符号直接决定：
    - A1 -> B1 时 X 轴坐标如何变化
    - A1 -> A2 时 Y 轴坐标如何变化
    - 图像向下/向右时位移台应该朝哪个方向移动

    因此这里要求必须显式配置，避免再用隐式默认值埋坑。
    """
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
    """
    将孔位名称解析为零基索引 (row_idx, col_idx)。

    参数
    ----
    well_name : str
        孔位名称，例如 A1、B3、C12、AA5。

    返回
    ----
    Tuple[int, int]
        (row_idx, col_idx)，均为从 0 开始的索引。

    规则
    ----
    - A -> 0
    - B -> 1
    - Z -> 25
    - AA -> 26
    - 列号 1 -> 0，2 -> 1，依此类推

    异常
    ----
    ValueError
        当孔位名称格式不合法时抛出。

    说明
    ----
    这个函数负责把“人类可读孔名”转换成“程序内部索引”，
    是后续做板几何换算的基础入口。
    """
    m = _WELL_RE.match(str(well_name).strip())
    if not m:
        raise ValueError(f"无效孔位名称: {well_name!r}，期望如 A1、B3、C12")

    row_letters = m.group(1).upper()
    col = int(m.group(2))

    # 将字母行号按 26 进制风格转换为整数：
    # A->1, B->2, ..., Z->26, AA->27 ...
    row = 0
    for ch in row_letters:
        row = row * 26 + (ord(ch) - ord('A') + 1)

    # 转成 0-based 索引
    row -= 1
    return row, col - 1


def well_name_from_index(row_idx: int, col_idx: int) -> str:
    """
    将零基索引 (row_idx, col_idx) 转回孔位名称。

    参数
    ----
    row_idx : int
        从 0 开始的行索引。
    col_idx : int
        从 0 开始的列索引。

    返回
    ----
    str
        孔位名称，例如 A1、B3、AA5。

    说明
    ----
    该函数是 parse_well_name 的反向操作，
    常用于批量生成孔名列表、输出结果和调试显示。
    """
    n = row_idx + 1
    letters: List[str] = []

    # 将行索引转换回字母编码
    while n > 0:
        n, rem = divmod(n - 1, 26)
        letters.append(chr(ord('A') + rem))

    return ''.join(reversed(letters)) + str(col_idx + 1)


def get_rows_cols(plate_cfg: Dict[str, Any]) -> Tuple[int, int]:
    """
    从培养板配置中读取行数和列数。

    参数
    ----
    plate_cfg : Dict[str, Any]
        单个板型配置，例如 12-well / 24-well 对应的配置字典。

    返回
    ----
    Tuple[int, int]
        (rows, cols)
    """
    return int(plate_cfg['rows']), int(plate_cfg['cols'])


def validate_well_name(plate_cfg: Dict[str, Any], well_name: str) -> None:
    """
    校验孔位名称是否落在当前板型范围内。

    参数
    ----
    plate_cfg : Dict[str, Any]
        培养板配置。
    well_name : str
        待校验孔名。

    异常
    ----
    ValueError
        当孔位超出当前板型的 rows / cols 范围时抛出。

    说明
    ----
    该函数不仅检查字符串格式是否合法，
    还会进一步检查它是否真的是当前板型上存在的孔位。
    例如在 12-well 板上，D1 或 A5 都应报错。
    """
    row_idx, col_idx = parse_well_name(well_name)
    rows, cols = get_rows_cols(plate_cfg)
    if not (0 <= row_idx < rows and 0 <= col_idx < cols):
        raise ValueError(f"孔位 {well_name} 超出当前板型范围：rows={rows}, cols={cols}")


def all_well_names(plate_cfg: Dict[str, Any]) -> List[str]:
    """
    生成当前板型的全部孔位名称列表。

    参数
    ----
    plate_cfg : Dict[str, Any]
        培养板配置。

    返回
    ----
    List[str]
        按行优先顺序生成的全部孔名，例如：
        A1, A2, A3, ..., B1, B2, ...

    说明
    ----
    该函数常用于：
    - full_plate 整板扫描
    - 批量任务生成
    - 配置检查与遍历
    """
    rows, cols = get_rows_cols(plate_cfg)
    return [well_name_from_index(r, c) for r in range(rows) for c in range(cols)]


def get_plate_pitch_mm(plate_cfg: Dict[str, Any]) -> float:
    """
    计算培养板孔中心间距（pitch），单位 mm。

    参数
    ----
    plate_cfg : Dict[str, Any]
        培养板配置。

    返回
    ----
    float
        孔中心间距 = 孔径 + 孔间隙。

    说明
    ----
    这里假设 pitch_mm = well_diameter_mm + well_gap_mm。
    对你的项目来说，这个 pitch 是从 A1 推算到 B1 / A2 / C3 等孔位起点的关键参数。
    """
    return (
        require_number(plate_cfg.get('well_diameter_mm'), 'well_diameter_mm')
        + require_number(plate_cfg.get('well_gap_mm'), 'well_gap_mm')
    )


def get_pulses_per_mm(plate_cfg: Dict[str, Any]) -> float:
    """
    读取位移台脉冲换算系数：1 mm 对应多少脉冲。

    参数
    ----
    plate_cfg : Dict[str, Any]
        培养板配置。

    返回
    ----
    float
        pulses_per_mm

    兼容规则
    --------
    - 优先使用新字段 pulses_per_mm
    - 若没有，则兼容旧字段 rpm_mm，并按旧单位换算为 pulses_per_mm

    异常
    ----
    KeyError
        当新旧字段都不存在时抛出。

    说明
    ----
    你之前的旧配置里 `rpm_mm` 表示的是 0.1 mm 对应脉冲数，
    因此这里乘以 10 转成标准的“1 mm 对应脉冲数”。
    """
    if 'pulses_per_mm' in plate_cfg and plate_cfg.get('pulses_per_mm') is not None:
        return require_number(plate_cfg.get('pulses_per_mm'), 'pulses_per_mm')

    if 'rpm_mm' in plate_cfg and plate_cfg.get('rpm_mm') is not None:
        return require_number(plate_cfg.get('rpm_mm'), 'rpm_mm') * 10.0

    raise KeyError('plates 配置缺少 pulses_per_mm（或旧字段 rpm_mm）')


def get_a1_start(plate_cfg: Dict[str, Any]) -> Dict[str, int]:
    """
    读取 A1 孔观测起始点坐标。

    参数
    ----
    plate_cfg : Dict[str, Any]
        培养板配置。

    返回
    ----
    Dict[str, int]
        {'x': ..., 'y': ...}

    异常
    ----
    KeyError
        当配置中缺少 a1_start 时抛出。

    说明
    ----
    A1 起始点是整个板坐标体系的基准点。
    后续其他孔位的起始点，都是在这个基准点上按 pitch 和方向符号推算出来的。
    """
    if 'a1_start' in plate_cfg and plate_cfg['a1_start'] is not None:
        a1 = plate_cfg['a1_start']
        return {'x': int(a1['x']), 'y': int(a1['y'])}
    raise KeyError('plates 配置缺少 a1_start')


def get_view_signs(plate_cfg: Dict[str, Any]) -> Tuple[int, int]:
    """
    读取“图像视野方向 -> 位移台方向”的映射符号。

    参数
    ----
    plate_cfg : Dict[str, Any]
        培养板配置。

    返回
    ----
    Tuple[int, int]
        (x_stage_sign_for_view_down, y_stage_sign_for_view_right)

    说明
    ----
    这两个符号用于处理“图像坐标方向”和“位移台运动方向”之间的映射关系：
    - 图像向下时，位移台 X 轴应按哪个方向变化
    - 图像向右时，位移台 Y 轴应按哪个方向变化

    这里要求显式配置，不再允许隐式默认值，以避免方向搞反。
    """
    return (
        require_sign(plate_cfg.get('x_stage_sign_for_view_down'), 'x_stage_sign_for_view_down'),
        require_sign(plate_cfg.get('y_stage_sign_for_view_right'), 'y_stage_sign_for_view_right'),
    )


def get_grid_signs(plate_cfg: Dict[str, Any]) -> Tuple[int, int]:
    """
    读取培养板换孔方向符号。

    参数
    ----
    plate_cfg : Dict[str, Any]
        培养板配置。

    返回
    ----
    Tuple[int, int]
        (row_stage_sign, col_stage_sign)

    说明
    ----
    这两个符号直接决定：
    - 从 A1 到 B1，位移台坐标如何变化
    - 从 A1 到 A2，位移台坐标如何变化

    它们是整板坐标推算的基础，因此也必须显式配置。
    """
    return (
        require_sign(plate_cfg.get('row_stage_sign'), 'row_stage_sign'),
        require_sign(plate_cfg.get('col_stage_sign'), 'col_stage_sign'),
    )


def compute_well_start(plate_cfg: Dict[str, Any], well_name: str) -> Dict[str, int]:
    """
    计算指定孔位的观测起始点坐标。

    参数
    ----
    plate_cfg : Dict[str, Any]
        培养板配置。
    well_name : str
        目标孔名，例如 A1、B3、C4。

    返回
    ----
    Dict[str, int]
        包含当前孔起始点坐标及索引信息：
        {
            'x': ...,
            'y': ...,
            'row_index': ...,
            'col_index': ...,
            'well_name': ...
        }

    处理逻辑
    --------
    1. 校验孔名是否合法且在板型范围内；
    2. 读取 A1 基准点；
    3. 将孔名转换为行列索引；
    4. 读取 pitch_mm、pulses_per_mm 和换孔方向符号；
    5. 计算从 A1 到目标孔在 X/Y 上应偏移多少脉冲；
    6. 得到目标孔的观测起始点坐标。

    说明
    ----
    这是培养板几何模块最核心的函数之一。
    它负责把“孔位名字”转换成“设备能执行的起始坐标”。

    对你的扫描流程来说，后续单孔扫描、整板扫描、换孔动作，
    最终都要依赖这个函数给出的结果。
    """
    validate_well_name(plate_cfg, well_name)

    a1_start = get_a1_start(plate_cfg)
    row_idx, col_idx = parse_well_name(well_name)
    pitch_mm = get_plate_pitch_mm(plate_cfg)
    ppm = get_pulses_per_mm(plate_cfg)
    row_sign, col_sign = get_grid_signs(plate_cfg)

    # 从 A1 到目标孔的位移量，先在物理空间(mm)上计算，再乘以脉冲系数转成设备坐标
    dx = round(row_idx * pitch_mm * ppm * row_sign)
    dy = round(col_idx * pitch_mm * ppm * col_sign)

    return {
        'x': int(a1_start['x'] + dx),
        'y': int(a1_start['y'] + dy),
        'row_index': int(row_idx),
        'col_index': int(col_idx),
        'well_name': str(well_name),
    }

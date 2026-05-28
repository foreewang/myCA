"""检测结果排序/评分策略。

当前项目先保持很朴素的规则: 目标面积越大，排序越靠前。
把它单独放成函数，是为了后续如果要改成更复杂的评分策略
比如置信度、圆度、前景比例综合评分时，不需要改主流程。
"""


def score_components_by_area(components):
    """按 area_px 从大到小排序检测结果。"""
    return sorted(components, key=lambda d: int(d.get("area_px", 0)), reverse=True)

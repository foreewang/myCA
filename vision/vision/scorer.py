def score_components_by_area(components):
    """
    独立评分接口，方便后续替换更复杂的打分策略。
    当前版本保持原逻辑: 仅按 area_px 从大到小排序。
    """
    return sorted(components, key=lambda d: int(d.get('area_px', 0)), reverse=True)

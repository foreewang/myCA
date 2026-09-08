"""适配第三方检测入口并把原始检测输出规范化为统一结果结构。"""
from __future__ import annotations

import importlib
import inspect
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence


_CANDIDATE_CALLABLES = (
    "run_detect_on_image",
    "detect_image",
    "process_image",
    "run_pipeline",
    "run",
    "detect",
    "process",
)


class DetectAPIError(RuntimeError):
    pass


def _project_paths() -> list[Path]:
    project_root = Path(__file__).resolve().parent.parent
    vision_outer = project_root / "vision"
    return [project_root, vision_outer]


def _ensure_import_paths() -> None:
    for p in _project_paths():
        p_str = str(p)
        if p_str not in sys.path:
            sys.path.insert(0, p_str)


def _resolve_callable(entrypoint: str | None = None) -> Callable[..., Any]:
    _ensure_import_paths()

    candidates: List[tuple[str, str]] = []
    if entrypoint:
        if ":" not in entrypoint:
            raise DetectAPIError(
                f"detect.entrypoint 格式必须为 'module:function'，收到: {entrypoint}"
            )
        mod_name, func_name = entrypoint.split(":", 1)
        candidates.append((mod_name, func_name))

        if mod_name.startswith("vision.vision."):
            candidates.append((mod_name.replace("vision.vision.", "vision.", 1), func_name))
        elif mod_name.startswith("vision."):
            candidates.append((mod_name.replace("vision.", "vision.vision.", 1), func_name))
    else:
        for fn in _CANDIDATE_CALLABLES:
            candidates.append(("vision.vision.instance_pipeline", fn))
            candidates.append(("vision.instance_pipeline", fn))

    tried: List[str] = []
    last_err: Exception | None = None

    for mod_name, func_name in candidates:
        tried.append(f"{mod_name}:{func_name}")
        try:
            mod = importlib.import_module(mod_name)
            fn = getattr(mod, func_name, None)
            if callable(fn):
                return fn
        except Exception as exc:
            last_err = exc
            continue

    raise DetectAPIError(
        "未能自动找到视觉识别入口函数。"
        f" 已尝试: {', '.join(tried)}。"
        f" 最后错误: {last_err}"
    )


def _to_int_pair(value: Any) -> tuple[int, int] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        try:
            return int(round(float(value[0]))), int(round(float(value[1])))
        except Exception:
            return None
    return None


def _coerce_bbox(value: Any) -> List[int] | None:
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 4:
        try:
            return [
                int(round(float(value[0]))),
                int(round(float(value[1]))),
                int(round(float(value[2]))),
                int(round(float(value[3]))),
            ]
        except Exception:
            return None
    return None


def _extract_center(item: Dict[str, Any]) -> tuple[int, int] | None:
    for key in ("center_pixel", "safe_point", "dark_core_center_pixel", "center_px", "center", "centroid", "clone_center_px"):
        if key in item:
            pair = _to_int_pair(item[key])
            if pair is not None:
                return pair

    bbox = None
    for key in ("bbox", "box"):
        if key in item:
            bbox = _coerce_bbox(item[key])
            if bbox is not None:
                break
    if bbox is not None:
        x, y, w, h = bbox
        return int(round(x + w / 2.0)), int(round(y + h / 2.0))
    return None


def _extract_score(item: Dict[str, Any]) -> float | None:
    for key in ("detection_confidence", "score", "confidence", "conf", "prob"):
        if key in item:
            try:
                return float(item[key])
            except Exception:
                return None
    return None


def _extract_area(item: Dict[str, Any]) -> float | None:
    for key in ("area_px", "area", "pixel_area"):
        if key in item:
            try:
                return float(item[key])
            except Exception:
                return None
    return None


def _extract_bool(item: Dict[str, Any], key: str) -> bool | None:
    if key not in item:
        return None
    value = item[key]
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in ("1", "true", "yes", "y"):
            return True
        if lowered in ("0", "false", "no", "n"):
            return False
    return None


def _normalize_items(raw: Any) -> List[Dict[str, Any]]:
    if raw is None:
        return []

    if isinstance(raw, dict):
        for key in (
            "components",
            "clones",
            "targets",
            "colonies",
            "detections",
            "results",
            "items",
        ):
            if key in raw:
                return _normalize_items(raw[key])

        return [raw]

    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        out: List[Dict[str, Any]] = []
        for x in raw:
            if isinstance(x, dict):
                out.append(x)
        return out

    return []


def _normalize_component_items(raw: Any, *, strict: bool, kind: str) -> List[Dict[str, Any]]:
    items = _normalize_items(raw)
    clones: List[Dict[str, Any]] = []

    for idx, item in enumerate(items, start=1):
        center = _extract_center(item)
        if center is None:
            if strict:
                raise DetectAPIError(f"vision schema v2 {kind}[{idx - 1}] 缺少有效中心或 bbox")
            continue

        bbox = None
        for key in ("bbox", "box"):
            if key in item:
                bbox = _coerce_bbox(item[key])
                if bbox is not None:
                    break

        normalized = {
                "clone_id": item.get("clone_id") or item.get("target_id") or item.get("id") or f"c{idx:02d}",
                "center_px": [center[0], center[1]],
                "bbox": bbox,
                "area_px": _extract_area(item),
                "score": _extract_score(item),
                "confidence": _extract_score(item),
                "is_valid_for_compensation": _extract_bool(item, "is_valid_for_compensation"),
                "touch_image_border": _extract_bool(item, "touch_image_border"),
                "image_border_sides": list(item.get("image_border_sides") or []),
                "image_edge_clipped": _extract_bool(item, "image_edge_clipped"),
                "is_pickable": _extract_bool(item, "is_pickable"),
                "status": item.get("status"),
                "detection_confidence": item.get("detection_confidence"),
                "segmentation_status": item.get("segmentation_status"),
                "segmentation_score": item.get("segmentation_score"),
                "truncated": _extract_bool(item, "truncated"),
                "instance_label": item.get("instance_label"),
                "detection_source": item.get("detection_source"),
                "contour_points": item.get("contour_points"),
                "review_reasons": list(item.get("review_reasons") or []),
                "location_valid": _extract_bool(item, "location_valid"),
                "eligible_for_10x_centering": _extract_bool(item, "eligible_for_10x_centering"),
                "quality_assessment": item.get("quality_assessment"),
                "raw": item,
            }
        clones.append(normalized)
    return clones


def normalize_detect_result(raw_result: Any) -> Dict[str, Any]:
    schema_version = 1
    if isinstance(raw_result, dict):
        try:
            schema_version = int(raw_result.get("schema_version", 1))
        except Exception as exc:
            raise DetectAPIError("vision schema_version 必须是整数") from exc

    if schema_version >= 2:
        if not isinstance(raw_result, dict):
            raise DetectAPIError("vision schema v2 顶层结果必须是对象")
        raw_components = raw_result.get("components")
        raw_reviews = raw_result.get("review_candidates")
        if not isinstance(raw_components, list) or not isinstance(raw_reviews, list):
            raise DetectAPIError("vision schema v2 必须包含 components 和 review_candidates 列表")
        try:
            declared_components = int(raw_result["component_count"])
            declared_reviews = int(raw_result["review_candidate_count"])
        except Exception as exc:
            raise DetectAPIError("vision schema v2 缺少合法计数字段") from exc
        if declared_components != len(raw_components):
            raise DetectAPIError(
                f"vision schema v2 component_count={declared_components} 与 components={len(raw_components)} 不一致"
            )
        if declared_reviews != len(raw_reviews):
            raise DetectAPIError(
                f"vision schema v2 review_candidate_count={declared_reviews} 与 review_candidates={len(raw_reviews)} 不一致"
            )
        clones = _normalize_component_items(raw_components, strict=True, kind="components")
        reviews = _normalize_component_items(raw_reviews, strict=True, kind="review_candidates")
        return {
            "schema_version": schema_version,
            "objective_name": raw_result.get("objective_name"),
            "purpose": raw_result.get("purpose"),
            "clone_count": len(clones),
            "review_candidate_count": len(reviews),
            "clones": clones,
            "review_candidates": reviews,
            "models": raw_result.get("models") or {},
            "runtime": raw_result.get("runtime") or {},
            "quality_assessment": raw_result.get("quality_assessment"),
            "raw_result": raw_result,
        }

    items = _normalize_items(raw_result)
    clones = _normalize_component_items(items, strict=False, kind="components")

    clone_count = len(clones)
    if isinstance(raw_result, dict):
        if "component_count" in raw_result:
            try:
                clone_count = int(raw_result["component_count"])
            except Exception:
                clone_count = len(clones)
        elif "clone_count" in raw_result:
            try:
                clone_count = int(raw_result["clone_count"])
            except Exception:
                clone_count = len(clones)

    return {
        "schema_version": schema_version,
        "clone_count": clone_count,
        "clones": clones,
        "review_candidate_count": 0,
        "review_candidates": [],
        "models": {},
        "runtime": {},
        "raw_result": raw_result if isinstance(raw_result, dict) else None,
    }


def _call_detect_entrypoint(fn: Callable[..., Any], image_path: str, detect_kwargs: Dict[str, Any]) -> Any:
    if not detect_kwargs:
        return fn(image_path)

    try:
        signature = inspect.signature(fn)
    except (TypeError, ValueError):
        return fn(image_path, **detect_kwargs)

    params = signature.parameters
    accepts_kwargs = any(param.kind == inspect.Parameter.VAR_KEYWORD for param in params.values())
    if accepts_kwargs:
        return fn(image_path, **detect_kwargs)

    accepted_kwargs = {key: value for key, value in detect_kwargs.items() if key in params}
    if accepted_kwargs:
        return fn(image_path, **accepted_kwargs)

    return fn(image_path)


def run_detect_on_image(
    image_path: str | Path,
    entrypoint: str | None = None,
    detect_kwargs: Dict[str, Any] | None = None,
) -> Dict[str, Any]:
    image_path = str(Path(image_path))
    fn = _resolve_callable(entrypoint)
    try:
        raw_result = _call_detect_entrypoint(fn, image_path, detect_kwargs or {})
    except TypeError as exc:
        raise DetectAPIError(
            f"视觉入口函数调用失败：{exc}。请检查 detect.entrypoint 与对应参数。"
        ) from exc
    return normalize_detect_result(raw_result)

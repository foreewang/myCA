from __future__ import annotations

import importlib
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


def _resolve_callable(entrypoint: str | None = None) -> Callable[[str], Any]:
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
            candidates.append(("vision.vision.detect_pipeline", fn))
            candidates.append(("vision.detect_pipeline", fn))

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
    for key in ("center_px", "center_pixel", "center", "centroid", "clone_center_px", "safe_point"):
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
    for key in ("score", "confidence", "conf", "prob"):
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


def normalize_detect_result(raw_result: Any) -> Dict[str, Any]:
    items = _normalize_items(raw_result)
    clones: List[Dict[str, Any]] = []

    for idx, item in enumerate(items, start=1):
        center = _extract_center(item)
        if center is None:
            continue

        bbox = None
        for key in ("bbox", "box"):
            if key in item:
                bbox = _coerce_bbox(item[key])
                if bbox is not None:
                    break

        clones.append(
            {
                "clone_id": item.get("clone_id") or item.get("target_id") or item.get("id") or f"c{idx:02d}",
                "center_px": [center[0], center[1]],
                "bbox": bbox,
                "area_px": _extract_area(item),
                "score": _extract_score(item),
                "raw": item,
            }
        )

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
        "clone_count": clone_count,
        "clones": clones,
        "raw_result": raw_result if isinstance(raw_result, dict) else None,
    }


def run_detect_on_image(image_path: str | Path, entrypoint: str | None = None) -> Dict[str, Any]:
    image_path = str(Path(image_path))
    fn = _resolve_callable(entrypoint)
    try:
        raw_result = fn(image_path)
    except TypeError as exc:
        raise DetectAPIError(
            f"视觉入口函数调用失败：{exc}。当前默认按 fn(image_path) 调用，"
            "若你的 detect_pipeline 需要其他参数，请把函数名和签名发给我。"
        ) from exc
    return normalize_detect_result(raw_result)

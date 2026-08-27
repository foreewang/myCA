"""Validate a COCO JSON before it is admitted to the 4x training dataset."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path
from typing import Any

from PIL import Image


class AnnotationValidationError(ValueError):
    pass


def _polygon_area(points: list[float]) -> float:
    xs = points[0::2]
    ys = points[1::2]
    return abs(
        sum(xs[index] * ys[(index + 1) % len(ys)] - ys[index] * xs[(index + 1) % len(xs)] for index in range(len(xs)))
    ) / 2.0


def validate_coco(annotation_json: str | Path, image_root: str | Path) -> dict[str, Any]:
    annotation_path = Path(annotation_json).resolve()
    image_root_path = Path(image_root).resolve()
    raw = json.loads(annotation_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise AnnotationValidationError("COCO root must be an object")
    images = raw.get("images")
    annotations = raw.get("annotations")
    categories = raw.get("categories")
    if not isinstance(images, list) or not isinstance(annotations, list) or not isinstance(categories, list):
        raise AnnotationValidationError("COCO requires images, annotations, and categories lists")
    if len(categories) != 1 or categories[0].get("name") != "ipsc_clone":
        raise AnnotationValidationError("4x dataset must contain exactly one category named 'ipsc_clone'")
    category_id = categories[0].get("id")

    errors: list[str] = []
    warnings: list[str] = []
    image_by_id: dict[Any, dict[str, Any]] = {}
    group_splits: dict[str, set[str]] = {}
    split_counts: Counter[str] = Counter()
    negative_count = 0
    for index, image in enumerate(images):
        image_id = image.get("id")
        if image_id in image_by_id:
            errors.append(f"images[{index}] duplicate id={image_id!r}")
            continue
        image_by_id[image_id] = image
        file_name = image.get("file_name")
        if not isinstance(file_name, str) or not file_name:
            errors.append(f"images[{index}] missing file_name")
            continue
        image_path = (image_root_path / file_name).resolve(strict=False)
        try:
            image_path.relative_to(image_root_path)
        except ValueError:
            errors.append(f"images[{index}] file escapes image_root: {file_name}")
            continue
        if not image_path.is_file():
            errors.append(f"images[{index}] file not found: {file_name}")
            continue
        try:
            with Image.open(image_path) as opened:
                actual_size = opened.size
        except OSError as exc:
            errors.append(f"images[{index}] unreadable: {file_name}: {exc}")
            continue
        declared_size = (image.get("width"), image.get("height"))
        if declared_size != actual_size:
            errors.append(
                f"images[{index}] size mismatch: declared={declared_size}, actual={actual_size}"
            )
        if str(image.get("objective") or "").lower() != "4x":
            errors.append(f"images[{index}] objective must be explicit '4x'")
        split = str(image.get("split") or "").lower()
        if split not in {"train", "val", "test"}:
            errors.append(f"images[{index}] split must be train, val, or test")
        else:
            split_counts[split] += 1
        group_id = str(image.get("group_id") or "")
        if not group_id:
            errors.append(f"images[{index}] group_id (batch/date/well grouping) is required")
        elif split:
            group_splits.setdefault(group_id, set()).add(split)

    annotation_count_by_image: Counter[Any] = Counter()
    annotation_ids: set[Any] = set()
    for index, annotation in enumerate(annotations):
        annotation_id = annotation.get("id")
        if annotation_id in annotation_ids:
            errors.append(f"annotations[{index}] duplicate id={annotation_id!r}")
        annotation_ids.add(annotation_id)
        image_id = annotation.get("image_id")
        image = image_by_id.get(image_id)
        if image is None:
            errors.append(f"annotations[{index}] unknown image_id={image_id!r}")
            continue
        annotation_count_by_image[image_id] += 1
        if annotation.get("category_id") != category_id:
            errors.append(f"annotations[{index}] category_id must be {category_id!r}")
        bbox = annotation.get("bbox")
        if not isinstance(bbox, list) or len(bbox) != 4:
            errors.append(f"annotations[{index}] bbox must be [x,y,w,h]")
        else:
            try:
                x, y, width, height = [float(value) for value in bbox]
                if width <= 0 or height <= 0 or x < 0 or y < 0:
                    errors.append(f"annotations[{index}] bbox is invalid")
                if x + width > float(image.get("width", 0)) + 1 or y + height > float(image.get("height", 0)) + 1:
                    errors.append(f"annotations[{index}] bbox exceeds image bounds")
            except Exception:
                errors.append(f"annotations[{index}] bbox contains non-numeric values")
        segmentation = annotation.get("segmentation")
        valid_geometry = False
        if isinstance(segmentation, list):
            for polygon in segmentation:
                if isinstance(polygon, list) and len(polygon) >= 6 and len(polygon) % 2 == 0:
                    try:
                        valid_geometry = _polygon_area([float(value) for value in polygon]) > 0
                    except Exception:
                        valid_geometry = False
                    if valid_geometry:
                        break
        elif isinstance(segmentation, dict):
            valid_geometry = bool(segmentation.get("counts") and segmentation.get("size"))
        if not valid_geometry:
            errors.append(f"annotations[{index}] requires a non-empty polygon or RLE mask")
        if not isinstance(annotation.get("truncated"), bool):
            errors.append(f"annotations[{index}] truncated must be explicit boolean")

    for image_id, image in image_by_id.items():
        if annotation_count_by_image[image_id] == 0:
            if image.get("annotation_status") != "complete_negative":
                errors.append(
                    f"image id={image_id!r} has no instances but is not marked complete_negative"
                )
            else:
                negative_count += 1
    leaking = {group: sorted(splits) for group, splits in group_splits.items() if len(splits) > 1}
    for group, splits in leaking.items():
        errors.append(f"group_id={group!r} leaks across splits {splits}")
    if negative_count == 0:
        warnings.append("No reviewed empty/hard-negative image is present.")
    if not split_counts.get("test"):
        warnings.append("No independent test images are assigned.")

    return {
        "valid": not errors,
        "errors": errors,
        "warnings": warnings,
        "summary": {
            "image_count": len(images),
            "annotation_count": len(annotations),
            "negative_image_count": negative_count,
            "split_counts": dict(sorted(split_counts.items())),
            "group_count": len(group_splits),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate annotated 4x COCO instances")
    parser.add_argument("annotation_json")
    parser.add_argument("image_root")
    args = parser.parse_args(argv)
    try:
        report = validate_coco(args.annotation_json, args.image_root)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

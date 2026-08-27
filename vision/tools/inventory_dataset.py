"""Create a read-only inventory of images awaiting instance annotation.

The scanner deliberately does not infer colony quality from directory names.  In
particular, ``较高质量`` and ``较低质量`` are useful acquisition-time hints,
but are not valid labels for the 4x instance-localisation task.

Example::

    python vision/tools/inventory_dataset.py "C:\\data\\iPSC" --output inventory.json
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, UnidentifiedImageError


SUPPORTED_EXTENSIONS = frozenset({".bmp", ".jpg", ".jpeg", ".png"})
QUALITY_FOLDER_NAMES = frozenset({"较高质量", "较低质量", "high_quality", "low_quality"})
QUALITY_WARNING = (
    "Folder names such as '较高质量'/'较低质量' are acquisition-time quality hints, "
    "not 4x instance labels. At 4x, annotate every visible iPSC colony with an "
    "instance polygon or mask; assess quality separately from 10x imagery."
)


def _sort_key(path: Path) -> str:
    return path.as_posix().casefold()


def iter_image_paths(root: Path) -> Iterable[Path]:
    """Yield supported image paths in deterministic order without changing them."""

    paths = (
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.casefold() in SUPPORTED_EXTENSIONS
    )
    yield from sorted(paths, key=_sort_key)


def read_image_metadata(path: Path) -> dict[str, Any]:
    """Read only image header metadata; ``Path`` keeps Windows Unicode paths safe."""

    with Image.open(path) as image:
        width, height = image.size
        bands = image.getbands()
        return {
            "width": int(width),
            "height": int(height),
            "channels": len(bands),
            "mode": image.mode,
            "format": str(image.format or path.suffix.lstrip(".")).upper(),
        }


def _quality_folder_hint(relative_path: Path) -> str | None:
    for part in relative_path.parts[:-1]:
        if part.casefold() in QUALITY_FOLDER_NAMES:
            return part
    return None


def build_inventory(dataset_root: str | Path, *, objective: str = "unknown") -> dict[str, Any]:
    """Build a JSON-serialisable inventory while opening source files read-only."""

    root = Path(dataset_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(f"Dataset root is not a directory: {root}")

    objective = str(objective).strip().lower()
    if objective not in {"4x", "10x", "unknown"}:
        raise ValueError("objective must be '4x', '10x', or 'unknown'")

    images: list[dict[str, Any]] = []
    unreadable = 0
    quality_hints_found: set[str] = set()

    for image_id, image_path in enumerate(iter_image_paths(root), start=1):
        relative_path = image_path.relative_to(root)
        quality_hint = _quality_folder_hint(relative_path)
        if quality_hint:
            quality_hints_found.add(quality_hint)

        record: dict[str, Any] = {
            "id": image_id,
            "relative_path": relative_path.as_posix(),
            "objective": objective,
            "annotation_status": "pending",
        }
        if quality_hint:
            record["source_folder_hint"] = quality_hint
            record["source_folder_hint_is_label"] = False

        try:
            record.update(read_image_metadata(image_path))
        except (OSError, ValueError, UnidentifiedImageError) as exc:
            unreadable += 1
            record["read_error"] = f"{type(exc).__name__}: {exc}"
        images.append(record)

    warnings = [QUALITY_WARNING]
    if not images:
        warnings.append("No BMP/JPG/JPEG/PNG images were found under the dataset root.")
    if unreadable:
        warnings.append(
            f"{unreadable} image(s) could not be read; fix or exclude them before annotation."
        )

    return {
        "schema_version": 1,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "dataset_root": str(root),
        "objective": {
            "magnification": objective,
            "source": "operator_supplied" if objective != "unknown" else "not_encoded_in_image_files",
            "task": "iPSC_colony_instance_localization_and_segmentation" if objective == "4x" else "iPSC_colony_quality_reference" if objective == "10x" else "reference_only_until_objective_confirmed",
            "quality_assessment": "not_assessed_at_4x" if objective == "4x" else "not_inferred_from_folder_name",
        },
        "annotation_policy": {
            "required_geometry": "one polygon or mask per visible colony instance",
            "empty_image_status": "complete_negative",
            "pending_images_are_training_eligible": False,
        },
        "summary": {
            "image_count": len(images),
            "readable_image_count": len(images) - unreadable,
            "unreadable_image_count": unreadable,
            "quality_hint_folders_found": sorted(quality_hints_found, key=str.casefold),
        },
        "warnings": warnings,
        "images": images,
    }


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Inventory 4x colony images without modifying the source dataset."
    )
    parser.add_argument("dataset_root", help="Root containing BMP/JPG/JPEG/PNG images")
    parser.add_argument(
        "--objective",
        choices=("4x", "10x", "unknown"),
        default="unknown",
        help="operator-confirmed magnification; image files do not encode it",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="Write UTF-8 JSON here; omit to print JSON to stdout",
    )
    args = parser.parse_args(argv)

    try:
        inventory = build_inventory(args.dataset_root, objective=args.objective)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))

    if args.output:
        _write_json(args.output.expanduser().resolve(), inventory)
        print(
            f"Inventoried {inventory['summary']['image_count']} image(s); "
            f"wrote {args.output}."
        )
        print(f"WARNING: {QUALITY_WARNING}")
    else:
        print(json.dumps(inventory, ensure_ascii=False, indent=2))
    return 0 if inventory["summary"]["unreadable_image_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())

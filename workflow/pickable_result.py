"""Export all photos and selectable clones, with explicit deduplication semantics."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from typing import Any


def pickable_output_path(params: dict[str, Any]) -> Path | None:
    if params.get("pickable_output_json"):
        path = Path(params["pickable_output_json"])
    elif params.get("detect_output_json"):
        path = Path(params["detect_output_json"]).with_name("pickable_detect_result.json")
    elif params.get("save_dir"):
        path = Path(params["save_dir"]) / "pickable_detect_result.json"
    else:
        return None  # Pure in-memory callers have no artifact destination.
    for key in ("detect_output_json", "scan_output_json", "scan_result_json",
                "compensate_output_json", "result_output_json", "_dump_json"):
        if params.get(key) and path.resolve() == Path(params[key]).resolve():
            raise ValueError(f"pickable_output_json must not overwrite {key}")
    return path


def _rank(ref):
    image, clone = ref
    dx, dy = clone.get("offset_from_image_center_px") or [0, 0]
    return (bool(clone.get("touch_image_border") or clone.get("truncated")),
            -float(clone.get("confidence") or 0), float(dx) ** 2 + float(dy) ** 2,
            int(image["index"]), str(clone["clone_id"]))


def build_pickable_detect_result(result: dict[str, Any]) -> dict[str, Any]:
    """Export explicit per-image mode or validated existing deduplication groups."""
    if (result.get("deduplication") or {}).get("enabled") is False:
        return _build_per_image_candidates(result)
    refs = {}
    for image in result.get("images", []):
        for clone in image.get("clones", []):
            source = clone.get("source_detection_id") or f"I{image['index']}:{clone['clone_id']}"
            if source in refs:
                raise ValueError(f"duplicate source detection: {source}")
            refs[source] = (image, clone)

    groups = result.get("unique_clones")
    if not isinstance(groups, list):
        raise ValueError("unique_clones is required; deduplicate the detection result first")
    seen_sources, seen_groups = set(), set()
    selected = set()
    for group in groups:
        gid = group.get("global_clone_id")
        if not gid or gid in seen_groups:
            raise ValueError("missing or duplicate global_clone_id")
        seen_groups.add(gid)
        eligible = []
        sources = group.get("source_detections") or []
        if not sources:
            raise ValueError(f"empty deduplication group: {gid}")
        for source in sources:
            if source not in refs or source in seen_sources:
                raise ValueError(f"invalid deduplication source: {source}")
            seen_sources.add(source)
            image, clone = refs[source]
            if clone.get("global_clone_id") != gid:
                raise ValueError(f"inconsistent global_clone_id: {source}")
            if clone.get("is_pickable") is True:
                eligible.append((image, clone))
        if not eligible:
            continue
        image, clone = min(eligible, key=_rank)
        source = clone.get("source_detection_id") or f"I{image['index']}:{clone['clone_id']}"
        selected.add(source)
    if seen_sources != set(refs):
        raise ValueError("deduplication groups do not cover all observations")

    images = []
    for original in result.get("images", []):
        clones = [{"clone_id": c["clone_id"], "global_clone_id": c["global_clone_id"],
                   "is_pickable": True, "center_px": deepcopy(c.get("center_px")),
                   "offset_from_image_center_px": deepcopy(c.get("offset_from_image_center_px"))}
                  for c in original.get("clones", [])
                  if (c.get("source_detection_id") or f"I{original['index']}:{c['clone_id']}") in selected]
        # Allowlist fields: newly added diagnostic fields must stay internal.
        image = {k: deepcopy(original[k]) for k in
                 ("index", "image_path", "overlay_image_path", "overlay_preview_path", "stage_x_actual", "stage_y_actual",
                  "stage_x_target", "stage_y_target", "mm_per_pixel") if k in original}
        image.update(is_pickable=bool(clones), clones=clones)
        images.append(image)
    metadata = ("task_id", "status", "plate_type", "well_name", "objective_name")
    output = {k: deepcopy(result[k]) for k in metadata if k in result}
    output.update(
        schema_version=3, images=images,
        image_count=len(images), total_clone_count=len(selected),
    )
    return output


def _build_per_image_candidates(result: dict[str, Any]) -> dict[str, Any]:
    """Preserve source coordinates for manual image-and-clone selection."""
    images = []
    seen_images = set()
    count = 0
    for original in result.get("images", []):
        index = original["index"]
        if index in seen_images:
            raise ValueError(f"duplicate image index: {index}")
        seen_images.add(index)
        clones = []
        seen_clones = set()
        for clone in original.get("clones", []):
            cid = clone["clone_id"]
            if cid in seen_clones:
                raise ValueError(f"duplicate clone_id in image {index}: {cid}")
            seen_clones.add(cid)
            if clone.get("is_pickable") is not True:
                continue
            clones.append({
                "clone_id": cid, "global_clone_id": None,
                "source_detection_id": f"I{index}:{cid}", "is_pickable": True,
                "center_px": deepcopy(clone.get("center_px")),
                "offset_from_image_center_px": deepcopy(clone.get("offset_from_image_center_px")),
            })
        image = {key: deepcopy(original[key]) for key in (
            "index", "image_path", "overlay_image_path", "overlay_preview_path",
            "stage_x_actual", "stage_y_actual", "stage_x_target", "stage_y_target",
            "mm_per_pixel", "point_name",
        ) if key in original}
        image.update(is_pickable=bool(clones), clones=clones)
        images.append(image)
        count += len(clones)
    output = {key: deepcopy(result[key]) for key in (
        "task_id", "status", "plate_type", "well_name", "objective_name",
    ) if key in result}
    output.update(
        schema_version=4, images=images, image_count=len(images),
        total_clone_count=count, candidate_count=count, unique_clone_count=None,
        count_semantics="per_image_candidates",
        deduplication={"enabled": False, "status": "not_performed"},
    )
    return output


def main() -> None:
    import argparse
    from workflow.file_io import atomic_write_json, read_json_with_retry

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="Existing detect_result.json with explicit per-image mode or deduplication groups")
    parser.add_argument("--output", help="Defaults to pickable_detect_result.json beside input")
    args = parser.parse_args()
    path = pickable_output_path({"detect_output_json": args.input, "pickable_output_json": args.output})
    atomic_write_json(path, build_pickable_detect_result(read_json_with_retry(args.input)))
    print(path)


if __name__ == "__main__":
    main()

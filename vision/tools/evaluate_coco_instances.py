"""Evaluate 4x instance predictions against an annotated COCO test split.

This evaluator intentionally uses only NumPy/OpenCV so it can run in the device
environment. Matching is one-to-one by mask IoU. Report both per-instance
precision/recall and mean Dice; release acceptance must be decided on an
independent, group-separated test set.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import cv2
import numpy as np


def _mask_from_segmentation(segmentation: Any, height: int, width: int) -> np.ndarray:
    mask = np.zeros((height, width), dtype=np.uint8)
    if isinstance(segmentation, list):
        polygons = []
        for points in segmentation:
            array = np.asarray(points, dtype=np.float32).reshape(-1, 2)
            polygons.append(np.rint(array).astype(np.int32).reshape(-1, 1, 2))
        if polygons:
            cv2.fillPoly(mask, polygons, 1)
        return mask
    raise ValueError("evaluator currently requires polygon COCO segmentations")


def _predicted_mask(item: dict[str, Any], height: int, width: int) -> np.ndarray:
    points = item.get("contour_points")
    if not isinstance(points, list) or len(points) < 3:
        raise ValueError(f"prediction {item.get('id')} lacks contour_points")
    mask = np.zeros((height, width), dtype=np.uint8)
    polygon = np.rint(np.asarray(points, dtype=np.float32)).astype(np.int32).reshape(-1, 1, 2)
    cv2.fillPoly(mask, [polygon], 1)
    return mask


def evaluate_coco_predictions(
    annotation_json: str | Path,
    predictions_json: str | Path,
    *,
    match_iou: float = 0.50,
) -> dict[str, Any]:
    ground_truth = json.loads(Path(annotation_json).read_text(encoding="utf-8"))
    predictions = json.loads(Path(predictions_json).read_text(encoding="utf-8"))
    images = {str(item["file_name"]).replace("\\", "/"): item for item in ground_truth["images"]}
    annotations_by_image: dict[Any, list[dict[str, Any]]] = {}
    for annotation in ground_truth["annotations"]:
        annotations_by_image.setdefault(annotation["image_id"], []).append(annotation)
    prediction_by_path = {
        str(item["image_path"]).replace("\\", "/"): item
        for item in predictions.get("images", [])
    }

    tp = fp = fn = 0
    matched_ious: list[float] = []
    matched_dice: list[float] = []
    per_image: list[dict[str, Any]] = []
    for relative_path, image in sorted(images.items()):
        if str(image.get("split") or "").lower() != "test":
            continue
        height, width = int(image["height"]), int(image["width"])
        gt_masks = [
            _mask_from_segmentation(annotation["segmentation"], height, width)
            for annotation in annotations_by_image.get(image["id"], [])
        ]
        candidates = [
            value
            for key, value in prediction_by_path.items()
            if key == relative_path or key.endswith("/" + relative_path)
        ]
        if len(candidates) != 1:
            raise ValueError(f"expected one prediction record for test image {relative_path}, found {len(candidates)}")
        pred_masks = [_predicted_mask(item, height, width) for item in candidates[0].get("clones", [])]
        pairs: list[tuple[float, int, int]] = []
        for gt_index, gt_mask in enumerate(gt_masks):
            gt_area = int(gt_mask.sum())
            for pred_index, pred_mask in enumerate(pred_masks):
                intersection = int(np.logical_and(gt_mask, pred_mask).sum())
                union = gt_area + int(pred_mask.sum()) - intersection
                pairs.append((intersection / union if union else 0.0, gt_index, pred_index))
        used_gt: set[int] = set()
        used_pred: set[int] = set()
        image_matches = 0
        for iou, gt_index, pred_index in sorted(pairs, reverse=True):
            if iou < match_iou or gt_index in used_gt or pred_index in used_pred:
                continue
            used_gt.add(gt_index)
            used_pred.add(pred_index)
            image_matches += 1
            intersection = int(np.logical_and(gt_masks[gt_index], pred_masks[pred_index]).sum())
            dice = 2.0 * intersection / float(int(gt_masks[gt_index].sum()) + int(pred_masks[pred_index].sum()))
            matched_ious.append(float(iou))
            matched_dice.append(float(dice))
        tp += image_matches
        fp += len(pred_masks) - image_matches
        fn += len(gt_masks) - image_matches
        per_image.append(
            {
                "file_name": relative_path,
                "ground_truth_count": len(gt_masks),
                "prediction_count": len(pred_masks),
                "true_positive": image_matches,
            }
        )
    precision = tp / float(tp + fp) if tp + fp else 1.0
    recall = tp / float(tp + fn) if tp + fn else 1.0
    return {
        "match_iou": float(match_iou),
        "true_positive": tp,
        "false_positive": fp,
        "false_negative": fn,
        "precision": precision,
        "recall": recall,
        "mean_matched_iou": float(np.mean(matched_ious)) if matched_ious else None,
        "mean_matched_dice": float(np.mean(matched_dice)) if matched_dice else None,
        "acceptance": {
            "target_precision": 0.995,
            "target_recall": 0.98,
            "passes_count_targets": precision >= 0.995 and recall >= 0.98,
        },
        "per_image": per_image,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate schema-v2 predictions on COCO test instances")
    parser.add_argument("annotation_json")
    parser.add_argument("predictions_json")
    parser.add_argument("--match-iou", type=float, default=0.50)
    args = parser.parse_args(argv)
    report = evaluate_coco_predictions(args.annotation_json, args.predictions_json, match_iou=args.match_iou)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["acceptance"]["passes_count_targets"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

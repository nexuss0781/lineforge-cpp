#!/usr/bin/env python3
"""Evaluate line-crop manifests against hand-labeled bounding boxes.

Annotations are kept separately from the public source tree when they contain
private or licensed page imagery. See README.md for the compact JSON schema.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path
from typing import Any

Box = tuple[float, float, float, float]


def as_box(value: list[int | float]) -> Box:
    if len(value) != 4:
        raise ValueError(f"bbox_xyxy must have four coordinates: {value!r}")
    x0, y0, x1, y1 = (float(v) for v in value)
    if x1 <= x0 or y1 <= y0:
        raise ValueError(f"bbox_xyxy must have positive area: {value!r}")
    return x0, y0, x1, y1


def area(box: Box) -> float:
    return max(0.0, box[2] - box[0]) * max(0.0, box[3] - box[1])


def intersection(a: Box, b: Box) -> float:
    return max(0.0, min(a[2], b[2]) - max(a[0], b[0])) * max(
        0.0, min(a[3], b[3]) - max(a[1], b[1])
    )


def iou(a: Box, b: Box) -> float:
    overlap = intersection(a, b)
    union = area(a) + area(b) - overlap
    return overlap / union if union else 0.0


def inside_roi(box: Box, roi: Box | None) -> bool:
    if roi is None:
        return True
    center_x = (box[0] + box[2]) / 2
    center_y = (box[1] + box[3]) / 2
    return roi[0] <= center_x < roi[2] and roi[1] <= center_y < roi[3]


def vertical_boundary_summary(errors: list[tuple[float, float]]) -> dict[str, Any]:
    top = [error[0] for error in errors]
    bottom = [error[1] for error in errors]
    count = len(errors)
    if not count:
        return {
            "matched_lines": 0,
            "mean_absolute_top_error_px": None,
            "median_absolute_top_error_px": None,
            "mean_absolute_bottom_error_px": None,
            "median_absolute_bottom_error_px": None,
            "top_within_3px": 0,
            "bottom_within_3px": 0,
            "top_clipped": 0,
            "bottom_clipped": 0,
            "top_excess_whitespace": 0,
            "bottom_excess_whitespace": 0,
        }
    absolute_top = [abs(value) for value in top]
    absolute_bottom = [abs(value) for value in bottom]
    return {
        "matched_lines": count,
        "mean_absolute_top_error_px": round(sum(absolute_top) / count, 3),
        "median_absolute_top_error_px": round(statistics.median(absolute_top), 3),
        "mean_absolute_bottom_error_px": round(sum(absolute_bottom) / count, 3),
        "median_absolute_bottom_error_px": round(statistics.median(absolute_bottom), 3),
        "top_within_3px": sum(value <= 3 for value in absolute_top),
        "bottom_within_3px": sum(value <= 3 for value in absolute_bottom),
        # Positive top error means the crop begins inside the annotated line;
        # negative bottom error means it ends before the annotated line ends.
        "top_clipped": sum(value > 0 for value in top),
        "bottom_clipped": sum(value < 0 for value in bottom),
        "top_excess_whitespace": sum(value < 0 for value in top),
        "bottom_excess_whitespace": sum(value > 0 for value in bottom),
    }


def evaluate_page(page: dict[str, Any], root: Path, threshold: float) -> dict[str, Any]:
    manifest_path = Path(page["manifest"])
    if not manifest_path.is_absolute():
        manifest_path = root / manifest_path
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    roi = as_box(page["roi_xyxy"]) if page.get("roi_xyxy") else None
    truth = [as_box(b) for b in page["ground_truth_xyxy"]]
    line_items = manifest.get("lines", [])
    predicted = [as_box(line["bbox_xyxy"]) for line in line_items]
    predicted = [box for box in predicted if inside_roi(box, roi)]
    selected_items = [line for line in line_items if inside_roi(as_box(line["bbox_xyxy"]), roi)]

    pairs: list[tuple[float, int, int]] = []
    for pi, pred in enumerate(predicted):
        for gi, gt in enumerate(truth):
            score = iou(pred, gt)
            if score >= threshold:
                pairs.append((score, pi, gi))
    pairs.sort(reverse=True)
    matched_pred: set[int] = set()
    matched_truth: set[int] = set()
    matches: list[dict[str, Any]] = []
    for score, pi, gi in pairs:
        if pi in matched_pred or gi in matched_truth:
            continue
        matched_pred.add(pi)
        matched_truth.add(gi)
        coverage = intersection(predicted[pi], truth[gi]) / area(truth[gi])
        top_error = predicted[pi][1] - truth[gi][1]
        bottom_error = predicted[pi][3] - truth[gi][3]
        matches.append({"prediction_index": pi, "truth_index": gi,
                        "iou": round(score, 4), "truth_coverage": round(coverage, 4),
                        "top_boundary_error_px": round(top_error, 3),
                        "bottom_boundary_error_px": round(bottom_error, 3)})

    vertical_boundary = vertical_boundary_summary([
        (match["top_boundary_error_px"], match["bottom_boundary_error_px"])
        for match in matches
    ])

    coverage_by_truth: list[list[tuple[int, float]]] = [[] for _ in truth]
    coverage_by_pred: list[list[tuple[int, float]]] = [[] for _ in predicted]
    for pi, pred in enumerate(predicted):
        for gi, gt in enumerate(truth):
            overlap = intersection(pred, gt)
            if overlap <= 0:
                continue
            pred_coverage = overlap / area(pred)
            gt_coverage = overlap / area(gt)
            if gt_coverage >= 0.20:
                coverage_by_truth[gi].append((pi, gt_coverage))
            # A wide crop can fully swallow an indented/short line while that
            # line occupies less than 20% of the crop's area. Count either a
            # meaningful fraction of the crop or most of a labeled line as
            # evidence that the candidate may contain multiple lines.
            if pred_coverage >= 0.15 or gt_coverage >= 0.50:
                coverage_by_pred[pi].append((gi, pred_coverage))

    cut_truth = 0
    cut_predictions: set[int] = set()
    for gi, overlaps in enumerate(coverage_by_truth):
        if not overlaps:
            continue
        pi, _ = max(overlaps, key=lambda item: item[1])
        pred, gt = predicted[pi], truth[gi]
        width_coverage = max(0.0, min(pred[2], gt[2]) - max(pred[0], gt[0])) / (gt[2] - gt[0])
        height_coverage = max(0.0, min(pred[3], gt[3]) - max(pred[1], gt[1])) / (gt[3] - gt[1])
        # A line is a cut candidate if the best overlapping crop omits >20%
        # of its width or >50% of its height. Small manual-label margins alone
        # should not be reported as cut glyphs.
        if width_coverage < 0.80 or height_coverage < 0.50:
            cut_truth += 1
            cut_predictions.add(pi)
    split_truth = sum(len(overlaps) > 1 for overlaps in coverage_by_truth)
    merged_predictions = sum(len(overlaps) > 1 for overlaps in coverage_by_pred)
    tp = len(matched_pred)
    error_predictions = set(range(len(predicted))) - matched_pred
    error_predictions.update(cut_predictions)
    error_predictions.update(i for i, overlaps in enumerate(coverage_by_pred) if len(overlaps) > 1)
    error_predictions.update(i for overlaps in coverage_by_truth if len(overlaps) > 1
                             for i, _ in overlaps)
    review_prediction_indices = {
        i for i, item in enumerate(selected_items) if item.get("review_required", False)
    }
    flagged_error_predictions = error_predictions & review_prediction_indices
    excluded_items = manifest.get("excluded", [])
    selected_excluded = [item for item in excluded_items
                         if inside_roi(as_box(item["bbox_xyxy"]), roi)]
    review_items = len(review_prediction_indices) + sum(
        bool(item.get("review_required", item.get("review", True))) for item in selected_excluded
    )
    candidate_count = len(predicted) + len(selected_excluded)
    page_has_error = bool(
        len(truth) - len(matched_truth) or error_predictions or cut_truth or
        merged_predictions or split_truth
    )
    page_has_review_alert = review_items > 0
    return {
        "name": page.get("name", manifest_path.stem),
        "iou_threshold": threshold,
        "ground_truth_lines": len(truth),
        "predicted_crops": len(predicted),
        "true_positive_matches": tp,
        "false_positive_crops": len(predicted) - tp,
        "missed_lines": len(truth) - len(matched_truth),
        "precision": round(tp / len(predicted), 4) if predicted else 0.0,
        "recall": round(tp / len(truth), 4) if truth else 0.0,
        "cut_lines": cut_truth,
        "merged_crops": merged_predictions,
        "split_lines": split_truth,
        "review_candidates": review_items,
        "review_candidate_rate": round(review_items / candidate_count, 4) if candidate_count else 0.0,
        "candidate_count_including_excluded": candidate_count,
        "crop_error_candidates": len(error_predictions),
        "reviewed_crop_errors": len(flagged_error_predictions),
        "crop_error_review_recall": (round(len(flagged_error_predictions) / len(error_predictions), 4)
                                     if error_predictions else None),
        "page_has_segmentation_error": page_has_error,
        "page_has_review_alert": page_has_review_alert,
        "vertical_boundary": vertical_boundary,
        "matches": matches,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--annotations", required=True, type=Path,
                        help="JSON file containing manifest paths and human-labeled boxes")
    parser.add_argument("--output-json", type=Path,
                        help="Optional output path for the complete metrics report")
    parser.add_argument("--iou-threshold", type=float,
                        help="Override the threshold in the annotations (default: 0.5)")
    args = parser.parse_args()
    annotations_path = args.annotations.resolve()
    annotations = json.loads(annotations_path.read_text(encoding="utf-8"))
    threshold = args.iou_threshold if args.iou_threshold is not None else float(annotations.get("iou_threshold", 0.5))
    if not 0 < threshold <= 1:
        parser.error("IoU threshold must be in (0, 1]")
    pages = [evaluate_page(page, annotations_path.parent, threshold)
             for page in annotations.get("pages", [])]
    total_gt = sum(page["ground_truth_lines"] for page in pages)
    total_pred = sum(page["predicted_crops"] for page in pages)
    total_tp = sum(page["true_positive_matches"] for page in pages)
    total_candidates = sum(page["candidate_count_including_excluded"] for page in pages)
    total_reviews = sum(page["review_candidates"] for page in pages)
    total_crop_errors = sum(page["crop_error_candidates"] for page in pages)
    total_reviewed_crop_errors = sum(page["reviewed_crop_errors"] for page in pages)
    all_vertical_errors = [
        (match["top_boundary_error_px"], match["bottom_boundary_error_px"])
        for page in pages for match in page["matches"]
    ]
    error_pages = [page for page in pages if page["page_has_segmentation_error"]]
    reviewed_error_pages = sum(page["page_has_review_alert"] for page in error_pages)
    result = {
        "benchmark": "hand-labeled line-crop evaluation",
        "matching": f"greedy one-to-one IoU >= {threshold:.3f}; boxes are xyxy with exclusive right/bottom edges",
        "scope_note": "Only annotations and optional ROIs listed in the input are scored. Excluded review candidates are not counted as predicted crops.",
        "pages": pages,
        "aggregate": {
            "pages": len(pages), "ground_truth_lines": total_gt,
            "predicted_crops": total_pred, "true_positive_matches": total_tp,
            "false_positive_crops": total_pred - total_tp,
            "missed_lines": total_gt - total_tp,
            "precision": round(total_tp / total_pred, 4) if total_pred else 0.0,
            "recall": round(total_tp / total_gt, 4) if total_gt else 0.0,
            "cut_lines": sum(page["cut_lines"] for page in pages),
            "merged_crops": sum(page["merged_crops"] for page in pages),
            "split_lines": sum(page["split_lines"] for page in pages),
            "review_candidates": total_reviews,
            "candidate_count_including_excluded": total_candidates,
            "review_candidate_rate": round(total_reviews / total_candidates, 4) if total_candidates else 0.0,
            "crop_error_candidates": total_crop_errors,
            "reviewed_crop_errors": total_reviewed_crop_errors,
            "crop_error_review_recall": (round(total_reviewed_crop_errors / total_crop_errors, 4)
                                         if total_crop_errors else None),
            "pages_with_segmentation_errors": len(error_pages),
            "error_page_review_recall": (round(reviewed_error_pages / len(error_pages), 4)
                                         if error_pages else None),
            "vertical_boundary": vertical_boundary_summary(all_vertical_errors),
        },
    }
    output = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

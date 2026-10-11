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


def contains(outer: Box, inner: Box) -> bool:
    return (outer[0] <= inner[0] and outer[1] <= inner[1] and
            outer[2] >= inner[2] and outer[3] >= inner[3])


def point_in_region(region: dict[str, Any], x: float, y: float) -> bool:
    box = as_box(region["bbox_xyxy"])
    if not (box[0] <= x < box[2] and box[1] <= y < box[3]):
        return False

    def edge_value(key: str, fallback: float) -> float:
        points = region.get(key)
        if not points:
            return fallback
        ordered = sorted(((float(px), float(py)) for px, py in points), key=lambda point: point[1])
        if y <= ordered[0][1]:
            return ordered[0][0]
        if y >= ordered[-1][1]:
            return ordered[-1][0]
        for (x0, y0), (x1, y1) in zip(ordered, ordered[1:]):
            if y0 <= y <= y1:
                if y1 == y0:
                    return (x0 + x1) / 2
                ratio = (y - y0) / (y1 - y0)
                return x0 + ratio * (x1 - x0)
        return fallback

    left = edge_value("left_edge_xy", box[0])
    right = edge_value("right_edge_xy", box[2])
    return left <= x < right


def order_summary(predicted_truth: list[int], truth_order: list[int]) -> dict[str, Any]:
    rank = {truth_index: position for position, truth_index in enumerate(truth_order)}
    observed = [truth_index for truth_index in predicted_truth if truth_index in rank]
    concordant = discordant = 0
    for i, first in enumerate(observed):
        for second in observed[i + 1:]:
            if rank[first] < rank[second]:
                concordant += 1
            else:
                discordant += 1
    pairs = concordant + discordant
    return {
        "matched_lines_in_order": len(observed),
        "ground_truth_order": truth_order,
        "predicted_order_of_matched_truth_indices": observed,
        "concordant_pairs": concordant,
        "comparable_pairs": pairs,
        "pairwise_order_accuracy": round(concordant / pairs, 4) if pairs else None,
        "pairwise_order_violations": discordant,
        "exact_order": observed == [i for i in truth_order if i in set(observed)],
    }


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
    excluded_items = manifest.get("excluded", [])
    selected_excluded = [item for item in excluded_items
                         if inside_roi(as_box(item["bbox_xyxy"]), roi)]

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

    matched_truth_to_pred = {m["truth_index"]: m["prediction_index"] for m in matches}
    predicted_truth_indices = [matches_by_pred for matches_by_pred in sorted(
        matched_truth_to_pred, key=lambda index: matched_truth_to_pred[index])]
    truth_order = [int(i) for i in page.get("ground_truth_order", range(len(truth)))]
    if sorted(truth_order) != list(range(len(truth))):
        raise ValueError("ground_truth_order must be a permutation of ground-truth line indices")
    reading_order = order_summary(predicted_truth_indices, truth_order)

    word_labels = page.get("ground_truth_words_xyxy", [])
    retained_words = 0
    word_results: list[dict[str, Any]] = []
    for word_index, word in enumerate(word_labels):
        if isinstance(word, dict):
            word_box = as_box(word["bbox_xyxy"])
            line_index = word.get("line_index")
        else:
            word_box = as_box(word)
            line_index = None
        if line_index is not None:
            line_index = int(line_index)
            if not 0 <= line_index < len(truth):
                raise ValueError(f"word label line_index out of range: {line_index}")
            pred_index = matched_truth_to_pred.get(line_index)
            candidates = [selected_items[pred_index]] if pred_index is not None else []
        else:
            candidates = selected_items
        contained_by_crop = False
        for item in candidates:
            source = item.get("source_crop_bbox_xyxy", item["bbox_xyxy"])
            if contains(as_box(source), word_box):
                contained_by_crop = True
                break
        retained_words += int(contained_by_crop)
        word_results.append({"word_index": word_index, "line_index": line_index,
                             "retained_inside_matched_crop": contained_by_crop})
    word_retention_rate = retained_words / len(word_labels) if word_labels else None

    expected_region_ids = page.get("ground_truth_region_ids", [])
    region_boxes = page.get("ground_truth_regions", {})
    prediction_regions = {str(region["id"]): region
                         for region in manifest.get("layout", {}).get("regions", [])}
    region_assignment_count = 0
    region_assignment_errors = 0
    if expected_region_ids:
        if len(expected_region_ids) != len(truth):
            raise ValueError("ground_truth_region_ids must align with ground_truth_xyxy")
        for gi, pred_index in matched_truth_to_pred.items():
            expected_id = expected_region_ids[gi]
            if expected_id is None:
                continue
            expected_value = region_boxes.get(str(expected_id)) if isinstance(region_boxes, dict) else None
            if expected_value is None:
                raise ValueError(f"missing ground_truth_regions entry for {expected_id!r}")
            expected_box = as_box(expected_value.get("bbox_xyxy", expected_value)
                                  if isinstance(expected_value, dict) else expected_value)
            item = selected_items[pred_index]
            predicted_region = prediction_regions.get(str(item.get("region")))
            cx = (truth[gi][0] + truth[gi][2]) / 2
            cy = (truth[gi][1] + truth[gi][3]) / 2
            correct = (expected_box[0] <= cx < expected_box[2] and
                       expected_box[1] <= cy < expected_box[3] and
                       predicted_region is not None and point_in_region(predicted_region, cx, cy))
            region_assignment_count += 1
            region_assignment_errors += int(not correct)
    region_assignment_accuracy = (
        (region_assignment_count - region_assignment_errors) / region_assignment_count
        if region_assignment_count else None
    )

    review_reason_counts: dict[str, int] = {}
    for item in selected_items:
        if item.get("review_required", False):
            for reason in item.get("review_reasons", []):
                review_reason_counts[reason] = review_reason_counts.get(reason, 0) + 1
    for item in selected_excluded:
        if item.get("review_required", item.get("review", True)):
            reason = item.get("reason", "unspecified")
            review_reason_counts[reason] = review_reason_counts.get(reason, 0) + 1
    timing = manifest.get("timing_ms", {})
    runtime_ms = timing.get("total_before_manifest_finalize")

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
    review_items = len(review_prediction_indices) + sum(
        bool(item.get("review_required", item.get("review", True))) for item in selected_excluded
    )
    candidate_count = len(predicted) + len(selected_excluded)
    page_has_error = bool(
        len(truth) - len(matched_truth) or error_predictions or cut_truth or
        merged_predictions or split_truth or len(word_labels) - retained_words or
        region_assignment_errors or reading_order["pairwise_order_violations"]
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
        "ground_truth_words": len(word_labels),
        "retained_words": retained_words,
        "clipped_or_unassigned_words": len(word_labels) - retained_words,
        "word_retention_rate": round(word_retention_rate, 4) if word_retention_rate is not None else None,
        "word_results": word_results,
        "region_assignment_count": region_assignment_count,
        "region_assignment_errors": region_assignment_errors,
        "region_assignment_accuracy": (round(region_assignment_accuracy, 4)
                                        if region_assignment_accuracy is not None else None),
        "reading_order": reading_order,
        "review_reason_counts": review_reason_counts,
        "runtime_ms": runtime_ms,
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
    total_words = sum(page["ground_truth_words"] for page in pages)
    total_retained_words = sum(page["retained_words"] for page in pages)
    total_region_assignments = sum(page["region_assignment_count"] for page in pages)
    total_region_errors = sum(page["region_assignment_errors"] for page in pages)
    order_pairs = sum(page["reading_order"]["comparable_pairs"] for page in pages)
    concordant_order_pairs = sum(page["reading_order"]["concordant_pairs"] for page in pages)
    order_violations = sum(page["reading_order"]["pairwise_order_violations"] for page in pages)
    runtimes = [float(page["runtime_ms"]) for page in pages if page["runtime_ms"] is not None]
    sorted_runtimes = sorted(runtimes)
    review_reason_totals: dict[str, int] = {}
    for page in pages:
        for reason, count in page["review_reason_counts"].items():
            review_reason_totals[reason] = review_reason_totals.get(reason, 0) + count
    all_vertical_errors = [
        (match["top_boundary_error_px"], match["bottom_boundary_error_px"])
        for page in pages for match in page["matches"]
    ]
    error_pages = [page for page in pages if page["page_has_segmentation_error"]]
    reviewed_error_pages = sum(page["page_has_review_alert"] for page in error_pages)
    result = {
        "benchmark": "hand-labeled line-crop evaluation",
        "matching": f"greedy one-to-one IoU >= {threshold:.3f}; boxes are xyxy with exclusive right/bottom edges",
        "scope_note": "Only annotations and optional ROIs listed in the input are scored. Optional word, region, and reading-order metrics require independent annotations. Excluded review candidates are not counted as predicted crops.",
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
            "ground_truth_words": total_words,
            "retained_words": total_retained_words,
            "clipped_or_unassigned_words": total_words - total_retained_words,
            "word_retention_rate": round(total_retained_words / total_words, 4) if total_words else None,
            "region_assignments": total_region_assignments,
            "region_assignment_errors": total_region_errors,
            "region_assignment_accuracy": (round((total_region_assignments - total_region_errors) /
                                                  total_region_assignments, 4)
                                           if total_region_assignments else None),
            "reading_order_comparable_pairs": order_pairs,
            "reading_order_violations": order_violations,
            "reading_order_pairwise_accuracy": (round(concordant_order_pairs / order_pairs, 4)
                                                if order_pairs else None),
            "review_reason_counts": review_reason_totals,
            "pages_with_runtime": len(runtimes),
            "runtime_ms_mean": round(statistics.mean(runtimes), 3) if runtimes else None,
            "runtime_ms_median": round(statistics.median(runtimes), 3) if runtimes else None,
            "runtime_ms_p95": (round(sorted_runtimes[min(len(sorted_runtimes) - 1,
                                                       int((len(sorted_runtimes) - 1) * 0.95))], 3)
                               if runtimes else None),
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

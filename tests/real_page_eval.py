#!/usr/bin/env python3
"""Evaluate line-crop manifests against hand-labeled bounding boxes.

Annotations are kept separately from the public source tree when they contain
private or licensed page imagery. See README.md for the compact JSON schema.
"""
from __future__ import annotations

import argparse
import json
import math
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


def nearest_rank_percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    if not 0 < quantile <= 1:
        raise ValueError("quantile must be in (0, 1]")
    ordered = sorted(values)
    return ordered[math.ceil(quantile * len(ordered)) - 1]


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


def geometric_side_metrics(split_x: float | None, truth: list[Box], predicted: list[Box],
                           selected_items: list[dict[str, Any]], matches: list[dict[str, Any]],
                           selected_excluded: list[dict[str, Any]],
                           error_predictions: set[int], review_prediction_indices: set[int],
                           flagged_error_predictions: set[int]) -> dict[str, Any] | None:
    if split_x is None:
        return None
    side_of = lambda box: "left" if (box[0] + box[2]) / 2 < split_x else "right"
    matched_truth_to_pred = {int(match["truth_index"]): int(match["prediction_index"])
                             for match in matches}
    matched_pred_to_truth = {prediction: truth_index
                             for truth_index, prediction in matched_truth_to_pred.items()}
    cross_side_matches = sum(
        side_of(truth[truth_index]) != side_of(predicted[prediction])
        for truth_index, prediction in matched_truth_to_pred.items()
    )
    crossing_by_side = {"left": 0, "right": 0}
    for item, box in zip(selected_items, predicted):
        source = as_box(item.get("source_crop_bbox_xyxy", item["bbox_xyxy"]))
        if source[0] < split_x < source[2]:
            crossing_by_side[side_of(box)] += 1
    excluded_reviews = {"left": 0, "right": 0}
    for item in selected_excluded:
        if item.get("review_required", item.get("review", True)):
            excluded_reviews[side_of(as_box(item["bbox_xyxy"]))] += 1

    result: dict[str, Any] = {"split_x": split_x, "cross_side_matches": cross_side_matches, "sides": {}}
    for side in ("left", "right"):
        truth_indices = {index for index, box in enumerate(truth) if side_of(box) == side}
        prediction_indices = {index for index, box in enumerate(predicted) if side_of(box) == side}
        correct_truth = {
            truth_index for truth_index in truth_indices
            if truth_index in matched_truth_to_pred and
            side_of(predicted[matched_truth_to_pred[truth_index]]) == side
        }
        correct_predictions = {
            prediction for prediction in prediction_indices
            if prediction in matched_pred_to_truth and
            side_of(truth[matched_pred_to_truth[prediction]]) == side
        }
        side_errors = error_predictions & prediction_indices
        flagged_errors = flagged_error_predictions & prediction_indices
        result["sides"][side] = {
            "ground_truth_lines": len(truth_indices),
            "predicted_crops": len(prediction_indices),
            "true_positive_matches": len(correct_truth),
            "false_positive_crops": len(prediction_indices) - len(correct_predictions),
            "missed_lines": len(truth_indices) - len(correct_truth),
            "precision": round(len(correct_predictions) / len(prediction_indices), 4)
                        if prediction_indices else 0.0,
            "recall": round(len(correct_truth) / len(truth_indices), 4) if truth_indices else 0.0,
            "cross_side_matches": sum(
                truth_index in matched_truth_to_pred and
                side_of(predicted[matched_truth_to_pred[truth_index]]) != side
                for truth_index in truth_indices
            ),
            "gutter_crossing_crops": crossing_by_side[side],
            "review_candidates": sum(index in review_prediction_indices for index in prediction_indices) +
                                 excluded_reviews[side],
            "crop_error_candidates": len(side_errors),
            "reviewed_crop_errors": len(flagged_errors),
            "crop_error_review_recall": round(len(flagged_errors) / len(side_errors), 4)
                                         if side_errors else None,
        }
    return result


def aggregate_side_metrics(pages: list[dict[str, Any]]) -> dict[str, Any] | None:
    side_pages = [page["side_metrics"] for page in pages if page.get("side_metrics") is not None]
    if not side_pages:
        return None
    aggregate: dict[str, Any] = {"annotated_pages": len(side_pages), "sides": {}}
    for side in ("left", "right"):
        rows = [page["sides"][side] for page in side_pages]
        gt = sum(row["ground_truth_lines"] for row in rows)
        predictions = sum(row["predicted_crops"] for row in rows)
        tp = sum(row["true_positive_matches"] for row in rows)
        errors = sum(row["crop_error_candidates"] for row in rows)
        reviewed = sum(row["reviewed_crop_errors"] for row in rows)
        aggregate["sides"][side] = {
            "ground_truth_lines": gt, "predicted_crops": predictions,
            "true_positive_matches": tp,
            "false_positive_crops": sum(row["false_positive_crops"] for row in rows),
            "missed_lines": sum(row["missed_lines"] for row in rows),
            "precision": round(tp / predictions, 4) if predictions else 0.0,
            "recall": round(tp / gt, 4) if gt else 0.0,
            "cross_side_matches": sum(row["cross_side_matches"] for row in rows),
            "gutter_crossing_crops": sum(row["gutter_crossing_crops"] for row in rows),
            "review_candidates": sum(row["review_candidates"] for row in rows),
            "crop_error_candidates": errors, "reviewed_crop_errors": reviewed,
            "crop_error_review_recall": round(reviewed / errors, 4) if errors else None,
        }
    aggregate["cross_side_matches"] = sum(page["cross_side_matches"] for page in side_pages)
    return aggregate


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

    source_line_box_results: list[dict[str, Any]] = []
    source_line_coverage: list[float] = []
    source_full = source_left = source_right = source_top = source_bottom = 0
    for truth_index, truth_box in enumerate(truth):
        pred_index = matched_truth_to_pred.get(truth_index)
        if pred_index is None:
            source_box = None
            coverage = 0.0
        else:
            item = selected_items[pred_index]
            source_box = as_box(item.get("source_crop_bbox_xyxy", item["bbox_xyxy"]))
            coverage = intersection(source_box, truth_box) / area(truth_box)
        retained = source_box is not None and contains(source_box, truth_box)
        left_retained = source_box is not None and source_box[0] <= truth_box[0]
        right_retained = source_box is not None and source_box[2] >= truth_box[2]
        top_retained = source_box is not None and source_box[1] <= truth_box[1]
        bottom_retained = source_box is not None and source_box[3] >= truth_box[3]
        source_line_coverage.append(coverage)
        source_full += int(retained)
        source_left += int(left_retained)
        source_right += int(right_retained)
        source_top += int(top_retained)
        source_bottom += int(bottom_retained)
        source_line_box_results.append({
            "truth_index": truth_index, "prediction_index": pred_index,
            "source_crop_bbox_xyxy": list(source_box) if source_box is not None else None,
            "area_coverage": round(coverage, 4), "fully_retained": retained,
            "left_edge_retained": left_retained, "right_edge_retained": right_retained,
            "top_edge_retained": top_retained, "bottom_edge_retained": bottom_retained,
        })
    source_line_box_retention = {
        "annotated_line_boxes": len(truth), "matched_line_boxes": len(matched_truth),
        "fully_retained": source_full,
        "fully_retained_rate": round(source_full / len(truth), 4) if truth else None,
        "mean_area_coverage": round(sum(source_line_coverage) / len(truth), 4) if truth else None,
        "left_edge_retained": source_left, "right_edge_retained": source_right,
        "top_edge_retained": source_top, "bottom_edge_retained": source_bottom,
    }

    word_labels = page.get("ground_truth_words_xyxy", [])
    retained_words = 0
    word_results: list[dict[str, Any]] = []
    word_left_edges = word_right_edges = word_both_edges = 0
    word_horizontal_coverages: list[float] = []
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
        best_source: Box | None = None
        best_horizontal_coverage = 0.0
        for item in candidates:
            source = item.get("source_crop_bbox_xyxy", item["bbox_xyxy"])
            source_box = as_box(source)
            if contains(source_box, word_box):
                contained_by_crop = True
            horizontal_coverage = max(0.0, min(source_box[2], word_box[2]) -
                                      max(source_box[0], word_box[0])) / (word_box[2] - word_box[0])
            if best_source is None or horizontal_coverage > best_horizontal_coverage:
                best_source, best_horizontal_coverage = source_box, horizontal_coverage
        left_edge_retained = best_source is not None and best_source[0] <= word_box[0]
        right_edge_retained = best_source is not None and best_source[2] >= word_box[2]
        retained_words += int(contained_by_crop)
        word_results.append({"word_index": word_index, "line_index": line_index,
                             "retained_inside_matched_crop": contained_by_crop,
                             "left_edge_retained": left_edge_retained,
                             "right_edge_retained": right_edge_retained,
                             "horizontal_coverage": round(best_horizontal_coverage, 4)})
        word_left_edges += int(left_edge_retained)
        word_right_edges += int(right_edge_retained)
        word_both_edges += int(left_edge_retained and right_edge_retained)
        word_horizontal_coverages.append(best_horizontal_coverage)
    word_retention_rate = retained_words / len(word_labels) if word_labels else None
    word_edge_retention = {
        "labeled_words": len(word_labels), "fully_retained_words": retained_words,
        "left_edges_retained": word_left_edges, "right_edges_retained": word_right_edges,
        "both_edges_retained": word_both_edges,
        "left_edge_retention_rate": round(word_left_edges / len(word_labels), 4) if word_labels else None,
        "right_edge_retention_rate": round(word_right_edges / len(word_labels), 4) if word_labels else None,
        "both_edges_retention_rate": round(word_both_edges / len(word_labels), 4) if word_labels else None,
        "mean_horizontal_coverage": round(sum(word_horizontal_coverages) / len(word_labels), 4)
                                    if word_labels else None,
    }

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
    side_split_x = page.get("side_split_x")
    if side_split_x is not None:
        side_split_x = float(side_split_x)
        image_width = int(manifest.get("image_size", {}).get("width", 0))
        if not 0 < side_split_x < image_width:
            raise ValueError("side_split_x must lie inside the manifest image width")
    side_metrics = geometric_side_metrics(
        side_split_x, truth, predicted, selected_items, matches, selected_excluded,
        error_predictions, review_prediction_indices, flagged_error_predictions)
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
        "source_line_box_retention": source_line_box_retention,
        "source_line_box_results": source_line_box_results,
        "word_edge_retention": word_edge_retention,
        "word_results": word_results,
        "region_assignment_count": region_assignment_count,
        "region_assignment_errors": region_assignment_errors,
        "region_assignment_accuracy": (round(region_assignment_accuracy, 4)
                                        if region_assignment_accuracy is not None else None),
        "reading_order": reading_order,
        "review_reason_counts": review_reason_counts,
        "side_metrics": side_metrics,
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
    total_line_boxes = sum(page["source_line_box_retention"]["annotated_line_boxes"] for page in pages)
    total_source_line_boxes_retained = sum(page["source_line_box_retention"]["fully_retained"] for page in pages)
    total_source_area_coverage = sum(
        page["source_line_box_retention"]["mean_area_coverage"] *
        page["source_line_box_retention"]["annotated_line_boxes"]
        for page in pages if page["source_line_box_retention"]["mean_area_coverage"] is not None
    )
    total_word_left_edges = sum(page["word_edge_retention"]["left_edges_retained"] for page in pages)
    total_word_right_edges = sum(page["word_edge_retention"]["right_edges_retained"] for page in pages)
    total_word_both_edges = sum(page["word_edge_retention"]["both_edges_retained"] for page in pages)
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
    side_aggregates = aggregate_side_metrics(pages)
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
            "source_line_box_retention": {
                "annotated_line_boxes": total_line_boxes,
                "fully_retained": total_source_line_boxes_retained,
                "fully_retained_rate": (round(total_source_line_boxes_retained / total_line_boxes, 4)
                                         if total_line_boxes else None),
                "mean_area_coverage": (round(total_source_area_coverage / total_line_boxes, 4)
                                        if total_line_boxes else None),
                "left_edge_retained": sum(page["source_line_box_retention"]["left_edge_retained"] for page in pages),
                "right_edge_retained": sum(page["source_line_box_retention"]["right_edge_retained"] for page in pages),
                "top_edge_retained": sum(page["source_line_box_retention"]["top_edge_retained"] for page in pages),
                "bottom_edge_retained": sum(page["source_line_box_retention"]["bottom_edge_retained"] for page in pages),
            },
            "word_edge_retention": {
                "labeled_words": total_words, "fully_retained_words": total_retained_words,
                "left_edges_retained": total_word_left_edges,
                "right_edges_retained": total_word_right_edges,
                "both_edges_retained": total_word_both_edges,
                "left_edge_retention_rate": round(total_word_left_edges / total_words, 4) if total_words else None,
                "right_edge_retention_rate": round(total_word_right_edges / total_words, 4) if total_words else None,
                "both_edges_retention_rate": round(total_word_both_edges / total_words, 4) if total_words else None,
            },
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
            "runtime_ms_p95": (round(nearest_rank_percentile(runtimes, 0.95), 3)
                               if runtimes else None),
            "vertical_boundary": vertical_boundary_summary(all_vertical_errors),
            "side_metrics": side_aggregates,
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

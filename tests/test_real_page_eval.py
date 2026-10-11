#!/usr/bin/env python3
"""Unit tests for the hand-labeled line-crop evaluator."""
import json
import tempfile
import unittest
from pathlib import Path

from real_page_eval import evaluate_page


class RealPageEvaluatorTests(unittest.TestCase):
    def evaluate(self, predictions, truth, threshold=0.5):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"lines": [{"bbox_xyxy": b} for b in predictions]}),
                                encoding="utf-8")
            return evaluate_page({"manifest": "manifest.json", "ground_truth_xyxy": truth},
                                 root, threshold)

    def test_one_to_one_exact_lines(self):
        boxes = [[10, 10, 100, 30], [10, 40, 100, 60]]
        result = self.evaluate(boxes, boxes)
        self.assertEqual(result["true_positive_matches"], 2)
        self.assertEqual(result["precision"], 1.0)
        self.assertEqual(result["recall"], 1.0)
        self.assertEqual(result["missed_lines"], 0)
        self.assertEqual(result["vertical_boundary"]["mean_absolute_top_error_px"], 0.0)
        self.assertEqual(result["vertical_boundary"]["mean_absolute_bottom_error_px"], 0.0)

    def test_vertical_boundaries_distinguish_clipping_from_excess_whitespace(self):
        result = self.evaluate([[10, 12, 100, 28]], [[10, 10, 100, 30]])
        vertical = result["vertical_boundary"]
        self.assertEqual(vertical["mean_absolute_top_error_px"], 2.0)
        self.assertEqual(vertical["mean_absolute_bottom_error_px"], 2.0)
        self.assertEqual(vertical["top_clipped"], 1)
        self.assertEqual(vertical["bottom_clipped"], 1)
        self.assertEqual(vertical["top_excess_whitespace"], 0)
        self.assertEqual(vertical["bottom_excess_whitespace"], 0)

    def test_vertical_boundary_metrics_count_extra_margins_separately(self):
        result = self.evaluate([[10, 8, 100, 33]], [[10, 10, 100, 30]])
        vertical = result["vertical_boundary"]
        self.assertEqual(vertical["top_excess_whitespace"], 1)
        self.assertEqual(vertical["bottom_excess_whitespace"], 1)
        self.assertEqual(vertical["top_clipped"], 0)
        self.assertEqual(vertical["bottom_clipped"], 0)
        self.assertEqual(vertical["top_within_3px"], 1)
        self.assertEqual(vertical["bottom_within_3px"], 1)

    def test_horizontally_cut_line_is_counted(self):
        result = self.evaluate([[40, 10, 100, 30]], [[10, 10, 110, 30]])
        self.assertEqual(result["true_positive_matches"], 1)
        self.assertEqual(result["cut_lines"], 1)

    def test_one_merged_crop_cannot_match_two_lines(self):
        result = self.evaluate([[10, 10, 50, 30]],
                               [[10, 10, 30, 30], [30, 10, 50, 30]])
        self.assertEqual(result["true_positive_matches"], 1)
        self.assertEqual(result["missed_lines"], 1)
        self.assertEqual(result["merged_crops"], 1)

    def test_wide_crop_detects_narrow_indented_second_line(self):
        result = self.evaluate([[0, 0, 200, 44]],
                               [[0, 0, 200, 36], [0, 24, 80, 44]])
        self.assertEqual(result["true_positive_matches"], 1)
        self.assertEqual(result["missed_lines"], 1)
        self.assertEqual(result["merged_crops"], 1)
        self.assertEqual(result["crop_error_candidates"], 1)

    def test_two_fragments_are_counted_as_a_split_line(self):
        result = self.evaluate([[10, 10, 30, 30], [30, 10, 50, 30]],
                               [[10, 10, 50, 30]])
        self.assertEqual(result["true_positive_matches"], 1)
        self.assertEqual(result["false_positive_crops"], 1)
        self.assertEqual(result["split_lines"], 1)

    def test_review_rate_and_crop_error_recall_use_candidate_flags(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "lines": [
                    {"bbox_xyxy": [10, 10, 100, 30], "review_required": False},
                    {"bbox_xyxy": [200, 10, 260, 30], "review_required": True},
                ],
                "excluded": [{"bbox_xyxy": [300, 10, 320, 30], "review_required": True}],
            }), encoding="utf-8")
            result = evaluate_page({"manifest": "manifest.json",
                                    "ground_truth_xyxy": [[10, 10, 100, 30]]}, root, 0.5)
        self.assertEqual(result["review_candidates"], 2)
        self.assertEqual(result["candidate_count_including_excluded"], 3)
        self.assertAlmostEqual(result["review_candidate_rate"], 2 / 3, places=3)
        self.assertEqual(result["crop_error_candidates"], 1)
        self.assertEqual(result["reviewed_crop_errors"], 1)
        self.assertEqual(result["crop_error_review_recall"], 1.0)
        self.assertTrue(result["page_has_review_alert"])

    def test_missed_line_is_counted_in_page_level_error_recall(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({"lines": [
                {"bbox_xyxy": [10, 10, 100, 30], "review_required": True},
            ]}), encoding="utf-8")
            result = evaluate_page({"manifest": "manifest.json",
                                    "ground_truth_xyxy": [[10, 10, 100, 30],
                                                          [10, 40, 100, 60]]}, root, 0.5)
        self.assertEqual(result["missed_lines"], 1)
        self.assertTrue(result["page_has_segmentation_error"])
        self.assertTrue(result["page_has_review_alert"])

    def test_word_region_order_review_and_runtime_metrics(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = root / "manifest.json"
            manifest.write_text(json.dumps({
                "lines": [
                    {"bbox_xyxy": [10, 10, 30, 20], "source_crop_bbox_xyxy": [9, 9, 31, 21],
                     "region": 1, "review_required": True, "review_reasons": ["edge_uncertain"]},
                    {"bbox_xyxy": [10, 40, 30, 50], "source_crop_bbox_xyxy": [9, 39, 31, 51],
                     "region": 2, "review_required": False},
                ],
                "layout": {"regions": [
                    {"id": 1, "bbox_xyxy": [0, 0, 35, 30],
                     "left_edge_xy": [[8, 0], [12, 30]], "right_edge_xy": [[35, 0], [35, 30]]},
                    {"id": 2, "bbox_xyxy": [0, 30, 35, 60]},
                ]},
                "excluded": [], "timing_ms": {"total_before_manifest_finalize": 12.5},
            }), encoding="utf-8")
            result = evaluate_page({
                "manifest": "manifest.json",
                "ground_truth_xyxy": [[10, 10, 30, 20], [10, 40, 30, 50]],
                "ground_truth_words_xyxy": [
                    {"line_index": 0, "bbox_xyxy": [12, 12, 20, 18]},
                    {"line_index": 1, "bbox_xyxy": [10, 41, 40, 48]},
                ],
                "ground_truth_regions": {"body": [0, 0, 40, 60]},
                "ground_truth_region_ids": ["body", "body"],
                "ground_truth_order": [1, 0],
            }, root, 0.5)
        self.assertEqual(result["retained_words"], 1)
        self.assertEqual(result["clipped_or_unassigned_words"], 1)
        self.assertEqual(result["region_assignment_errors"], 0)
        self.assertEqual(result["region_assignment_accuracy"], 1.0)
        self.assertEqual(result["reading_order"]["pairwise_order_violations"], 1)
        self.assertEqual(result["reading_order"]["pairwise_order_accuracy"], 0.0)
        self.assertEqual(result["review_reason_counts"], {"edge_uncertain": 1})
        self.assertEqual(result["runtime_ms"], 12.5)

    def test_clean_unflagged_page_has_no_crop_error_denominator(self):
        result = self.evaluate([[10, 10, 100, 30]], [[10, 10, 100, 30]])
        self.assertEqual(result["review_candidate_rate"], 0.0)
        self.assertIsNone(result["crop_error_review_recall"])
        self.assertFalse(result["page_has_segmentation_error"])


if __name__ == "__main__":
    unittest.main()

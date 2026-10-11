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

    def test_clean_unflagged_page_has_no_crop_error_denominator(self):
        result = self.evaluate([[10, 10, 100, 30]], [[10, 10, 100, 30]])
        self.assertEqual(result["review_candidate_rate"], 0.0)
        self.assertIsNone(result["crop_error_review_recall"])
        self.assertFalse(result["page_has_segmentation_error"])


if __name__ == "__main__":
    unittest.main()

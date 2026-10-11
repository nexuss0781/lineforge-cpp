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

    def test_two_fragments_are_counted_as_a_split_line(self):
        result = self.evaluate([[10, 10, 30, 30], [30, 10, 50, 30]],
                               [[10, 10, 50, 30]])
        self.assertEqual(result["true_positive_matches"], 1)
        self.assertEqual(result["false_positive_crops"], 1)
        self.assertEqual(result["split_lines"], 1)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from PIL import Image

from scripts.lineforge_dataset import assign_pdf_shards, sha256_file
from scripts.lineforge_telegram_stage import (
    plan_telegram_jobs,
    prepare_manifest,
    rights_template,
    validate_fresh_selection,
)


class TelegramSelectionTests(unittest.TestCase):
    def test_selection_is_bound_to_fresh_channel_inventory_and_boolean_status(self) -> None:
        fresh = {
            "source_chat": "@et_bookstore",
            "files": [
                {"message_id": 11, "file_name": "one.pdf", "size_bytes": 100},
                {"message_id": 12, "file_name": "two.pdf", "size_bytes": 200},
            ],
        }
        reviewed = {
            "source_chat": "@et_bookstore",
            "files": [
                {"message_id": 11, "file_name": "one.pdf", "size_bytes": 100, "status": True, "duplicate": False},
                {"message_id": 12, "file_name": "two.pdf", "size_bytes": 200, "status": False, "duplicate": True},
            ],
        }
        chosen = validate_fresh_selection(fresh, reviewed, "@et_bookstore")
        self.assertEqual([row["message_id"] for row in chosen], [11])
        self.assertIs(chosen[0]["status"], True)
        self.assertEqual(chosen[0]["reviewed_sha256"], None)

    def test_stale_or_contradictory_selection_fails_closed(self) -> None:
        fresh = {"source_chat": "@et_bookstore", "files": [{"message_id": 11, "file_name": "one.pdf"}]}
        stale = {"source_chat": "@et_bookstore", "files": []}
        with self.assertRaisesRegex(ValueError, "differs from the reviewed selection"):
            validate_fresh_selection(fresh, stale, "@et_bookstore")
        contradictory = {
            "source_chat": "@et_bookstore",
            "files": [{"message_id": 11, "file_name": "one.pdf", "status": True, "duplicate": True}],
        }
        with self.assertRaisesRegex(ValueError, "known byte-identical copy"):
            validate_fresh_selection(fresh, contradictory, "@et_bookstore")

    def test_plan_splits_407_selected_pdfs_equally_and_matches_runtime_assignment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            selected_report = {
                "source_chat": "@et_bookstore",
                "files": [
                    {
                        "message_id": message_id,
                        "file_name": f"book-{message_id}.pdf",
                        "size_bytes": 1000 + message_id,
                        "sha256": None,
                        "status": message_id <= 407,
                        "duplicate": message_id > 407,
                    }
                    for message_id in range(1, 422)
                ],
            }
            report_path = root / "selection.json"
            report_path.write_text(json.dumps(selected_report), encoding="utf-8")
            plan_dir = root / "private-jobs"
            result = plan_telegram_jobs(report_path, plan_dir, "stable-plan-seed")

            self.assertEqual(result["selected_pdf_count"], 407)
            self.assertEqual(result["assigned_pdf_count"], 407)
            self.assertEqual(result["minimum_pdfs_per_job"], 21)
            self.assertEqual(result["maximum_pdfs_per_job"], 22)
            self.assertEqual(result["job_counts"].count(22), 8)
            self.assertEqual(result["job_counts"].count(21), 11)
            self.assertFalse(result["ready_for_processing"])
            self.assertEqual(plan_dir.stat().st_mode & 0o777, 0o700)

            planned_ids = []
            for shard_id in range(19):
                payload = json.loads((plan_dir / f"job-{shard_id:02d}.json").read_text(encoding="utf-8"))
                self.assertEqual(payload["shard_id"], shard_id)
                self.assertFalse(payload["ready_for_processing"])
                self.assertEqual(payload["run_seed"], "stable-plan-seed")
                self.assertEqual(len(payload["files"]), payload["pdf_count"])
                self.assertEqual((plan_dir / f"job-{shard_id:02d}.json").stat().st_mode & 0o777, 0o600)
                planned_ids.append({row["pdf_id"] for row in payload["files"]})

            staged_manifest_rows = [
                {"pdf_id": f"telegram-{message_id}", "pdf_sha256": f"{message_id:064x}", "page_count": 100}
                for message_id in range(1, 408)
            ]
            runtime_assignments = assign_pdf_shards(staged_manifest_rows, run_seed="stable-plan-seed")
            self.assertEqual(planned_ids, [{row["pdf_id"] for row in shard} for shard in runtime_assignments])

    def test_plan_rejects_selected_known_duplicate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            report_path = root / "selection.json"
            report_path.write_text(json.dumps({
                "source_chat": "@et_bookstore",
                "files": [{"message_id": 1, "file_name": "copy.pdf", "status": True, "duplicate": True}],
            }), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "byte-identical copy"):
                plan_telegram_jobs(report_path, root / "jobs", "seed")
            self.assertFalse((root / "jobs").exists())


class OfflineManifestPreparationTests(unittest.TestCase):
    def _make_pdf(self, path: Path, page_count: int = 12) -> None:
        pages = [Image.new("RGB", (160, 100), "white") for _ in range(page_count)]
        pages[0].save(path, "PDF", save_all=True, append_images=pages[1:], resolution=72)

    def test_rights_template_and_manifest_recheck_local_pdf_bytes_and_pages(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "downloads" / "one.pdf"
            source.parent.mkdir()
            self._make_pdf(source)
            digest = sha256_file(source)
            inventory = {
                "schema_version": 1,
                "source_chat": "@et_bookstore",
                "fresh_report_sha256": "a" * 64,
                "selection_report_sha256": "b" * 64,
                "files": [
                    {
                        "message_id": 11,
                        "file_name": "one.pdf",
                        "path": "downloads/one.pdf",
                        "sha256": digest,
                        "page_count": 12,
                        "byte_size": source.stat().st_size,
                        "status": True,
                        "duplicate": False,
                        "canonical_message_id": 11,
                    },
                    {
                        "message_id": 12,
                        "file_name": "copy.pdf",
                        "path": "downloads/one.pdf",
                        "sha256": digest,
                        "page_count": 12,
                        "byte_size": source.stat().st_size,
                        "status": True,
                        "duplicate": True,
                        "canonical_message_id": 11,
                    },
                ],
            }
            inventory_path = root / "telegram-inventory.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            rights_path = root / "rights-review.json"
            template_result = rights_template(inventory_path, rights_path)
            self.assertEqual(template_result["source_count"], 1)
            rights = json.loads(rights_path.read_text(encoding="utf-8"))
            rights["files"][0].update({
                "allowed": False,
                "source_license": "CC-BY-4.0",
                "dataset_license": "cc-by-4.0",
                "evidence_ref": "private-test-rights-record",
                "public_source_name": "Synthetic Geez Book",
            })
            rights_path.write_text(json.dumps(rights), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "explicit approval"):
                prepare_manifest(inventory_path, rights_path, root / "manifest.json")

            rights["files"][0]["allowed"] = True
            rights_path.write_text(json.dumps(rights), encoding="utf-8")
            result = prepare_manifest(inventory_path, rights_path, root / "manifest.json")
            self.assertEqual(result["eligible_sources"], 1)
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["files"][0]["pdf_sha256"], digest)
            self.assertEqual(manifest["files"][0]["public_source_name"], "Synthetic Geez Book")
            self.assertEqual(manifest["files"][0]["public_source_slug"], "synthetic-geez-book")
            self.assertEqual(manifest["files"][0]["redistribution"]["dataset_license"], "cc-by-4.0")

    def test_stale_rights_review_checksum_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "source.pdf"
            self._make_pdf(source)
            inventory = {
                "schema_version": 1,
                "source_chat": "@et_bookstore",
                "files": [{
                    "message_id": 1, "file_name": "source.pdf", "path": "source.pdf",
                    "sha256": sha256_file(source), "page_count": 12, "byte_size": source.stat().st_size,
                    "status": True, "duplicate": False,
                }],
            }
            inventory_path = root / "inventory.json"
            inventory_path.write_text(json.dumps(inventory), encoding="utf-8")
            rights_path = root / "rights.json"
            rights_path.write_text(json.dumps({"schema_version": 1, "inventory_sha256": "0" * 64, "files": []}), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "not bound to this exact"):
                prepare_manifest(inventory_path, rights_path, root / "manifest.json")


if __name__ == "__main__":
    unittest.main()

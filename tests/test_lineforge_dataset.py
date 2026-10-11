from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

from PIL import Image, ImageDraw, ImageFont

from scripts.lineforge_dataset import (
    dispatch_local_run,
    QuotaLedger,
    assign_pdf_shards,
    crop_acceptance_reason,
    middle_page_order,
    monitor_local_workers,
    require_hf_token,
    reduce_worker_results,
    sha256_file,
    validate_source_manifest,
)


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class HuggingFaceTokenTests(unittest.TestCase):
    def test_token_is_required_and_read_from_hf_token_environment_variable(self) -> None:
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "HF_TOKEN"):
                require_hf_token()
        with patch.dict("os.environ", {"HF_TOKEN": "  test-token-not-real  "}, clear=True):
            self.assertEqual(require_hf_token(), "test-token-not-real")


def make_crop(path: Path, kind: str = "text", tag: int = 0) -> None:
    image = Image.new("L", (420, 84), 255)
    draw = ImageDraw.Draw(image)
    if kind == "text":
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 28)
        except OSError:
            font = ImageFont.load_default()
        draw.text((12, 22), f"Line sample {tag} 012345", fill=15, font=font)
    elif kind == "rule":
        draw.rectangle((2, 39, 417, 42), fill=10)
    elif kind == "figure":
        draw.rectangle((45, 8, 370, 76), outline=20, width=3)
        for x in range(70, 360, 24):
            draw.line((x, 14, x + 18, 68), fill=20, width=2)
        draw.line((50, 45, 366, 45), fill=20, width=3)
    image.save(path, format="PNG")


def make_record(shard_id: int, page: int = 10, line_index: int = 1, pdf_hash: str | None = None) -> dict:
    return {
        "source_pdf_sha256": pdf_hash or digest(f"pdf-{shard_id}-{page}-{line_index}"),
        "source_pdf_id": f"private-pdf-{shard_id}",
        "page_number": page,
        "line_index": line_index,
        "shard_id": shard_id,
        "engine_version": "lineforge-1.0.1",
        "source_license": "CC0-1.0",
        "rights_evidence_ref": "private-fixture-rights-record",
    }


class ManifestTests(unittest.TestCase):
    def test_manifest_requires_status_rights_and_checksums(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            pdf = root / "sample.pdf"
            pdf.write_bytes(b"synthetic staged PDF bytes")
            base = {
                "schema_version": 1,
                "dataset_id": "private-fixture",
                "revision": "fixture-r1",
                "files": [{
                    "pdf_id": "doc-1",
                    "pdf_sha256": sha256_file(pdf),
                    "path": str(pdf),
                    "page_count": 30,
                    "byte_size": pdf.stat().st_size,
                    "status": True,
                    "redistribution": {"allowed": True, "license": "CC0-1.0", "evidence_ref": "fixture"},
                }],
            }
            self.assertEqual(len(validate_source_manifest(base, root)), 1)
            self.assertEqual(len(validate_source_manifest(base, root, allowed_root=root)), 1)
            with self.assertRaisesRegex(ValueError, "outside the approved source root"):
                validate_source_manifest(base, root, allowed_root=root / "different-root")
            bad_status = json.loads(json.dumps(base))
            bad_status["files"][0]["status"] = False
            with self.assertRaisesRegex(ValueError, "Status=true"):
                validate_source_manifest(bad_status, root)
            bad_rights = json.loads(json.dumps(base))
            bad_rights["files"][0]["redistribution"]["allowed"] = False
            with self.assertRaisesRegex(ValueError, "redistribute"):
                validate_source_manifest(bad_rights, root)
            bad_hash = json.loads(json.dumps(base))
            bad_hash["files"][0]["pdf_sha256"] = digest("wrong")
            with self.assertRaisesRegex(ValueError, "does not match"):
                validate_source_manifest(bad_hash, root)

    def test_short_pdfs_are_excluded_and_duplicate_pdf_bytes_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            short = root / "short.pdf"
            full = root / "full.pdf"
            short.write_bytes(b"short")
            full.write_bytes(b"full")
            def row(pdf: Path, pdf_id: str, pages: int) -> dict:
                return {
                    "pdf_id": pdf_id, "pdf_sha256": sha256_file(pdf), "path": str(pdf),
                    "page_count": pages, "status": True,
                    "redistribution": {"allowed": True, "license": "CC0-1.0", "evidence_ref": "fixture"},
                }
            doc = {"schema_version": 1, "dataset_id": "x", "revision": "r1", "files": [row(short, "short", 9), row(full, "full", 20)]}
            eligible = validate_source_manifest(doc, root)
            self.assertEqual([entry["pdf_id"] for entry in eligible], ["full"])
            dup = json.loads(json.dumps(doc))
            dup["files"][1]["pdf_sha256"] = dup["files"][0]["pdf_sha256"]
            with self.assertRaisesRegex(ValueError, "duplicate PDF content"):
                validate_source_manifest(dup, root, verify_files=False)

    def test_shards_cover_every_pdf_exactly_once_deterministically(self) -> None:
        entries = [{"pdf_id": f"pdf-{i}", "pdf_sha256": digest(f"pdf-{i}"), "page_count": 11 + i} for i in range(407)]
        first = assign_pdf_shards(entries)
        second = assign_pdf_shards(entries)
        self.assertEqual(first, second)
        flattened = [entry["pdf_sha256"] for shard in first for entry in shard]
        self.assertEqual(len(flattened), 407)
        self.assertEqual(len(set(flattened)), 407)
        self.assertEqual(len(first), 19)
        self.assertLess(max(sum(item["page_count"] for item in shard) for shard in first),
                        2 * min(sum(item["page_count"] for item in shard) for shard in first))

    def test_page_sampling_is_seeded_middle_only_unique_and_starts_at_ten(self) -> None:
        pdf_hash = digest("book")
        first = middle_page_order(100, "run-A", pdf_hash)
        self.assertEqual(first, middle_page_order(100, "run-A", pdf_hash))
        self.assertNotEqual(first, middle_page_order(100, "run-B", pdf_hash))
        self.assertEqual(len(first), len(set(first)))
        self.assertTrue(all(25 <= page <= 75 for page in first))
        self.assertTrue(all(page >= 10 for page in middle_page_order(12, "short-book", pdf_hash)))
        self.assertEqual(middle_page_order(9, "tiny-book", pdf_hash), [])


class CropFilterTests(unittest.TestCase):
    def test_text_like_line_passes_and_figure_strips_or_review_flags_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            text = root / "text.png"
            rule = root / "rule.png"
            figure = root / "figure.png"
            make_crop(text, "text")
            make_crop(rule, "rule")
            make_crop(figure, "figure")
            self.assertEqual(crop_acceptance_reason(text, {"review_required": False, "review_reasons": []}), (True, "accepted_text_like_crop"))
            accepted, reason = crop_acceptance_reason(rule, {"review_required": False, "review_reasons": []})
            self.assertFalse(accepted)
            self.assertIn(reason, {"horizontal_rule_or_figure_strip", "too_few_ink_rows"})
            self.assertFalse(crop_acceptance_reason(figure, {"review_required": False, "review_reasons": []})[0])
            self.assertEqual(crop_acceptance_reason(text, {"review_required": True, "review_reasons": ["uncertain"]})[1], "engine_review_required")


class QuotaAndReducerTests(unittest.TestCase):
    def test_global_unique_image_dedup_exact_quota_and_page_floor(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = QuotaLedger(root / "quota.sqlite", root / "dataset", 2, "run-1", digest("manifest"))
            first = root / "first.png"
            duplicate = root / "duplicate.png"
            second = root / "second.png"
            third = root / "third.png"
            make_crop(first, "text", 1)
            duplicate.write_bytes(first.read_bytes())
            make_crop(second, "text", 2)
            make_crop(third, "text", 3)
            self.assertEqual(ledger.accept(first, make_record(0)), "accepted")
            self.assertEqual(ledger.accept(duplicate, make_record(1)), "duplicate_image")
            self.assertEqual(ledger.accept(second, make_record(2)), "accepted")
            self.assertEqual(ledger.accept(third, make_record(3)), "target_reached")
            self.assertEqual(ledger.count(), 2)
            self.assertEqual(len({row["image_sha256"] for row in ledger.records()}), 2)
            with self.assertRaisesRegex(ValueError, "before page 10"):
                ledger.accept(third, make_record(3, page=9))

    def test_concurrent_workers_cannot_overshoot_exact_global_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ledger = QuotaLedger(root / "quota.sqlite", root / "dataset", 7, "run-concurrent", digest("manifest"))
            crops = []
            for index in range(19):
                path = root / f"line-{index}.png"
                make_crop(path, "text", index)
                crops.append(path)
            def add(index: int) -> str:
                return ledger.accept(crops[index], make_record(index))
            with ThreadPoolExecutor(max_workers=19) as pool:
                outcomes = list(pool.map(add, range(19)))
            self.assertEqual(outcomes.count("accepted"), 7)
            self.assertEqual(ledger.count(), 7)
            self.assertEqual(ledger.count(), len({row["image_sha256"] for row in ledger.records()}))

    def _write_worker_results(self, root: Path, ledger: QuotaLedger, manifest_sha: str, run_id: str, target: int) -> Path:
        results = root / "workers"
        results.mkdir(exist_ok=True)
        for shard in range(19):
            result = {
                "schema_version": 1,
                "run_id": run_id,
                "run_seed": "fixture-seed",
                "shard_id": shard,
                "worker_count": 19,
                "source_manifest_sha256": manifest_sha,
                "status": "completed",
                "target": target,
                "accepted_count": ledger.count_for_shard(shard),
                "global_live_count": ledger.count(),
                "pages_processed": 1,
                "pdfs_processed": 1,
                "failures": [],
            }
            (results / f"worker-{shard:02d}.json").write_text(json.dumps(result), encoding="utf-8")
        return results

    def test_reducer_requires_all_19_workers_common_identity_and_exact_provenance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_sha = digest("manifest")
            ledger = QuotaLedger(root / "quota.sqlite", root / "dataset", 2, "run-reduce", manifest_sha)
            for shard in (0, 1):
                crop = root / f"line-{shard}.png"
                make_crop(crop, "text", shard)
                self.assertEqual(ledger.accept(crop, make_record(shard, page=12 + shard)), "accepted")
            result_dir = self._write_worker_results(root, ledger, manifest_sha, "run-reduce", 2)
            monitored = monitor_local_workers(ledger, result_dir, poll_seconds=1, timeout_seconds=1)
            self.assertEqual(monitored, {"status": "target_reached", "image_count": 2, "worker_results": 19})
            final_path = root / "dataset-manifest.json"
            final = reduce_worker_results(result_dir, ledger, "run-reduce", manifest_sha, 2, final_path)
            self.assertEqual(final["image_count"], 2)
            self.assertFalse(final["publishable"])
            for image in final["images"]:
                self.assertGreaterEqual(image["page_number"], 10)
                self.assertTrue(image["source_pdf_sha256"])
                self.assertTrue(image["engine_version"])
                self.assertEqual(image["source_license"], "CC0-1.0")
                self.assertTrue(image["source_pdf_id"])
                self.assertEqual(image["rights_evidence_ref"], "private-fixture-rights-record")
                self.assertTrue((root / "dataset" / image["relative_path"]).is_file())
            self.assertTrue(final_path.is_file())

            (result_dir / "worker-18.json").unlink()
            with self.assertRaisesRegex(ValueError, "exactly 19"):
                reduce_worker_results(result_dir, ledger, "run-reduce", manifest_sha, 2, final_path)

    def test_reducer_rejects_duplicate_missing_malformed_and_mixed_input_workers(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_sha = digest("manifest")
            ledger = QuotaLedger(root / "quota.sqlite", root / "dataset", 1, "run-bad", manifest_sha)
            crop = root / "line.png"
            make_crop(crop, "text", 42)
            ledger.accept(crop, make_record(0, page=10))
            result_dir = self._write_worker_results(root, ledger, manifest_sha, "run-bad", 1)
            data = json.loads((result_dir / "worker-18.json").read_text())
            data["shard_id"] = 0
            (result_dir / "worker-18.json").write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "duplicate worker result"):
                reduce_worker_results(result_dir, ledger, "run-bad", manifest_sha, 1, root / "out.json")
            data["shard_id"] = 18
            data["source_manifest_sha256"] = digest("other")
            (result_dir / "worker-18.json").write_text(json.dumps(data))
            with self.assertRaisesRegex(ValueError, "different run or input identities"):
                reduce_worker_results(result_dir, ledger, "run-bad", manifest_sha, 1, root / "out.json")
            (result_dir / "worker-18.json").write_text("not json")
            with self.assertRaisesRegex(ValueError, "cannot read valid JSON"):
                reduce_worker_results(result_dir, ledger, "run-bad", manifest_sha, 1, root / "out.json")

    def test_reducer_rejects_incomplete_dataset_below_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_sha = digest("manifest")
            ledger = QuotaLedger(root / "quota.sqlite", root / "dataset", 2, "run-short", manifest_sha)
            crop = root / "line.png"
            make_crop(crop, "text", 8)
            ledger.accept(crop, make_record(0, page=10))
            result_dir = self._write_worker_results(root, ledger, manifest_sha, "run-short", 2)
            with self.assertRaisesRegex(ValueError, "exact target not reached"):
                reduce_worker_results(result_dir, ledger, "run-short", manifest_sha, 2, root / "out.json")

    def test_reducer_rejects_failed_worker_and_inconsistent_seed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            manifest_sha = digest("manifest")
            ledger = QuotaLedger(root / "quota.sqlite", root / "dataset", 1, "run-health", manifest_sha)
            crop = root / "line.png"
            make_crop(crop, "text", 13)
            ledger.accept(crop, make_record(0, page=10))
            result_dir = self._write_worker_results(root, ledger, manifest_sha, "run-health", 1)
            worker_path = result_dir / "worker-03.json"
            worker = json.loads(worker_path.read_text(encoding="utf-8"))
            worker["status"] = "failed"
            worker_path.write_text(json.dumps(worker), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "did not complete successfully"):
                reduce_worker_results(result_dir, ledger, "run-health", manifest_sha, 1, root / "out.json")
            worker["status"] = "completed"
            worker["run_seed"] = "different-seed"
            worker_path.write_text(json.dumps(worker), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different deterministic run seeds"):
                reduce_worker_results(result_dir, ledger, "run-health", manifest_sha, 1, root / "out.json")


class DispatcherIntegrationTests(unittest.TestCase):
    def test_dispatch_starts_19_workers_and_coordinator_and_collects_exact_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "private-source"
            source_root.mkdir()
            pages = []
            for page_number in range(1, 13):
                page = Image.new("RGB", (300, 200), "white")
                ImageDraw.Draw(page).text((20, 40), f"Synthetic page {page_number}", fill="black")
                pages.append(page)
            pdf_path = source_root / "sample.pdf"
            pages[0].save(pdf_path, "PDF", save_all=True, append_images=pages[1:], resolution=100)
            manifest = {
                "schema_version": 1,
                "dataset_id": "private-integration-fixture",
                "revision": "fixture-r1",
                "files": [{
                    "pdf_id": "private-book-1",
                    "pdf_sha256": sha256_file(pdf_path),
                    "path": "sample.pdf",
                    "page_count": 12,
                    "status": True,
                    "redistribution": {
                        "allowed": True,
                        "license": "CC0-1.0",
                        "evidence_ref": "private-fixture-rights-record",
                    },
                }],
            }
            manifest_path = source_root / "manifest.json"
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            fake_linecrop = root / "fake-linecrop"
            fake_linecrop.write_text(
                f"#!{sys.executable}\n"
                "import argparse, json\n"
                "from pathlib import Path\n"
                "from PIL import Image, ImageDraw\n"
                "p=argparse.ArgumentParser()\n"
                "p.add_argument('--input'); p.add_argument('--output'); p.add_argument('--layout')\n"
                "a=p.parse_args()\n"
                "out=Path(a.output); out.mkdir(parents=True, exist_ok=True)\n"
                "page=int(Path(a.input).parent.name.split('-')[-1])\n"
                "im=Image.new('RGB',(420,84),'white'); d=ImageDraw.Draw(im)\n"
                "for y in range(20,62,5):\n"
                "  for x in range(10+page%11,400,19): d.line((x,y,x+7,y+2),fill='black',width=2)\n"
                "im.save(out/'line_0001.png')\n"
                "(out/'manifest.json').write_text(json.dumps({'version':'test-lineforge','lines':[{'file':'line_0001.png','review_required':False,'review_reasons':[]}] }))\n",
                encoding="utf-8",
            )
            fake_linecrop.chmod(0o755)
            run_dir = root / "run-1"
            result = dispatch_local_run(
                input_manifest=manifest_path,
                source_root=source_root,
                run_dir=run_dir,
                run_seed="integration-seed",
                run_id="integration-run-1",
                linecrop_binary=fake_linecrop,
                target=2,
                poll_seconds=1,
                timeout_seconds=90,
            )
            self.assertEqual(result["worker_count"], 19)
            self.assertEqual(result["coordinator_count"], 1)
            self.assertEqual(result["image_count"], 2)
            self.assertFalse(result["publishable"])
            self.assertEqual(len(list((run_dir / "dataset" / "workers").glob("worker-*.json"))), 19)
            self.assertTrue((run_dir / "dataset-manifest.json").is_file())
            with self.assertRaises(FileExistsError):
                dispatch_local_run(
                    manifest_path, source_root, run_dir, "integration-seed", "integration-run-2",
                    fake_linecrop, target=2, poll_seconds=1, timeout_seconds=5,
                )


if __name__ == "__main__":
    unittest.main()

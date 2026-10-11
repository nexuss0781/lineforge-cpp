from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image, ImageDraw

from scripts.lineforge_dataset import QuotaLedger, reduce_worker_results, sha256_file
from scripts.lineforge_hf_publish import HF_DATASET_REPO, publish_dataset

LICENSE = "cc-by-4.0"


def digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def make_fixture(root: Path, *, source_dataset_license: str | None = LICENSE, target: int = 2) -> dict:
    source_root = root / "private-source"
    source_root.mkdir()
    source_pdf = source_root / "source.pdf"
    source_pdf.write_bytes(b"synthetic PDF bytes used only by an offline publisher test")
    source_hash = sha256_file(source_pdf)
    rights = {
        "allowed": True,
        "license": "CC-BY-4.0",
        "evidence_ref": "private-fixture-rights-evidence",
    }
    if source_dataset_license is not None:
        rights["dataset_license"] = source_dataset_license
    source_document = {
        "schema_version": 1,
        "dataset_id": "private-fixture",
        "revision": "fixture-r1",
        "files": [{
            "pdf_id": "private-fixture-source-id",
            "pdf_sha256": source_hash,
            "path": "source.pdf",
            "page_count": 30,
            "status": True,
            "redistribution": rights,
        }],
    }
    source_manifest = source_root / "manifest.json"
    source_manifest.write_text(json.dumps(source_document), encoding="utf-8")
    source_manifest_sha = sha256_file(source_manifest)

    run_id = "offline-fixture-run"
    run_dir = root / "output" / run_id
    run_dir.mkdir(parents=True)
    dataset_dir = run_dir / "dataset"
    ledger = QuotaLedger(run_dir / "quota.sqlite", dataset_dir, target, run_id, source_manifest_sha)
    for index in range(target):
        image_path = root / f"crop-{index}.png"
        image = Image.new("L", (240, 48), 255)
        draw = ImageDraw.Draw(image)
        draw.line((8, 12 + index, 220, 32 - index), fill=0, width=3)
        draw.line((24 + index * 7, 16, 80 + index * 9, 30), fill=20, width=2)
        image.save(image_path, format="PNG")
        ledger.accept(image_path, {
            "source_pdf_sha256": source_hash,
            "source_pdf_id": "private-fixture-source-id",
            "page_number": 12 + index,
            "line_index": index + 1,
            "shard_id": index,
            "engine_version": "lineforge-1.0.1",
            "source_license": "CC-BY-4.0",
            "rights_evidence_ref": "private-fixture-rights-evidence",
        })

    source_manifest_digest = source_manifest_sha
    workers_dir = dataset_dir / "workers"
    workers_dir.mkdir()
    for shard in range(19):
        count = ledger.count_for_shard(shard)
        result = {
            "schema_version": 1,
            "run_id": run_id,
            "run_seed": "synthetic-offline-seed",
            "shard_id": shard,
            "worker_count": 19,
            "source_manifest_sha256": source_manifest_digest,
            "status": "completed",
            "target": target,
            "accepted_count": count,
            "global_live_count": target,
            "pages_processed": 1,
            "pdfs_processed": 1,
            "failures": [],
        }
        (workers_dir / f"worker-{shard:02d}.json").write_text(json.dumps(result), encoding="utf-8")

    dataset_manifest = run_dir / "dataset-manifest.json"
    reduced = reduce_worker_results(
        workers_dir, ledger, run_id, source_manifest_digest, target, dataset_manifest
    )
    (run_dir / "dispatch-summary.json").write_text(json.dumps({
        "run_id": run_id,
        "worker_count": 19,
        "coordinator_count": 1,
        "source_manifest_sha256": source_manifest_digest,
        "target_unique_images": target,
        "image_count": target,
        "publishable": False,
        "run_dir": str(run_dir.resolve()),
        "dataset_manifest": str(dataset_manifest.resolve()),
        "image_directory": str((dataset_dir / "images").resolve()),
    }), encoding="utf-8")
    card_template = root / "dataset-card.template.md"
    card_template.write_text(
        "---\nlicense: {{DATASET_LICENSE}}\npretty_name: Geez OCR Dataset\n---\n\n"
        "A LineForge line-crop image dataset.\n",
        encoding="utf-8",
    )
    return {
        "run_dir": run_dir,
        "source_root": source_root,
        "source_manifest": source_manifest,
        "card_template": card_template,
        "dataset_manifest": reduced,
        "source_hash": source_hash,
        "target": target,
    }


class FakeHfApi:
    def __init__(self, *, private: bool = False):
        self.private = private
        self.repo_info_calls = []
        self.upload_calls = []

    def repo_info(self, **kwargs):
        self.repo_info_calls.append(kwargs)
        return SimpleNamespace(private=self.private)

    def upload_folder(self, **kwargs):
        self.upload_calls.append(kwargs)
        payload = Path(kwargs["folder_path"])
        files = sorted(path.relative_to(payload).as_posix() for path in payload.rglob("*") if path.is_file())
        self.uploaded_files = files
        self.readme = (payload / "README.md").read_text(encoding="utf-8")
        self.public_index = json.loads((payload / "lineforge-public-manifest.json").read_text(encoding="utf-8"))


class HuggingFacePublisherTests(unittest.TestCase):
    def publish_args(self, fixture: dict) -> tuple:
        return (
            fixture["run_dir"],
            fixture["source_manifest"],
            fixture["source_root"],
            HF_DATASET_REPO,
            LICENSE,
            fixture["card_template"],
        )

    def test_offline_dry_run_checks_full_payload_without_token_or_network(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {}, clear=True):
            fixture = make_fixture(Path(temp))
            api = FakeHfApi()
            result = publish_dataset(*self.publish_args(fixture), expected_target=2, dry_run=True, api=api)
            self.assertEqual(result["status"], "dry_run_validated")
            self.assertEqual(result["image_count"], 2)
            self.assertEqual(result["file_count"], 4)
            self.assertEqual(api.repo_info_calls, [])
            self.assertEqual(api.upload_calls, [])

    def test_offline_stub_upload_contains_only_public_card_index_and_verified_images(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = make_fixture(Path(temp))
            api = FakeHfApi()
            result = publish_dataset(
                *self.publish_args(fixture),
                expected_target=2,
                confirm_publish=True,
                token="dummy-test-token-only",
                api=api,
            )
            self.assertEqual(result["status"], "uploaded")
            self.assertEqual(api.repo_info_calls, [{"repo_id": HF_DATASET_REPO, "repo_type": "dataset"}])
            self.assertEqual(len(api.upload_calls), 1)
            self.assertEqual(len(api.uploaded_files), 4)
            self.assertIn("license: cc-by-4.0", api.readme)
            self.assertNotIn("private-fixture-source-id", api.readme)
            self.assertNotIn("private-fixture-rights-evidence", api.readme)
            serialized_index = json.dumps(api.public_index)
            self.assertEqual(api.public_index["image_count"], 2)
            self.assertNotIn("private-fixture-source-id", serialized_index)
            self.assertNotIn("private-fixture-rights-evidence", serialized_index)
            self.assertNotIn(fixture["source_hash"], serialized_index)
            self.assertFalse(any("quota.sqlite" in path or "worker-" in path for path in api.uploaded_files))

    def test_remote_upload_requires_explicit_flag_before_secret_or_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {}, clear=True):
            fixture = make_fixture(Path(temp))
            api = FakeHfApi()
            with self.assertRaisesRegex(RuntimeError, "explicit --confirm-publish"):
                publish_dataset(*self.publish_args(fixture), expected_target=2, api=api)
            self.assertEqual(api.repo_info_calls, [])
            self.assertEqual(api.upload_calls, [])

    def test_confirmed_upload_still_requires_hf_token_before_network_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp, patch.dict("os.environ", {}, clear=True):
            fixture = make_fixture(Path(temp))
            api = FakeHfApi()
            with self.assertRaisesRegex(RuntimeError, "HF_TOKEN"):
                publish_dataset(
                    *self.publish_args(fixture), expected_target=2,
                    confirm_publish=True, api=api,
                )
            self.assertEqual(api.repo_info_calls, [])
            self.assertEqual(api.upload_calls, [])

    def test_dataset_license_requires_explicit_per_source_clearance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = make_fixture(Path(temp), source_dataset_license=None)
            api = FakeHfApi()
            with self.assertRaisesRegex(ValueError, "explicitly clear the selected dataset license"):
                publish_dataset(*self.publish_args(fixture), expected_target=2, dry_run=True, api=api)
            self.assertEqual(api.upload_calls, [])

    def test_mismatched_dataset_license_fails_before_remote_calls(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = make_fixture(Path(temp), source_dataset_license="cc0-1.0")
            api = FakeHfApi()
            with self.assertRaisesRegex(ValueError, "explicitly clear the selected dataset license"):
                publish_dataset(*self.publish_args(fixture), expected_target=2, dry_run=True, api=api)
            self.assertEqual(api.repo_info_calls, [])
            self.assertEqual(api.upload_calls, [])

    def test_image_tampering_and_missing_quota_ledger_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = make_fixture(Path(temp))
            first_image = fixture["run_dir"] / "dataset" / fixture["dataset_manifest"]["images"][0]["relative_path"]
            first_image.write_bytes(b"tampered")
            with self.assertRaisesRegex(ValueError, "checksum"):
                publish_dataset(*self.publish_args(fixture), expected_target=2, dry_run=True)

        with tempfile.TemporaryDirectory() as temp:
            fixture = make_fixture(Path(temp))
            (fixture["run_dir"] / "quota.sqlite").unlink()
            with self.assertRaisesRegex(ValueError, "quota ledger"):
                publish_dataset(*self.publish_args(fixture), expected_target=2, dry_run=True)

    def test_tampered_worker_identity_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = make_fixture(Path(temp))
            worker_path = fixture["run_dir"] / "dataset" / "workers" / "worker-03.json"
            worker = json.loads(worker_path.read_text(encoding="utf-8"))
            worker["shard_id"] = 2
            worker_path.write_text(json.dumps(worker), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "filenames and shard identities"):
                publish_dataset(*self.publish_args(fixture), expected_target=2, dry_run=True)

    def test_non_public_repository_is_not_changed_or_uploaded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = make_fixture(Path(temp))
            api = FakeHfApi(private=True)
            with self.assertRaisesRegex(RuntimeError, "already exist and be public"):
                publish_dataset(
                    *self.publish_args(fixture), expected_target=2,
                    confirm_publish=True, token="dummy-test-token-only", api=api,
                )
            self.assertEqual(api.repo_info_calls, [{"repo_id": HF_DATASET_REPO, "repo_type": "dataset"}])
            self.assertEqual(api.upload_calls, [])

    def test_wrong_target_repo_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            fixture = make_fixture(Path(temp))
            with self.assertRaisesRegex(ValueError, "restricted"):
                publish_dataset(
                    fixture["run_dir"], fixture["source_manifest"], fixture["source_root"],
                    "someone/other-dataset", LICENSE, fixture["card_template"],
                    expected_target=2, dry_run=True,
                )


if __name__ == "__main__":
    unittest.main()

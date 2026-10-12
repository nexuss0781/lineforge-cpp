from __future__ import annotations

import asyncio
import hashlib
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.lineforge_telegram_fetch import fetch_with_client, load_reviewed_sources


PDF_BYTES = b"%PDF-1.7\nsynthetic fixture for telegram fetch test\n"
PDF_SHA256 = hashlib.sha256(PDF_BYTES).hexdigest()


def source_row(message_id: int, *, digest: str = PDF_SHA256, allowed: bool = True) -> dict:
    return {
        "pdf_id": f"telegram-{message_id}",
        "pdf_sha256": digest,
        "path": f"old-local-path/{message_id}.pdf",
        "page_count": 12,
        "byte_size": len(PDF_BYTES),
        "status": True,
        "public_source_name": f"Synthetic Book {message_id}",
        "public_source_slug": f"synthetic-book-{message_id}",
        "redistribution": {
            "allowed": allowed,
            "license": "Synthetic test source license",
            "evidence_ref": "private-test-rights-record",
            "dataset_license": "cc-by-4.0",
        },
    }


def reviewed_document(*rows: dict) -> dict:
    return {
        "schema_version": 1,
        "dataset_id": "synthetic-test-set",
        "revision": "reviewed-fixture-revision",
        "source_chat": "@et_bookstore",
        "files": list(rows),
    }


class FakeTelegramClient:
    def __init__(self, payload_by_id: dict[int, bytes]) -> None:
        self.payload_by_id = payload_by_id
        self.lookups: list[int] = []
        self.downloads: list[int] = []

    async def get_messages(self, chat: str, *, ids: int):
        self.lookups.append(ids)
        payload = self.payload_by_id[ids]
        return types.SimpleNamespace(
            id=ids,
            document=object(),
            file=types.SimpleNamespace(
                name=f"book-{ids}.pdf",
                mime_type="application/pdf",
                size=len(payload),
            ),
        )

    async def download_media(self, message, *, file: str):
        self.downloads.append(message.id)
        Path(file).write_bytes(self.payload_by_id[message.id])
        return file


class TelegramRuntimeFetchTests(unittest.TestCase):
    def test_source_selection_requires_rights_and_exact_407_count_before_download(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "reviewed-manifest.json"
            path.write_text(json.dumps(reviewed_document(source_row(1))), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "expects exactly 407"):
                load_reviewed_sources(path, expected_pdf_count=407)

            denied = source_row(1, allowed=False)
            path.write_text(json.dumps(reviewed_document(denied)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "explicit approval"):
                load_reviewed_sources(path)

    def test_duplicate_pdf_content_fails_closed_before_telegram_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "reviewed-manifest.json"
            path.write_text(
                json.dumps(reviewed_document(source_row(1), source_row(2))), encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate PDF content SHA-256"):
                load_reviewed_sources(path)

    def test_publication_requires_one_exact_dataset_license_before_telegram_access(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = root / "reviewed-manifest.json"
            first = source_row(1)
            second = source_row(2, digest=hashlib.sha256(b"other PDF bytes").hexdigest())
            second["redistribution"]["dataset_license"] = "cc0-1.0"
            path.write_text(json.dumps(reviewed_document(first, second)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "same dataset license"):
                load_reviewed_sources(path, require_dataset_license=True)

            first["redistribution"].pop("dataset_license")
            path.write_text(json.dumps(reviewed_document(first)), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no reviewed dataset_license"):
                load_reviewed_sources(path, require_dataset_license=True)

    def test_downloads_only_reviewed_unique_messages_and_writes_runtime_manifest(self) -> None:
        payloads = {
            17: PDF_BYTES,
            18: b"%PDF-1.7\nsecond synthetic fixture\n",
        }
        rows = [
            source_row(17, digest=hashlib.sha256(payloads[17]).hexdigest()),
            source_row(18, digest=hashlib.sha256(payloads[18]).hexdigest()),
        ]
        for row, message_id in zip(rows, (17, 18)):
            row["byte_size"] = len(payloads[message_id])
        document = reviewed_document(*rows)

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            reviewed_path = root / "reviewed-manifest.json"
            reviewed_path.write_text(json.dumps(document), encoding="utf-8")
            validated_document, validated_rows = load_reviewed_sources(reviewed_path)
            output_root = root / "runtime-source"
            client = FakeTelegramClient(payloads)
            with patch("scripts.lineforge_telegram_fetch._pdf_page_count", return_value=12):
                result = asyncio.run(fetch_with_client(client, validated_document, validated_rows, output_root))

            self.assertEqual(sorted(client.lookups), [17, 18])
            self.assertEqual(sorted(client.downloads), [17, 18])
            self.assertEqual(result["selected_pdf_count"], 2)
            self.assertEqual(result["downloaded_pdf_count"], 2)
            self.assertEqual(result["unique_pdf_count"], 2)
            runtime = json.loads((output_root / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(len(runtime["files"]), 2)
            self.assertEqual({row["pdf_id"] for row in runtime["files"]}, {"telegram-17", "telegram-18"})
            for entry in runtime["files"]:
                pdf_path = output_root / entry["path"]
                self.assertTrue(pdf_path.is_file())
                self.assertEqual(hashlib.sha256(pdf_path.read_bytes()).hexdigest(), entry["pdf_sha256"])
                self.assertTrue(entry["redistribution"]["allowed"])
            self.assertEqual(output_root.stat().st_mode & 0o777, 0o700)
            self.assertEqual((output_root / "manifest.json").stat().st_mode & 0o777, 0o600)

    def test_checksum_or_page_count_change_aborts_without_runtime_manifest(self) -> None:
        cases = (
            ("checksum", "0" * 64, 12, "checksum differs"),
            ("page-count", PDF_SHA256, 11, "page count differs"),
        )
        for case_name, digest, observed_pages, expected_error in cases:
            with self.subTest(case=case_name), tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                row = source_row(31, digest=digest)
                row["byte_size"] = len(PDF_BYTES)
                document = reviewed_document(row)
                reviewed_path = root / "reviewed-manifest.json"
                reviewed_path.write_text(json.dumps(document), encoding="utf-8")
                validated_document, validated_rows = load_reviewed_sources(reviewed_path)
                output_root = root / "runtime-source"
                client = FakeTelegramClient({31: PDF_BYTES})
                with patch("scripts.lineforge_telegram_fetch._pdf_page_count", return_value=observed_pages):
                    with self.assertRaisesRegex(ValueError, expected_error):
                        asyncio.run(fetch_with_client(client, validated_document, validated_rows, output_root))
                self.assertFalse((output_root / "manifest.json").exists())
                self.assertEqual(list((output_root / "pdfs").glob("*.partial")), [])


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import hashlib
import json
import sys
import tempfile
import unittest
from pathlib import Path

from PIL import Image, ImageDraw

from scripts.lineforge_dataset import sha256_file
from scripts.lineforge_pilot import run_one_job_pilot


class OneJobPilotTests(unittest.TestCase):
    def _make_source(self, source_root: Path, page_count: int = 10) -> Path:
        pages = []
        for page_number in range(1, page_count + 1):
            page = Image.new("RGB", (520, 180), "white")
            if page_number == 10:
                ImageDraw.Draw(page).text((20, 55), "Synthetic one-line pilot 012345", fill="black")
            pages.append(page)
        pdf_path = source_root / "synthetic-pilot.pdf"
        pages[0].save(pdf_path, "PDF", save_all=True, append_images=pages[1:], resolution=100)
        manifest = {
            "schema_version": 1,
            "dataset_id": "lineforge-synthetic-one-image-pilot",
            "revision": "synthetic-fixture-r1",
            "files": [{
                "pdf_id": "synthetic-only-source",
                "pdf_sha256": sha256_file(pdf_path),
                "path": pdf_path.name,
                "page_count": page_count,
                "byte_size": pdf_path.stat().st_size,
                "status": True,
                "public_source_name": "Synthetic Pilot Book",
                "public_source_slug": "synthetic-pilot-book",
                "redistribution": {
                    "allowed": True,
                    "license": "CC0-1.0",
                    "evidence_ref": "generated-synthetic-fixture-no-third-party-source",
                },
            }],
        }
        manifest_path = source_root / "manifest.json"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path

    def _make_fake_linecrop(self, path: Path, invocation_log: Path) -> None:
        path.write_text(
            f"#!{sys.executable}\n"
            "import argparse, json\n"
            "from pathlib import Path\n"
            "from PIL import Image, ImageDraw, ImageFont\n"
            "p=argparse.ArgumentParser()\n"
            "p.add_argument('--input'); p.add_argument('--output'); p.add_argument('--layout')\n"
            "a=p.parse_args()\n"
            f"Path({str(invocation_log)!r}).open('a').write('called\\n')\n"
            "out=Path(a.output); out.mkdir(parents=True, exist_ok=True)\n"
            "im=Image.new('L',(420,84),255)\n"
            "try: font=ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',28)\n"
            "except OSError: font=ImageFont.load_default()\n"
            "ImageDraw.Draw(im).text((12,20),'Synthetic pilot line 012345',fill=0,font=font)\n"
            "im.save(out/'line_0001.png')\n"
            "(out/'manifest.json').write_text(json.dumps({'version':'test-lineforge','lines':[{'file':'line_0001.png','review_required':False,'review_reasons':[]}]}))\n",
            encoding="utf-8",
        )
        path.chmod(0o755)

    def test_one_job_accepts_exactly_one_image_and_writes_nonpublishable_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "private-source"
            source_root.mkdir()
            manifest_path = self._make_source(source_root)
            fake_linecrop = root / "fake-linecrop"
            invocation_log = root / "linecrop-invocations.txt"
            self._make_fake_linecrop(fake_linecrop, invocation_log)
            run_dir = root / "pilot-run"

            summary = run_one_job_pilot(
                manifest_path, source_root, run_dir, "synthetic-pilot-r1", fake_linecrop,
            )

            self.assertEqual(summary["status"], "completed")
            self.assertEqual(summary["job_count"], 1)
            self.assertEqual(summary["linecrop_invocations"], 1)
            self.assertEqual(summary["rendered_pages"], 1)
            self.assertEqual(summary["page_number"], 10)
            self.assertEqual(summary["target_unique_images"], 1)
            self.assertEqual(summary["image_count"], 1)
            self.assertFalse(summary["publishable"])
            self.assertEqual(invocation_log.read_text(encoding="utf-8").splitlines(), ["called"])

            manifest = json.loads(Path(summary["dataset_manifest"]).read_text(encoding="utf-8"))
            self.assertTrue(manifest["pilot_only"])
            self.assertFalse(manifest["publishable"])
            self.assertEqual(manifest["job_count"], 1)
            self.assertEqual(len(manifest["images"]), 1)
            image = run_dir / "dataset" / manifest["images"][0]["relative_path"]
            self.assertTrue(image.is_file())
            self.assertEqual(hashlib.sha256(image.read_bytes()).hexdigest(), summary["image_sha256"])

    def test_refuses_multiple_source_pdfs_before_creating_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "private-source"
            source_root.mkdir()
            manifest_path = self._make_source(source_root)
            document = json.loads(manifest_path.read_text(encoding="utf-8"))
            second = dict(document["files"][0])
            second.update(pdf_id="another-source", pdf_sha256=hashlib.sha256(b"other").hexdigest())
            document["files"].append(second)
            manifest_path.write_text(json.dumps(document), encoding="utf-8")
            fake_linecrop = root / "unused-linecrop"
            self._make_fake_linecrop(fake_linecrop, root / "invocations")
            run_dir = root / "pilot-run"

            with self.assertRaisesRegex(ValueError, "exactly one source PDF"):
                run_one_job_pilot(manifest_path, source_root, run_dir, "pilot-multi", fake_linecrop)
            self.assertFalse(run_dir.exists())

    def test_refuses_a_preexisting_output_directory(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source_root = root / "private-source"
            source_root.mkdir()
            manifest_path = self._make_source(source_root)
            fake_linecrop = root / "unused-linecrop"
            self._make_fake_linecrop(fake_linecrop, root / "invocations")
            run_dir = root / "pilot-run"
            run_dir.mkdir()

            with self.assertRaisesRegex(FileExistsError, "refusing to reuse"):
                run_one_job_pilot(manifest_path, source_root, run_dir, "pilot-existing", fake_linecrop)


if __name__ == "__main__":
    unittest.main()

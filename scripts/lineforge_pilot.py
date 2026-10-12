#!/usr/bin/env python3
"""Run one isolated, local LineForge dataset pilot job and accept at most one image.

This path is deliberately separate from the 19-worker production dispatcher and
Hugging Face publisher. It validates one staged source, renders page 10 once,
invokes LineForge once, and writes a local, non-publishable one-image result.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

try:  # Support both direct CLI execution and package imports in tests.
    from .lineforge_dataset import (
        ENGINE_VERSION,
        SCHEMA_VERSION,
        QuotaLedger,
        atomic_write,
        crop_acceptance_reason,
        load_json,
        sha256_file,
        validate_source_manifest,
    )
except ImportError:  # pragma: no cover - exercised by direct CLI execution.
    from lineforge_dataset import (  # type: ignore[no-redef]
        ENGINE_VERSION,
        SCHEMA_VERSION,
        QuotaLedger,
        atomic_write,
        crop_acceptance_reason,
        load_json,
        sha256_file,
        validate_source_manifest,
    )

PILOT_PAGE_NUMBER = 10
PILOT_TARGET = 1


def _pdf_page_count(pdf_path: Path) -> int:
    try:
        proc = subprocess.run(
            ["pdfinfo", str(pdf_path)], text=True, capture_output=True,
            check=False, timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PDF page-count inspection timed out") from exc
    if proc.returncode != 0:
        raise RuntimeError("pdfinfo could not inspect the pilot PDF")
    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError as exc:
                raise RuntimeError("pdfinfo returned an invalid page count") from exc
    raise RuntimeError("pdfinfo did not report a page count")


def run_one_job_pilot(
    input_manifest: Path,
    source_root: Path,
    run_dir: Path,
    run_id: str,
    linecrop_binary: Path,
    dpi: int = 300,
) -> dict[str, Any]:
    """Run exactly one local page job and accept exactly one unique image."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", run_id):
        raise ValueError("run_id must use 1-128 letters, digits, dots, underscores, or hyphens")
    if type(dpi) is not int or not 72 <= dpi <= 600:
        raise ValueError("DPI must be between 72 and 600")

    source_root = source_root.resolve()
    input_manifest = input_manifest.resolve()
    run_dir = run_dir.resolve()
    linecrop_binary = linecrop_binary.resolve()
    if not source_root.is_dir():
        raise FileNotFoundError(f"approved source root is unavailable: {source_root}")
    try:
        input_manifest.relative_to(source_root)
    except ValueError as exc:
        raise ValueError("input manifest resolves outside the approved source root") from exc
    if run_dir == source_root or run_dir.is_relative_to(source_root) or source_root.is_relative_to(run_dir):
        raise ValueError("pilot output and private source root must be separate trees")
    if run_dir.exists():
        raise FileExistsError(f"refusing to reuse an existing pilot directory: {run_dir}")
    if not linecrop_binary.is_file() or not os.access(linecrop_binary, os.X_OK):
        raise FileNotFoundError(f"LineForge executable is unavailable: {linecrop_binary}")
    for utility in ("pdfinfo", "pdftoppm"):
        if shutil.which(utility) is None:
            raise FileNotFoundError(f"required PDF utility is unavailable: {utility}")

    source_document = load_json(input_manifest)
    if not isinstance(source_document, dict) or not isinstance(source_document.get("files"), list):
        raise ValueError("input manifest must contain one source file")
    if len(source_document["files"]) != 1:
        raise ValueError("one-job pilot accepts exactly one source PDF")
    sources = validate_source_manifest(
        source_document,
        input_manifest.parent,
        verify_files=True,
        allowed_root=source_root,
    )
    if len(sources) != 1:
        raise ValueError("one-job pilot requires exactly one eligible, rights-cleared PDF")
    source = sources[0]
    pdf_path = Path(source["path"])
    actual_pages = _pdf_page_count(pdf_path)
    if actual_pages != source["page_count"]:
        raise ValueError("staged PDF page count differs from the immutable manifest")
    if actual_pages < PILOT_PAGE_NUMBER:
        raise ValueError("pilot PDF must include page 10 to satisfy the dataset page floor")

    run_dir.mkdir(parents=True)
    page_work = run_dir / "work" / "page-00010"
    page_work.mkdir(parents=True)
    page_base = page_work / "source-page"
    rendered_path = page_work / "source-page.png"
    try:
        render = subprocess.run(
            ["pdftoppm", "-f", str(PILOT_PAGE_NUMBER), "-l", str(PILOT_PAGE_NUMBER),
             "-singlefile", "-png", "-r", str(dpi), str(pdf_path), str(page_base)],
            text=True, capture_output=True, check=False, timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("pilot PDF page render timed out") from exc
    if render.returncode != 0 or not rendered_path.is_file():
        raise RuntimeError("pilot PDF page render failed")

    crop_dir = page_work / "crops"
    try:
        engine = subprocess.run(
            [str(linecrop_binary), "--input", str(rendered_path), "--output", str(crop_dir), "--layout", "auto"],
            text=True, capture_output=True, check=False, timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("LineForge timed out on the pilot page") from exc
    page_manifest_path = crop_dir / "manifest.json"
    if engine.returncode not in (0, 2) or not page_manifest_path.is_file():
        raise RuntimeError("LineForge failed to produce a pilot page manifest")
    page_manifest = load_json(page_manifest_path)
    lines = page_manifest.get("lines") if isinstance(page_manifest, dict) else None
    if not isinstance(lines, list):
        raise ValueError("LineForge page manifest has no lines array")

    source_manifest_sha256 = sha256_file(input_manifest)
    output_dir = run_dir / "dataset"
    ledger = QuotaLedger(
        run_dir / "quota.sqlite", output_dir, PILOT_TARGET, run_id,
        source_manifest_sha256,
    )
    rejected: list[str] = []
    accepted = False
    for line_index, line in enumerate(lines, start=1):
        if not isinstance(line, dict) or not isinstance(line.get("file"), str):
            raise ValueError("LineForge emitted a malformed line entry")
        crop_path = crop_dir / line["file"]
        try:
            crop_path.resolve().relative_to(crop_dir.resolve())
        except ValueError as exc:
            raise ValueError("LineForge line path escapes the pilot crop directory") from exc
        if crop_path.is_symlink() or not crop_path.is_file():
            raise ValueError("LineForge line crop is not a regular file")
        accepted_by_filter, reason = crop_acceptance_reason(crop_path, line)
        if not accepted_by_filter:
            rejected.append(reason)
            continue
        result = ledger.accept(crop_path, {
            "source_pdf_sha256": source["pdf_sha256"],
            "source_pdf_id": source["pdf_id"],
            "source_book_name": source["public_source_name"],
            "source_book_slug": source["public_source_slug"],
            "page_number": PILOT_PAGE_NUMBER,
            "line_index": line_index,
            "shard_id": 0,
            "engine_version": page_manifest.get("version", ENGINE_VERSION),
            "source_license": source["redistribution"]["license"],
            "rights_evidence_ref": source["redistribution"]["evidence_ref"],
        })
        if result != "accepted":
            raise RuntimeError(f"one-image pilot could not accept its first candidate: {result}")
        accepted = True
        break
    if not accepted:
        raise RuntimeError("pilot produced no acceptable line crop; rejected reasons: " + ", ".join(rejected))

    records = ledger.records()
    if ledger.count() != PILOT_TARGET or len(records) != PILOT_TARGET:
        raise RuntimeError("pilot did not finish with exactly one unique image")
    record = records[0]
    image_path = output_dir / record["relative_path"]
    if not image_path.is_file() or sha256_file(image_path) != record["image_sha256"]:
        raise RuntimeError("pilot output image is missing or failed SHA-256 verification")

    dataset_manifest = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": source_document["dataset_id"],
        "run_id": run_id,
        "source_manifest_sha256": source_manifest_sha256,
        "engine_version": page_manifest.get("version", ENGINE_VERSION),
        "job_count": 1,
        "target_unique_images": PILOT_TARGET,
        "image_count": PILOT_TARGET,
        "page_number": PILOT_PAGE_NUMBER,
        "images": records,
        "pilot_only": True,
        "publishable": False,
        "publish_blocker": "One-job synthetic pilot output is not a reviewed or publishable dataset.",
    }
    dataset_manifest_path = run_dir / "pilot-dataset-manifest.json"
    atomic_write(
        dataset_manifest_path,
        json.dumps(dataset_manifest, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    summary = {
        "status": "completed",
        "run_id": run_id,
        "job_count": 1,
        "linecrop_invocations": 1,
        "rendered_pages": 1,
        "page_number": PILOT_PAGE_NUMBER,
        "target_unique_images": PILOT_TARGET,
        "image_count": PILOT_TARGET,
        "image_sha256": record["image_sha256"],
        "source_manifest_sha256": source_manifest_sha256,
        "dataset_manifest": str(dataset_manifest_path),
        "run_dir": str(run_dir),
        "publishable": False,
    }
    atomic_write(
        run_dir / "pilot-summary.json",
        json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n",
    )
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run exactly one local LineForge dataset pilot job and accept one image"
    )
    parser.add_argument("--input-manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--linecrop", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=300)
    args = parser.parse_args()
    result = run_one_job_pilot(
        args.input_manifest, args.source_root, args.run_dir, args.run_id,
        args.linecrop, args.dpi,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised through the CLI.
    raise SystemExit(main())

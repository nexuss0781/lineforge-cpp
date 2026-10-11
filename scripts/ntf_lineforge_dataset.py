#!/usr/bin/env python3
"""Local-file NTF adapter for preparing a provenance-rich LineForge crop dataset.

This module deliberately has no Telegram, Hugging Face, cloud-storage, or training
client. Its SQLite quota ledger is safe only when all workers share one filesystem.
GitHub Actions jobs do not share that filesystem; hosted dispatch requires a
separately configured shared atomic control store and approved PDF source adapter.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from PIL import Image

SCHEMA_VERSION = 1
WORKER_COUNT = 19
DEFAULT_TARGET = 100_024
MIN_PAGE = 10  # one-based PDF page number
ENGINE_VERSION = "lineforge-1.0.1"


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".tmp-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass
        raise


def load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read valid JSON from {path}: {exc}") from exc


def validate_source_manifest(document: Any, base_dir: Path, verify_files: bool = True) -> list[dict[str, Any]]:
    """Validate a private local-file manifest; reject unknown rights and duplicate PDFs."""
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("input manifest must be an object with schema_version=1")
    if not isinstance(document.get("dataset_id"), str) or not document["dataset_id"].strip():
        raise ValueError("dataset_id must be a non-empty string")
    if not isinstance(document.get("revision"), str) or not document["revision"].strip():
        raise ValueError("revision must be a non-empty immutable source revision")
    files = document.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("files must be a non-empty array")

    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    accepted: list[dict[str, Any]] = []
    for index, raw in enumerate(files):
        where = f"files[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where} must be an object")
        pdf_id = raw.get("pdf_id")
        digest = raw.get("pdf_sha256")
        page_count = raw.get("page_count")
        local_path = raw.get("path")
        if not isinstance(pdf_id, str) or not pdf_id.strip():
            raise ValueError(f"{where}.pdf_id must be a non-empty string")
        if pdf_id in seen_ids:
            raise ValueError(f"duplicate pdf_id: {pdf_id}")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError(f"{where}.pdf_sha256 must be a lowercase SHA-256 digest")
        if digest in seen_hashes:
            raise ValueError(f"duplicate PDF content SHA-256: {digest}")
        if type(page_count) is not int or page_count < 1:
            raise ValueError(f"{where}.page_count must be a positive integer")
        if raw.get("status") is not True:
            raise ValueError(f"{where} is not explicitly Status=true")
        if not isinstance(local_path, str) or not local_path.strip():
            raise ValueError(f"{where}.path must identify a local staged PDF")
        rights = raw.get("redistribution")
        if not isinstance(rights, dict) or rights.get("allowed") is not True:
            raise ValueError(f"{where} lacks explicit approval to redistribute derived images")
        if not isinstance(rights.get("license"), str) or not rights["license"].strip():
            raise ValueError(f"{where}.redistribution.license is required")
        if not isinstance(rights.get("evidence_ref"), str) or not rights["evidence_ref"].strip():
            raise ValueError(f"{where}.redistribution.evidence_ref is required")

        resolved = Path(local_path)
        if not resolved.is_absolute():
            resolved = (base_dir / resolved).resolve()
        entry = dict(raw)
        entry["path"] = str(resolved)
        if page_count < MIN_PAGE:
            entry["eligible"] = False
            entry["skip_reason"] = "fewer_than_10_pages"
        else:
            entry["eligible"] = True
            if verify_files:
                if not resolved.is_file():
                    raise FileNotFoundError(f"{where}.path does not exist: {resolved}")
                actual = sha256_file(resolved)
                if actual != digest:
                    raise ValueError(f"{where}.pdf_sha256 does not match the staged PDF")
                byte_size = raw.get("byte_size")
                if byte_size is not None and (type(byte_size) is not int or byte_size != resolved.stat().st_size):
                    raise ValueError(f"{where}.byte_size does not match the staged PDF")
        seen_ids.add(pdf_id)
        seen_hashes.add(digest)
        accepted.append(entry)
    return [entry for entry in accepted if entry["eligible"]]


def assign_pdf_shards(files: Iterable[dict[str, Any]], shard_count: int = WORKER_COUNT) -> list[list[dict[str, Any]]]:
    """Balance whole PDFs by page count with a stable largest-processing-first rule."""
    if shard_count != WORKER_COUNT:
        raise ValueError("NTF adapter requires exactly 19 worker shards")
    entries = list(files)
    if len({item["pdf_sha256"] for item in entries}) != len(entries):
        raise ValueError("duplicate PDF content would be assigned more than once")
    shards: list[list[dict[str, Any]]] = [[] for _ in range(shard_count)]
    page_loads = [0] * shard_count
    for item in sorted(entries, key=lambda row: (-row["page_count"], row["pdf_sha256"])):
        shard = min(range(shard_count), key=lambda sid: (page_loads[sid], sid))
        shards[shard].append(item)
        page_loads[shard] += item["page_count"]
    for shard in shards:
        shard.sort(key=lambda row: row["pdf_sha256"])
    assigned = [item["pdf_sha256"] for shard in shards for item in shard]
    if len(assigned) != len(entries) or len(set(assigned)) != len(assigned):
        raise AssertionError("internal error: shard assignment is not exact and disjoint")
    return shards


def middle_page_order(page_count: int, run_seed: str, pdf_sha256: str) -> list[int]:
    """Return a deterministic random ordering of unique, one-based middle pages >=10."""
    if type(page_count) is not int or page_count < 1:
        raise ValueError("page_count must be positive")
    if not isinstance(run_seed, str) or not run_seed:
        raise ValueError("run_seed must be a non-empty string")
    if len(pdf_sha256) != 64:
        raise ValueError("pdf_sha256 must be a SHA-256 digest")
    if page_count < MIN_PAGE:
        return []
    middle_start = max(MIN_PAGE, math.ceil(page_count * 0.25))
    middle_end = min(page_count, math.floor(page_count * 0.75))
    if middle_start > middle_end:
        middle_start = MIN_PAGE
        middle_end = page_count
    pages = list(range(middle_start, middle_end + 1))
    seed_material = f"{run_seed}\0{pdf_sha256}".encode("utf-8")
    seed = int.from_bytes(hashlib.sha256(seed_material).digest(), "big")
    random.Random(seed).shuffle(pages)
    return pages


def crop_acceptance_reason(image_path: Path, line_metadata: dict[str, Any]) -> tuple[bool, str]:
    """A conservative non-OCR screen; ambiguous/review-flagged crops are rejected."""
    if line_metadata.get("review_required") is not False:
        return False, "engine_review_required"
    if line_metadata.get("review_reasons") not in ([], None):
        return False, "engine_review_reasons"
    try:
        with Image.open(image_path) as source:
            gray = source.convert("L")
            width, height = gray.size
            if width < 20 or height < 6:
                return False, "crop_too_small"
            if width / height < 1.25:
                return False, "not_line_shaped"
            mask = gray.point(lambda px: 255 if px < 175 else 0)
            histogram = mask.histogram()
            ink = sum(histogram[1:])
            density = ink / (width * height)
            if density < 0.004:
                return False, "too_little_ink"
            if density > 0.42:
                return False, "excessively_dense_or_illustration_like"
            row_projection = mask.resize((1, height), Image.Resampling.BOX)
            col_projection = mask.resize((width, 1), Image.Resampling.BOX)
            row_occupancy = [value / 255.0 for value in row_projection.tobytes()]
            col_occupancy = [value / 255.0 for value in col_projection.tobytes()]
            active_rows = sum(value >= 0.015 for value in row_occupancy)
            active_cols = sum(value >= 0.015 for value in col_occupancy)
            if active_rows < max(3, math.ceil(height * 0.16)):
                return False, "too_few_ink_rows"
            if active_cols < max(8, math.ceil(width * 0.06)):
                return False, "too_few_ink_columns"
            active_row_values = [value for value in row_occupancy if value >= 0.015]
            median_active_row_ink = sorted(active_row_values)[len(active_row_values) // 2]
            if active_rows / height > 0.65 and median_active_row_ink < 0.12:
                return False, "figure_like_row_coverage"
            if any(value >= 0.92 for value in row_occupancy):
                return False, "horizontal_rule_or_figure_strip"
            if any(value >= 0.92 for value in col_occupancy):
                return False, "vertical_rule_or_figure_edge"
            return True, "accepted_text_like_crop"
    except (OSError, ValueError) as exc:
        return False, f"invalid_image:{type(exc).__name__}"


class QuotaLedger:
    """SQLite exact-count, de-duplicating ledger for colocated/local workers only."""

    def __init__(self, database: Path, output_dir: Path, target: int, run_id: str, manifest_sha256: str):
        if type(target) is not int or target < 1:
            raise ValueError("target must be a positive integer")
        if not run_id or len(manifest_sha256) != 64:
            raise ValueError("run_id and manifest SHA-256 are required")
        self.database = database
        self.output_dir = output_dir
        self.target = target
        self.run_id = run_id
        self.manifest_sha256 = manifest_sha256
        self._connection_state = threading.local()
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        with self._connection() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("""CREATE TABLE IF NOT EXISTS metadata (
                id INTEGER PRIMARY KEY CHECK (id = 1), target INTEGER NOT NULL,
                run_id TEXT NOT NULL, manifest_sha256 TEXT NOT NULL)""")
            connection.execute("""CREATE TABLE IF NOT EXISTS images (
                sequence INTEGER PRIMARY KEY, image_sha256 TEXT NOT NULL UNIQUE,
                provenance_key TEXT NOT NULL UNIQUE, source_pdf_sha256 TEXT NOT NULL,
                page_number INTEGER NOT NULL, line_index INTEGER NOT NULL,
                shard_id INTEGER NOT NULL, engine_version TEXT NOT NULL,
                source_license TEXT NOT NULL,
                relative_path TEXT NOT NULL)""")
            stored = connection.execute("SELECT target, run_id, manifest_sha256 FROM metadata WHERE id=1").fetchone()
            if stored is None:
                connection.execute("INSERT INTO metadata(id,target,run_id,manifest_sha256) VALUES(1,?,?,?)",
                                   (target, run_id, manifest_sha256))
            elif tuple(stored) != (target, run_id, manifest_sha256):
                raise ValueError("ledger run identity, input manifest, or target does not match")

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database, timeout=60, isolation_level=None)
        connection.execute("PRAGMA busy_timeout=60000")
        return connection

    def _connection(self) -> sqlite3.Connection:
        connection = getattr(self._connection_state, "connection", None)
        if connection is None:
            connection = self._connect()
            self._connection_state.connection = connection
        return connection

    def count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM images").fetchone()[0])

    def count_for_shard(self, shard_id: int) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM images WHERE shard_id=?", (shard_id,)).fetchone()[0])

    def accept(self, image_path: Path, record: dict[str, Any]) -> str:
        required = ("source_pdf_sha256", "page_number", "line_index", "shard_id", "source_license")
        if any(key not in record for key in required):
            raise ValueError("image provenance is incomplete")
        if type(record["page_number"]) is not int or record["page_number"] < MIN_PAGE:
            raise ValueError("refusing to count a crop from a PDF page before page 10")
        if type(record["line_index"]) is not int or record["line_index"] < 1:
            raise ValueError("line_index must be a positive integer")
        if type(record["shard_id"]) is not int or not 0 <= record["shard_id"] < WORKER_COUNT:
            raise ValueError("shard_id must be in [0,18]")
        source_hash = record["source_pdf_sha256"]
        if not isinstance(source_hash, str) or len(source_hash) != 64:
            raise ValueError("source_pdf_sha256 must be a SHA-256 digest")
        if not isinstance(record["source_license"], str) or not record["source_license"].strip():
            raise ValueError("source_license must be a non-empty rights declaration")
        image_bytes = image_path.read_bytes()
        image_hash = sha256_bytes(image_bytes)
        provenance_key = f"{source_hash}:{record['page_number']}:{record['line_index']}"
        relative_path = Path("images") / image_hash[:2] / f"{image_hash}.png"
        destination = self.output_dir / relative_path

        with self._connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = int(connection.execute("SELECT COUNT(*) FROM images").fetchone()[0])
            if current >= self.target:
                connection.rollback()
                return "target_reached"
            if connection.execute("SELECT 1 FROM images WHERE image_sha256=?", (image_hash,)).fetchone():
                connection.rollback()
                return "duplicate_image"
            if connection.execute("SELECT 1 FROM images WHERE provenance_key=?", (provenance_key,)).fetchone():
                connection.rollback()
                return "duplicate_provenance"
            if destination.exists():
                if sha256_file(destination) != image_hash:
                    connection.rollback()
                    raise ValueError("existing image path does not match its SHA-256")
            else:
                atomic_write(destination, image_bytes)
            try:
                connection.execute(
                    "INSERT INTO images(sequence,image_sha256,provenance_key,source_pdf_sha256,page_number,line_index,shard_id,engine_version,source_license,relative_path) VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (current + 1, image_hash, provenance_key, source_hash, record["page_number"],
                     record["line_index"], record["shard_id"], record.get("engine_version", ENGINE_VERSION),
                     record["source_license"],
                     str(relative_path.as_posix())),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        return "accepted"

    def records(self) -> list[dict[str, Any]]:
        with self._connection() as connection:
            rows = connection.execute(
                "SELECT sequence,image_sha256,source_pdf_sha256,page_number,line_index,shard_id,engine_version,source_license,relative_path FROM images ORDER BY sequence"
            ).fetchall()
        return [dict(zip(("sequence", "image_sha256", "source_pdf_sha256", "page_number", "line_index", "shard_id", "engine_version", "source_license", "relative_path"), row)) for row in rows]


def load_shard_identity(shards_dir: Path) -> tuple[str, str]:
    shard_files = sorted(shards_dir.glob("shard-*.json"))
    if len(shard_files) != WORKER_COUNT:
        raise ValueError("coordinator requires exactly 19 shard manifests")
    docs = [load_json(path) for path in shard_files]
    shard_ids = [doc.get("shard_id") if isinstance(doc, dict) else None for doc in docs]
    if any(type(shard) is not int or not 0 <= shard < WORKER_COUNT for shard in shard_ids):
        raise ValueError("shard manifest has a malformed shard_id")
    if len(set(shard_ids)) != WORKER_COUNT or set(shard_ids) != set(range(WORKER_COUNT)):
        raise ValueError("shard manifests do not cover each worker exactly once")
    if any(doc.get("schema_version") != SCHEMA_VERSION or doc.get("worker_count") != WORKER_COUNT for doc in docs):
        raise ValueError("shard manifest schema or worker count mismatch")
    identities = {(doc.get("run_id"), doc.get("source_manifest_sha256")) for doc in docs}
    if len(identities) != 1:
        raise ValueError("shard manifests do not share one run and input identity")
    run_id, manifest_sha256 = next(iter(identities))
    if not isinstance(run_id, str) or not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
        raise ValueError("shard manifests have malformed run or input identity")
    return run_id, manifest_sha256


def monitor_local_workers(ledger: QuotaLedger, results_dir: Path, poll_seconds: int = 5, timeout_seconds: int = 21_600) -> dict[str, int | str]:
    """Monitor colocated workers; this cannot monitor isolated GitHub-hosted filesystems."""
    if poll_seconds < 1 or timeout_seconds < 1:
        raise ValueError("poll and timeout values must be positive")
    deadline = time.monotonic() + timeout_seconds
    while True:
        count = ledger.count()
        if count > ledger.target:
            raise ValueError("shared quota ledger exceeded its configured target")
        result_files = sorted(results_dir.glob("worker-*.json")) if results_dir.exists() else []
        print(f"live unique images: {count}/{ledger.target}; worker results: {len(result_files)}/{WORKER_COUNT}", flush=True)
        if count == ledger.target and len(result_files) == WORKER_COUNT:
            return {"status": "target_reached", "image_count": count, "worker_results": len(result_files)}
        if len(result_files) == WORKER_COUNT and count < ledger.target:
            raise ValueError(f"all 19 workers stopped below target: {count}/{ledger.target}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"timed out monitoring workers at {count}/{ledger.target} images")
        time.sleep(min(poll_seconds, remaining))


def prepare_shard_manifests(input_path: Path, output_dir: Path, run_seed: str, run_id: str) -> dict[str, Any]:
    if not run_seed or not run_id:
        raise ValueError("run_seed and run_id must be non-empty")
    document = load_json(input_path)
    entries = validate_source_manifest(document, input_path.parent, verify_files=True)
    if not entries:
        raise ValueError("no Status=true PDFs with at least 10 pages are eligible")
    input_digest = sha256_file(input_path)
    shards = assign_pdf_shards(entries)
    output_dir.mkdir(parents=True, exist_ok=True)
    for shard_id, shard_entries in enumerate(shards):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "run_id": run_id,
            "run_seed": run_seed,
            "worker_count": WORKER_COUNT,
            "shard_id": shard_id,
            "source_manifest_sha256": input_digest,
            "dataset_id": document["dataset_id"],
            "revision": document["revision"],
            "files": shard_entries,
        }
        atomic_write(output_dir / f"shard-{shard_id:02d}.json", json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    summary = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_seed": run_seed,
        "worker_count": WORKER_COUNT,
        "source_manifest_sha256": input_digest,
        "eligible_pdf_count": len(entries),
        "assigned_pdf_count": sum(len(shard) for shard in shards),
        "assigned_page_count": sum(item["page_count"] for item in entries),
        "shards": [{"shard_id": sid, "pdf_count": len(shard), "page_count": sum(row["page_count"] for row in shard)} for sid, shard in enumerate(shards)],
    }
    atomic_write(output_dir / "assignment-summary.json", json.dumps(summary, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return summary


def _pdf_page_count(pdf_path: Path) -> int:
    proc = subprocess.run(["pdfinfo", str(pdf_path)], text=True, capture_output=True, check=False)
    if proc.returncode != 0:
        raise RuntimeError("pdfinfo could not inspect a staged PDF")
    for line in proc.stdout.splitlines():
        if line.startswith("Pages:"):
            return int(line.split(":", 1)[1].strip())
    raise RuntimeError("pdfinfo did not report a page count")


def process_shard(
    shard_manifest_path: Path,
    linecrop_binary: Path,
    work_dir: Path,
    output_dir: Path,
    ledger_path: Path,
    target: int = DEFAULT_TARGET,
    dpi: int = 300,
) -> Path:
    shard_doc = load_json(shard_manifest_path)
    if shard_doc.get("schema_version") != SCHEMA_VERSION or shard_doc.get("worker_count") != WORKER_COUNT:
        raise ValueError("invalid shard manifest schema or worker count")
    shard_id = shard_doc.get("shard_id")
    if type(shard_id) is not int or not 0 <= shard_id < WORKER_COUNT:
        raise ValueError("invalid shard_id")
    run_id = shard_doc.get("run_id")
    manifest_digest = shard_doc.get("source_manifest_sha256")
    files = shard_doc.get("files")
    if not isinstance(run_id, str) or not isinstance(manifest_digest, str) or not isinstance(files, list):
        raise ValueError("shard manifest lacks run/input identity or files")
    if not linecrop_binary.is_file() or not os.access(linecrop_binary, os.X_OK):
        raise FileNotFoundError(f"linecrop executable is unavailable: {linecrop_binary}")
    ledger = QuotaLedger(ledger_path, output_dir, target, run_id, manifest_digest)
    page_count_checked: set[str] = set()
    pages_processed = 0
    pdfs_processed = 0
    skipped_review = 0
    skipped_duplicate = 0
    failures: list[str] = []
    stopped_at_quota = False

    for pdf in files:
        if ledger.count() >= target:
            stopped_at_quota = True
            break
        pdf_path = Path(pdf["path"])
        if not pdf_path.is_file() or sha256_file(pdf_path) != pdf["pdf_sha256"]:
            raise ValueError("staged PDF is missing or its source checksum changed")
        actual_pages = _pdf_page_count(pdf_path)
        if actual_pages != pdf["page_count"]:
            raise ValueError("staged PDF page count differs from the immutable manifest")
        page_count_checked.add(pdf["pdf_sha256"])
        pdfs_processed += 1
        for page_number in middle_page_order(pdf["page_count"], shard_doc["run_seed"], pdf["pdf_sha256"]):
            if ledger.count() >= target:
                stopped_at_quota = True
                break
            page_work = work_dir / run_id / f"shard-{shard_id:02d}" / pdf["pdf_sha256"] / f"page-{page_number:05d}"
            page_work.mkdir(parents=True, exist_ok=True)
            page_base = page_work / "source-page"
            rendered_path = Path(str(page_base) + ".png")
            render = subprocess.run(
                ["pdftoppm", "-f", str(page_number), "-l", str(page_number), "-singlefile", "-png", "-r", str(dpi), str(pdf_path), str(page_base)],
                text=True, capture_output=True, check=False,
            )
            if render.returncode != 0 or not rendered_path.is_file():
                raise RuntimeError(f"page render failed for page {page_number}; source content was not accepted")
            crop_dir = page_work / "crops"
            proc = subprocess.run(
                [str(linecrop_binary), "--input", str(rendered_path), "--output", str(crop_dir), "--layout", "auto"],
                text=True, capture_output=True, check=False,
            )
            page_manifest_path = crop_dir / "manifest.json"
            if proc.returncode not in (0, 2) or not page_manifest_path.is_file():
                raise RuntimeError(f"LineForge failed on source page {page_number}; source content was not accepted")
            page_manifest = load_json(page_manifest_path)
            lines = page_manifest.get("lines")
            if not isinstance(lines, list):
                raise ValueError("LineForge page manifest has no lines array")
            pages_processed += 1
            for line_index, line in enumerate(lines, start=1):
                if ledger.count() >= target:
                    stopped_at_quota = True
                    break
                if not isinstance(line, dict) or not isinstance(line.get("file"), str):
                    raise ValueError("LineForge emitted a malformed line entry")
                crop_path = crop_dir / line["file"]
                accepted, reason = crop_acceptance_reason(crop_path, line)
                if not accepted:
                    skipped_review += 1
                    continue
                result = ledger.accept(crop_path, {
                    "source_pdf_sha256": pdf["pdf_sha256"],
                    "page_number": page_number,
                    "line_index": line_index,
                    "shard_id": shard_id,
                    "engine_version": page_manifest.get("version", ENGINE_VERSION),
                    "source_license": pdf["redistribution"]["license"],
                })
                if result.startswith("duplicate"):
                    skipped_duplicate += 1
                if result == "target_reached":
                    stopped_at_quota = True
                    break
            if stopped_at_quota:
                break
        if stopped_at_quota:
            break

    worker_result = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "run_seed": shard_doc["run_seed"],
        "shard_id": shard_id,
        "worker_count": WORKER_COUNT,
        "source_manifest_sha256": manifest_digest,
        "status": "quota_reached" if ledger.count() >= target else "completed",
        "target": target,
        "accepted_count": ledger.count_for_shard(shard_id),
        "global_live_count": ledger.count(),
        "pages_processed": pages_processed,
        "pdfs_processed": pdfs_processed,
        "review_or_nontext_rejections": skipped_review,
        "dedupe_rejections": skipped_duplicate,
        "source_pdf_count": len(page_count_checked),
        "failures": failures,
    }
    result_path = output_dir / "workers" / f"worker-{shard_id:02d}.json"
    atomic_write(result_path, json.dumps(worker_result, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return result_path


def reduce_worker_results(results_dir: Path, ledger: QuotaLedger, run_id: str, manifest_sha256: str, target: int, output_path: Path) -> dict[str, Any]:
    result_files = sorted(results_dir.glob("worker-*.json"))
    if len(result_files) != WORKER_COUNT:
        raise ValueError(f"expected exactly 19 worker result files, found {len(result_files)}")
    seen: set[int] = set()
    results: list[dict[str, Any]] = []
    run_seeds: set[str] = set()
    for path in result_files:
        result = load_json(path)
        if not isinstance(result, dict) or result.get("schema_version") != SCHEMA_VERSION:
            raise ValueError(f"malformed worker result: {path.name}")
        shard = result.get("shard_id")
        if type(shard) is not int or not 0 <= shard < WORKER_COUNT:
            raise ValueError(f"invalid shard id in {path.name}")
        if shard in seen:
            raise ValueError(f"duplicate worker result for shard {shard:02d}")
        seen.add(shard)
        if result.get("worker_count") != WORKER_COUNT:
            raise ValueError(f"wrong worker count in {path.name}")
        if result.get("run_id") != run_id or result.get("source_manifest_sha256") != manifest_sha256:
            raise ValueError("workers used different run or input identities")
        if not isinstance(result.get("run_seed"), str) or not result["run_seed"]:
            raise ValueError(f"worker shard {shard:02d} has no deterministic run seed")
        run_seeds.add(result["run_seed"])
        if result.get("status") not in ("completed", "quota_reached"):
            raise ValueError(f"worker shard {shard:02d} did not complete successfully")
        if result.get("failures") != []:
            raise ValueError(f"worker shard {shard:02d} reports failures")
        for field in ("accepted_count", "global_live_count", "pages_processed", "pdfs_processed"):
            if type(result.get(field)) is not int or result[field] < 0:
                raise ValueError(f"invalid {field} in {path.name}")
        if type(result.get("target")) is not int or result["target"] != target:
            raise ValueError(f"target mismatch in {path.name}")
        if result["accepted_count"] != ledger.count_for_shard(shard):
            raise ValueError(f"worker shard {shard:02d} count differs from the shared local ledger")
        results.append(result)
    missing = sorted(set(range(WORKER_COUNT)) - seen)
    if missing:
        raise ValueError("missing worker shard result(s): " + ", ".join(f"{item:02d}" for item in missing))
    if sum(item["accepted_count"] for item in results) != ledger.count():
        raise ValueError("worker accepted counts do not reconcile with the global ledger")
    if len(run_seeds) != 1:
        raise ValueError("workers used different deterministic run seeds")
    if ledger.count() != target:
        raise ValueError(f"exact target not reached: have {ledger.count()}, require {target}")
    records = ledger.records()
    if len(records) != target or len({row["image_sha256"] for row in records}) != target:
        raise ValueError("final ledger does not contain exactly target unique image bytes")
    for row in records:
        if row["page_number"] < MIN_PAGE:
            raise ValueError("final output contains a crop from before page 10")
        image_path = ledger.output_dir / row["relative_path"]
        if not image_path.is_file() or sha256_file(image_path) != row["image_sha256"]:
            raise ValueError("final image is missing or its content checksum differs")
    final = {
        "schema_version": SCHEMA_VERSION,
        "dataset_id": "lineforge-ethiopic-line-crops",
        "run_id": run_id,
        "run_seed": next(iter(run_seeds)),
        "source_manifest_sha256": manifest_sha256,
        "engine_version": ENGINE_VERSION,
        "worker_count": WORKER_COUNT,
        "target_unique_images": target,
        "image_count": len(records),
        "images": records,
        "workers": [{"shard_id": item["shard_id"], "accepted_count": item["accepted_count"], "status": item["status"], "pages_processed": item["pages_processed"]} for item in sorted(results, key=lambda item: item["shard_id"])],
        "publishable": False,
        "publish_blocker": "Public Hugging Face publication requires a separate rights review and user approval.",
    }
    atomic_write(output_path, json.dumps(final, ensure_ascii=False, indent=2, sort_keys=True).encode("utf-8") + b"\n")
    return final


def _cmd_prepare(args: argparse.Namespace) -> int:
    summary = prepare_shard_manifests(args.input_manifest, args.output_dir, args.run_seed, args.run_id)
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    result = process_shard(args.shard_manifest, args.linecrop, args.work_dir, args.output_dir, args.ledger, args.target, args.dpi)
    print(result)
    return 0


def _cmd_reduce(args: argparse.Namespace) -> int:
    run_id, manifest_sha = load_shard_identity(args.shards_dir)
    ledger = QuotaLedger(args.ledger, args.output_dir, args.target, run_id, manifest_sha)
    result = reduce_worker_results(args.results_dir, ledger, run_id, manifest_sha, args.target, args.output)
    print(json.dumps({"run_id": run_id, "image_count": result["image_count"], "publishable": False, "output": str(args.output)}, sort_keys=True))
    return 0


def _cmd_monitor(args: argparse.Namespace) -> int:
    run_id, manifest_sha = load_shard_identity(args.shards_dir)
    ledger = QuotaLedger(args.ledger, args.output_dir, args.target, run_id, manifest_sha)
    result = monitor_local_workers(ledger, args.results_dir, args.poll_seconds, args.timeout_seconds)
    print(json.dumps(result, sort_keys=True))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    prepare = subparsers.add_parser("prepare", help="validate a staged local PDF manifest and make 19 disjoint shards")
    prepare.add_argument("--input-manifest", type=Path, required=True)
    prepare.add_argument("--output-dir", type=Path, required=True)
    prepare.add_argument("--run-seed", required=True)
    prepare.add_argument("--run-id", required=True)
    prepare.set_defaults(func=_cmd_prepare)
    worker = subparsers.add_parser("worker", help="process one local shard using a shared local SQLite quota ledger")
    worker.add_argument("--shard-manifest", type=Path, required=True)
    worker.add_argument("--linecrop", type=Path, required=True)
    worker.add_argument("--work-dir", type=Path, required=True)
    worker.add_argument("--output-dir", type=Path, required=True)
    worker.add_argument("--ledger", type=Path, required=True)
    worker.add_argument("--target", type=int, default=DEFAULT_TARGET)
    worker.add_argument("--dpi", type=int, default=300)
    worker.set_defaults(func=_cmd_worker)
    reduce = subparsers.add_parser("reduce", help="validate all 19 local worker results and emit exact-count provenance")
    reduce.add_argument("--shards-dir", type=Path, required=True)
    reduce.add_argument("--results-dir", type=Path, required=True)
    reduce.add_argument("--ledger", type=Path, required=True)
    reduce.add_argument("--output-dir", type=Path, required=True)
    reduce.add_argument("--target", type=int, default=DEFAULT_TARGET)
    reduce.add_argument("--output", type=Path, required=True)
    reduce.set_defaults(func=_cmd_reduce)
    monitor = subparsers.add_parser("monitor", help="monitor local worker counts using the shared SQLite quota ledger")
    monitor.add_argument("--shards-dir", type=Path, required=True)
    monitor.add_argument("--results-dir", type=Path, required=True)
    monitor.add_argument("--ledger", type=Path, required=True)
    monitor.add_argument("--output-dir", type=Path, required=True)
    monitor.add_argument("--target", type=int, default=DEFAULT_TARGET)
    monitor.add_argument("--poll-seconds", type=int, default=5)
    monitor.add_argument("--timeout-seconds", type=int, default=21_600)
    monitor.set_defaults(func=_cmd_monitor)
    args = parser.parse_args()
    try:
        return args.func(args)
    except Exception as exc:
        print(f"ntf-lineforge: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

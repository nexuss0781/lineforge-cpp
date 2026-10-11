#!/usr/bin/env python3
"""Fail-closed Hugging Face publisher for validated LineForge dataset runs.

The default workflow is local-only preflight. A remote upload requires both an
explicit --confirm-publish CLI choice and HF_TOKEN in the environment. The
publisher never uploads private source manifests, worker results, quota data,
source identifiers, source checksums, or rights-evidence references.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
from pathlib import Path
from typing import Any

try:  # Supports both `python scripts/lineforge_hf_publish.py` and package tests.
    from .lineforge_dataset import (
        DEFAULT_TARGET,
        MIN_PAGE,
        SCHEMA_VERSION,
        WORKER_COUNT,
        load_json,
        require_hf_token,
        sha256_file,
        validate_source_manifest,
    )
except ImportError:  # pragma: no cover - exercised by direct CLI execution.
    from lineforge_dataset import (  # type: ignore[no-redef]
        DEFAULT_TARGET,
        MIN_PAGE,
        SCHEMA_VERSION,
        WORKER_COUNT,
        load_json,
        require_hf_token,
        sha256_file,
        validate_source_manifest,
    )

HF_DATASET_REPO = "Nexuss0781/Geez-OCR-Dataset"
PUBLIC_INDEX_NAME = "lineforge-public-manifest.json"
LICENSE_PLACEHOLDER = "{{DATASET_LICENSE}}"
_IMAGE_COLUMNS = (
    "sequence", "image_sha256", "provenance_key", "source_pdf_sha256",
    "source_pdf_id", "page_number", "line_index", "shard_id",
    "engine_version", "source_license", "rights_evidence_ref", "relative_path",
)


def _require_file(path: Path, description: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"{description} must be a regular, non-symlink file")
    return path.resolve(strict=True)


def _require_directory(path: Path, description: str) -> Path:
    if path.is_symlink() or not path.is_dir():
        raise ValueError(f"{description} must be a regular, non-symlink directory")
    return path.resolve(strict=True)


def _valid_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _load_quota_records(database: Path, expected_target: int, run_id: str, source_manifest_sha256: str) -> list[dict[str, Any]]:
    if database.is_symlink() or not database.is_file():
        raise ValueError("the local quota ledger is missing or unsafe")
    uri = database.resolve(strict=True).as_uri() + "?mode=ro"
    try:
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        try:
            metadata = connection.execute(
                "SELECT target, run_id, manifest_sha256 FROM metadata WHERE id=1"
            ).fetchone()
            if metadata is None or tuple(metadata) != (expected_target, run_id, source_manifest_sha256):
                raise ValueError("quota ledger run identity, target, or source manifest does not match")
            rows = connection.execute(
                "SELECT " + ",".join(_IMAGE_COLUMNS) + " FROM images ORDER BY sequence"
            ).fetchall()
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
            if integrity is None or integrity[0] != "ok":
                raise ValueError("quota ledger failed its SQLite integrity check")
            return [dict(row) for row in rows]
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise ValueError("the local quota ledger is missing, malformed, or unreadable") from exc


def _validate_workers(
    results_dir: Path,
    ledger_records: list[dict[str, Any]],
    run_id: str,
    source_manifest_sha256: str,
    run_seed: str,
    expected_target: int,
) -> None:
    if results_dir.is_symlink() or not results_dir.is_dir():
        raise ValueError("the worker-results directory is missing or unsafe")
    result_files = sorted(results_dir.glob("worker-*.json"))
    expected_names = {f"worker-{shard:02d}.json" for shard in range(WORKER_COUNT)}
    if {path.name for path in result_files} != expected_names or len(result_files) != WORKER_COUNT:
        raise ValueError("exactly 19 distinct worker results are required")

    accepted_by_shard = {shard: 0 for shard in range(WORKER_COUNT)}
    for row in ledger_records:
        shard = row["shard_id"]
        if type(shard) is not int or shard not in accepted_by_shard:
            raise ValueError("quota ledger contains an invalid worker shard")
        accepted_by_shard[shard] += 1

    seen_shards: set[int] = set()
    for path in result_files:
        if path.is_symlink() or not path.is_file():
            raise ValueError("worker result is not a regular file")
        result = load_json(path)
        if not isinstance(result, dict):
            raise ValueError("worker result is malformed")
        shard = result.get("shard_id")
        if type(shard) is not int or not 0 <= shard < WORKER_COUNT:
            raise ValueError("worker result has an invalid shard id")
        if shard in seen_shards or path.name != f"worker-{shard:02d}.json":
            raise ValueError("worker result filenames and shard identities do not match one-to-one")
        seen_shards.add(shard)
        if (
            result.get("schema_version") != SCHEMA_VERSION
            or result.get("worker_count") != WORKER_COUNT
            or result.get("run_id") != run_id
            or result.get("source_manifest_sha256") != source_manifest_sha256
            or result.get("run_seed") != run_seed
            or result.get("target") != expected_target
            or result.get("status") not in ("completed", "quota_reached")
            or result.get("failures") != []
            or type(result.get("accepted_count")) is not int
            or result.get("accepted_count") != accepted_by_shard[shard]
            or type(result.get("global_live_count")) is not int
            or not 0 <= result["global_live_count"] <= expected_target
            or any(
                type(result.get(field)) is not int or result[field] < 0
                for field in ("pages_processed", "pdfs_processed")
            )
        ):
            raise ValueError("worker results do not reconcile with the validated run and quota ledger")
    if seen_shards != set(range(WORKER_COUNT)):
        raise ValueError("worker results do not cover all 19 shard identities")


def _render_card(card_template: Path, dataset_license: str) -> str:
    template_path = _require_file(card_template, "dataset-card template")
    template = template_path.read_text(encoding="utf-8")
    expected_line = f"license: {LICENSE_PLACEHOLDER}"
    if template.count(expected_line) != 1 or not template.startswith("---\n"):
        raise ValueError("dataset-card template must declare one license placeholder in its YAML front matter")
    card = template.replace(LICENSE_PLACEHOLDER, dataset_license)
    front_matter = card.split("---\n", 2)
    if len(front_matter) < 3 or f"license: {dataset_license}\n" not in front_matter[1]:
        raise ValueError("rendered dataset card does not declare the selected dataset license")
    if LICENSE_PLACEHOLDER in card:
        raise ValueError("dataset-card template contains an unresolved placeholder")
    return card


def validate_publication(
    run_dir: Path,
    source_manifest_path: Path,
    source_root: Path,
    repo_id: str,
    dataset_license: str,
    card_template: Path,
    expected_target: int = DEFAULT_TARGET,
) -> dict[str, Any]:
    """Verify source rights, provenance, workers, quota ledger, and image bytes."""
    if repo_id != HF_DATASET_REPO:
        raise ValueError("publication is restricted to the configured Hugging Face dataset repository")
    if not isinstance(dataset_license, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}", dataset_license):
        raise ValueError("a single explicit Hugging Face/SPDX-style dataset license identifier is required")
    if type(expected_target) is not int or expected_target < 1:
        raise ValueError("expected target must be a positive integer")

    run_dir = _require_directory(run_dir, "run directory")
    source_root = _require_directory(source_root, "private source root")
    source_manifest_path = _require_file(source_manifest_path, "private source manifest")
    card_template = _require_file(card_template, "dataset-card template")
    try:
        source_manifest_path.relative_to(source_root)
    except ValueError as exc:
        raise ValueError("private source manifest is outside the configured source root") from exc
    if run_dir == source_root or run_dir.is_relative_to(source_root) or source_root.is_relative_to(run_dir):
        raise ValueError("private source and dataset output must be separate trees")

    dataset_dir = _require_directory(run_dir / "dataset", "dataset output directory")
    images_dir = _require_directory(dataset_dir / "images", "dataset image directory")
    manifest_path = _require_file(run_dir / "dataset-manifest.json", "reduced dataset manifest")
    summary_path = _require_file(run_dir / "dispatch-summary.json", "dispatch summary")
    source_digest = sha256_file(source_manifest_path)
    source_document = load_json(source_manifest_path)
    source_files = validate_source_manifest(
        source_document,
        source_manifest_path.parent,
        verify_files=True,
        allowed_root=source_root,
    )
    if not source_files:
        raise ValueError("no rights-cleared eligible source PDFs are present")

    source_by_hash: dict[str, dict[str, Any]] = {}
    for source in source_files:
        rights = source.get("redistribution")
        if not isinstance(rights, dict) or rights.get("allowed") is not True:
            raise ValueError("every eligible source must explicitly allow redistribution")
        if rights.get("dataset_license") != dataset_license:
            raise ValueError("every eligible source must explicitly clear the selected dataset license")
        if not isinstance(rights.get("evidence_ref"), str) or not rights["evidence_ref"].strip():
            raise ValueError("every eligible source must have a source-specific rights evidence reference")
        source_by_hash[source["pdf_sha256"]] = source

    summary = load_json(summary_path)
    manifest = load_json(manifest_path)
    if not isinstance(summary, dict) or not isinstance(manifest, dict):
        raise ValueError("dispatch summary or reduced dataset manifest is malformed")
    if (
        manifest.get("schema_version") != SCHEMA_VERSION
        or manifest.get("worker_count") != WORKER_COUNT
        or manifest.get("source_manifest_sha256") != source_digest
        or manifest.get("target_unique_images") != expected_target
        or manifest.get("image_count") != expected_target
        or manifest.get("publishable") is not False
        or not isinstance(manifest.get("run_id"), str)
        or not manifest.get("run_id")
        or not isinstance(manifest.get("run_seed"), str)
        or not manifest.get("run_seed")
        or not isinstance(manifest.get("images"), list)
        or len(manifest["images"]) != expected_target
    ):
        raise ValueError("reduced dataset manifest is incomplete or does not match the exact target")
    if (
        summary.get("run_id") != manifest["run_id"]
        or summary.get("worker_count") != WORKER_COUNT
        or summary.get("coordinator_count") != 1
        or summary.get("source_manifest_sha256") != source_digest
        or summary.get("target_unique_images") != expected_target
        or summary.get("image_count") != expected_target
        or summary.get("publishable") is not False
        or Path(str(summary.get("run_dir", ""))).resolve() != run_dir
        or Path(str(summary.get("dataset_manifest", ""))).resolve() != manifest_path
        or Path(str(summary.get("image_directory", ""))).resolve() != images_dir
    ):
        raise ValueError("dispatch summary does not match the reduced dataset manifest and output paths")

    ledger_records = _load_quota_records(
        run_dir / "quota.sqlite", expected_target, manifest["run_id"], source_digest
    )
    ledger_manifest_records = [
        {key: value for key, value in record.items() if key != "provenance_key"}
        for record in ledger_records
    ]
    if len(ledger_records) != expected_target or ledger_manifest_records != manifest["images"]:
        raise ValueError("quota ledger and reduced dataset manifest do not contain the same exact image records")
    _validate_workers(
        dataset_dir / "workers",
        ledger_records,
        manifest["run_id"],
        source_digest,
        manifest["run_seed"],
        expected_target,
    )

    seen_hashes: set[str] = set()
    seen_provenance: set[str] = set()
    for sequence, row in enumerate(manifest["images"], start=1):
        if not isinstance(row, dict) or row.get("sequence") != sequence:
            raise ValueError("image records are not in unique contiguous quota order")
        image_hash = row.get("image_sha256")
        if not _valid_sha256(image_hash) or image_hash in seen_hashes:
            raise ValueError("image checksums must be valid and unique")
        seen_hashes.add(image_hash)
        source_hash = row.get("source_pdf_sha256")
        source = source_by_hash.get(source_hash)
        if source is None:
            raise ValueError("image provenance points to a source outside the approved manifest")
        rights = source["redistribution"]
        if (
            row.get("source_pdf_id") != source["pdf_id"]
            or row.get("source_license") != rights["license"]
            or row.get("rights_evidence_ref") != rights["evidence_ref"]
            or type(row.get("page_number")) is not int
            or row["page_number"] < MIN_PAGE
            or row["page_number"] > source["page_count"]
            or type(row.get("line_index")) is not int
            or row["line_index"] < 1
            or type(row.get("shard_id")) is not int
            or not 0 <= row["shard_id"] < WORKER_COUNT
            or not isinstance(row.get("engine_version"), str)
            or not row["engine_version"].strip()
        ):
            raise ValueError("image provenance does not match source-specific redistribution clearance")
        provenance = f"{source_hash}:{row['page_number']}:{row['line_index']}"
        if ledger_records[sequence - 1].get("provenance_key") != provenance or provenance in seen_provenance:
            raise ValueError("image provenance keys are malformed or duplicated")
        seen_provenance.add(provenance)
        expected_relative = f"images/{image_hash[:2]}/{image_hash}.png"
        if row.get("relative_path") != expected_relative:
            raise ValueError("image path is not content-addressed by its validated SHA-256")
        image_path = dataset_dir / expected_relative
        if image_path.is_symlink() or not image_path.is_file():
            raise ValueError("a validated dataset image is missing or unsafe")
        try:
            image_path.resolve(strict=True).relative_to(images_dir)
        except ValueError as exc:
            raise ValueError("a dataset image resolves outside the image directory") from exc
        if sha256_file(image_path) != image_hash:
            raise ValueError("a dataset image checksum does not match its content")

    card = _render_card(card_template, dataset_license)
    public_index = {
        "schema_version": 1,
        "dataset_license": dataset_license,
        "image_count": expected_target,
        "images": [
            {"path": row["relative_path"], "sha256": row["image_sha256"]}
            for row in manifest["images"]
        ],
    }
    return {
        "run_dir": run_dir,
        "dataset_dir": dataset_dir,
        "images_dir": images_dir,
        "image_records": manifest["images"],
        "dataset_license": dataset_license,
        "repo_id": repo_id,
        "image_count": expected_target,
        "file_count": expected_target + 2,
        "card": card,
        "public_index": public_index,
    }


def _build_public_payload(validated: dict[str, Any], run_dir: Path) -> Path:
    stage_root = Path(tempfile.mkdtemp(prefix=".lineforge-hf-stage-", dir=run_dir))
    payload = stage_root / "payload"
    payload.mkdir()
    try:
        for record in validated["image_records"]:
            source = validated["dataset_dir"] / record["relative_path"]
            destination = payload / record["relative_path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            # The staging folder is beneath the run directory, so hard links avoid
            # a second copy of a potentially very large, already-validated corpus.
            os.link(source, destination, follow_symlinks=False)
            if sha256_file(destination) != record["image_sha256"]:
                raise ValueError("an image changed while the publication payload was staged")
        (payload / "README.md").write_text(validated["card"], encoding="utf-8")
        (payload / PUBLIC_INDEX_NAME).write_text(
            json.dumps(validated["public_index"], ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        return stage_root
    except BaseException:
        shutil.rmtree(stage_root, ignore_errors=True)
        raise


def publish_dataset(
    run_dir: Path,
    source_manifest_path: Path,
    source_root: Path,
    repo_id: str,
    dataset_license: str,
    card_template: Path,
    *,
    expected_target: int = DEFAULT_TARGET,
    dry_run: bool = False,
    confirm_publish: bool = False,
    token: str | None = None,
    api: Any | None = None,
) -> dict[str, Any]:
    """Preflight locally or publish through an injected/API client after consent."""
    if dry_run and confirm_publish:
        raise ValueError("choose either offline dry-run or confirmed upload, not both")
    validated = validate_publication(
        run_dir, source_manifest_path, source_root, repo_id, dataset_license,
        card_template, expected_target,
    )
    if dry_run:
        return {
            "status": "dry_run_validated",
            "repo_id": repo_id,
            "dataset_license": dataset_license,
            "image_count": validated["image_count"],
            "file_count": validated["file_count"],
        }
    if not confirm_publish:
        raise RuntimeError("refusing remote publication without explicit --confirm-publish authorization")
    if token is None:
        token = require_hf_token()
    if not isinstance(token, str) or not token.strip():
        raise RuntimeError("Hugging Face publication requires a non-empty HF_TOKEN")
    if api is None:
        try:
            from huggingface_hub import HfApi
        except ImportError as exc:
            raise RuntimeError("install the optional huggingface_hub dependency to publish") from exc
        api = HfApi(token=token.strip())

    stage_root: Path | None = None
    try:
        info = api.repo_info(repo_id=repo_id, repo_type="dataset")
        is_private = info.get("private") if isinstance(info, dict) else getattr(info, "private", None)
        if is_private is not False:
            raise RuntimeError("target repository must already exist and be public; refusing to create or change visibility")
        stage_root = _build_public_payload(validated, validated["run_dir"])
        payload = stage_root / "payload"
        api.upload_folder(
            repo_id=repo_id,
            repo_type="dataset",
            folder_path=str(payload),
            commit_message="Publish validated LineForge dataset release",
        )
    except RuntimeError:
        raise
    except Exception as exc:
        # Third-party exceptions can contain request details; do not echo them to
        # a public Actions log or risk exposing credential-bearing diagnostics.
        raise RuntimeError(f"Hugging Face publication stopped ({type(exc).__name__})") from None
    finally:
        if stage_root is not None:
            shutil.rmtree(stage_root, ignore_errors=True)

    return {
        "status": "uploaded",
        "repo_id": repo_id,
        "dataset_license": dataset_license,
        "image_count": validated["image_count"],
        "file_count": validated["file_count"],
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate or publish one LineForge dataset run to Hugging Face")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--source-manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--repo-id", required=True)
    parser.add_argument("--dataset-license", required=True)
    parser.add_argument("--card-template", type=Path, required=True)
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true", help="offline validation only; never contacts Hugging Face")
    mode.add_argument("--confirm-publish", action="store_true", help="explicitly authorize the remote dataset upload")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = publish_dataset(
            args.run_dir,
            args.source_manifest,
            args.source_root,
            args.repo_id,
            args.dataset_license,
            args.card_template,
            expected_target=args.target,
            dry_run=args.dry_run,
            confirm_publish=args.confirm_publish,
        )
    except Exception as exc:
        # Avoid printing local source paths, private provenance, or third-party
        # request diagnostics to GitHub Actions logs.
        print(f"ERROR: Hugging Face publication stopped ({type(exc).__name__})", file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Local-only Telegram source intake for the LineForge dataset pipeline.

This tool invokes the user's existing telegram-file-bridge checkout and local
MTProto session. It never copies credentials/session files into LineForge, Git,
GitHub Actions, or a public payload. Running `fetch` contacts Telegram and
creates a private local staging tree; other commands can be tested offline.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import unicodedata
from pathlib import Path
from typing import Any

try:
    from .lineforge_dataset import atomic_write, load_json, sha256_file, slugify_public_source, validate_source_manifest
except ImportError:  # direct script execution
    from lineforge_dataset import atomic_write, load_json, sha256_file, slugify_public_source, validate_source_manifest  # type: ignore[no-redef]

DEFAULT_CHAT = "@et_bookstore"
LINEFORGE_ROOT = Path(__file__).resolve().parents[1]


def _secure_directory(path: Path, *, must_be_new: bool = False) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError("private staging paths may not be symlinks")
    path = path.resolve()
    if must_be_new and path.exists():
        raise FileExistsError(f"refusing to reuse an existing staging directory: {path}")
    if path == LINEFORGE_ROOT or path.is_relative_to(LINEFORGE_ROOT):
        raise ValueError("Telegram source files and reports must remain outside the LineForge checkout")
    path.mkdir(parents=True, exist_ok=not must_be_new, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _run_bridge(bridge_root: Path, env_file: Path, arguments: list[str]) -> dict[str, Any]:
    command = [sys.executable, "-m", "telegram_file_bridge", "--env-file", str(env_file), *arguments]
    try:
        completed = subprocess.run(
            command, cwd=bridge_root, text=True, capture_output=True, check=False, timeout=86_400,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("Telegram bridge operation timed out; inspect the private staging directory before retrying") from exc
    if completed.returncode != 0:
        # Do not echo child stderr: it may contain local paths or account diagnostics.
        raise RuntimeError(f"Telegram bridge operation failed (exit code {completed.returncode})")
    try:
        value = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Telegram bridge returned an unexpected result; no manifest was created") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Telegram bridge returned a malformed result")
    return value


def _rows(report: Any, description: str) -> list[dict[str, Any]]:
    if not isinstance(report, dict) or not isinstance(report.get("files"), list):
        raise ValueError(f"{description} must be a Telegram PDF report with a files array")
    rows = report["files"]
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError(f"{description} contains a malformed file record")
    return rows


def _message_map(rows: list[dict[str, Any]], description: str) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for row in rows:
        message_id = row.get("message_id")
        if type(message_id) is not int or message_id < 1:
            raise ValueError(f"{description} contains an invalid Telegram message ID")
        if message_id in result:
            raise ValueError(f"{description} contains a duplicate Telegram message ID")
        result[message_id] = row
    return result


def _validate_inventory(inventory: Any) -> list[dict[str, Any]]:
    if not isinstance(inventory, dict) or inventory.get("schema_version") != 1:
        raise ValueError("unsupported Telegram inventory schema")
    if not isinstance(inventory.get("source_chat"), str) or not inventory["source_chat"].strip():
        raise ValueError("Telegram inventory is missing its source channel")
    rows = _rows(inventory, "Telegram inventory")
    seen_ids: set[int] = set()
    for row in rows:
        message_id = row.get("message_id")
        digest = row.get("sha256")
        local_path = row.get("path")
        if type(message_id) is not int or message_id < 1 or message_id in seen_ids:
            raise ValueError("Telegram inventory contains an invalid or repeated message ID")
        seen_ids.add(message_id)
        if row.get("status") is not True and row.get("status") is not False:
            raise ValueError("Telegram inventory status must be an explicit Boolean")
        if row.get("duplicate") is not True and row.get("duplicate") is not False:
            raise ValueError("Telegram inventory duplicate state must be an explicit Boolean")
        if not isinstance(digest, str) or len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
            raise ValueError("Telegram inventory contains an invalid PDF SHA-256")
        if not isinstance(local_path, str) or not local_path.strip():
            raise ValueError("Telegram inventory contains a missing staged PDF path")
        relative = Path(local_path)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Telegram inventory paths must be relative and confined to the private staging root")
        if type(row.get("page_count")) is not int or row["page_count"] < 1:
            raise ValueError("Telegram inventory contains an invalid PDF page count")
        if type(row.get("byte_size")) is not int or row["byte_size"] < 1:
            raise ValueError("Telegram inventory contains an invalid PDF byte size")
    return rows


def validate_fresh_selection(fresh_report: Any, selection_report: Any, chat: str) -> list[dict[str, Any]]:
    """Match the reviewed status overlay to a fresh channel scan by message ID."""
    for description, report in (("fresh scan", fresh_report), ("selection report", selection_report)):
        if not isinstance(report, dict) or report.get("source_chat") != chat:
            raise ValueError(f"{description} does not identify the requested Telegram channel")
    fresh = _message_map(_rows(fresh_report, "fresh scan"), "fresh scan")
    selection = _message_map(_rows(selection_report, "selection report"), "selection report")
    if set(fresh) != set(selection):
        raise ValueError("fresh Telegram inventory differs from the reviewed selection; update the status review first")

    chosen: list[dict[str, Any]] = []
    for message_id in sorted(fresh):
        live_row = fresh[message_id]
        reviewed_row = selection[message_id]
        if reviewed_row.get("status") is not True and reviewed_row.get("status") is not False:
            raise ValueError("every reviewed source must have an explicit Boolean Status value")
        if live_row.get("file_name") != reviewed_row.get("file_name"):
            raise ValueError("a Telegram message's file name changed since the review; refresh the selection")
        old_size = reviewed_row.get("size_bytes")
        current_size = live_row.get("size_bytes")
        if old_size is not None and current_size is not None and old_size != current_size:
            raise ValueError("a Telegram message's reported size changed since the review; refresh the selection")
        if reviewed_row.get("status") is not True:
            continue
        # A known byte-identical copy must not be simultaneously opted in.
        if reviewed_row.get("duplicate") is True:
            raise ValueError("the selection marks a known byte-identical copy Status=true; correct the review")
        row = dict(live_row)
        row["status"] = True
        row["reviewed_sha256"] = reviewed_row.get("sha256")
        row["reviewed_page_count"] = reviewed_row.get("page_count")
        chosen.append(row)
    if not chosen:
        raise ValueError("the reviewed selection contains no Status=true Telegram PDFs")
    return chosen


def _pdf_page_count(path: Path) -> int:
    try:
        result = subprocess.run(["pdfinfo", str(path)], text=True, capture_output=True, check=False, timeout=60)
    except FileNotFoundError as exc:
        raise RuntimeError("pdfinfo is required to inspect downloaded PDFs") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PDF page-count inspection timed out") from exc
    if result.returncode != 0:
        raise ValueError("a downloaded source is not a readable PDF")
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                count = int(line.split(":", 1)[1].strip())
            except ValueError as exc:
                raise ValueError("pdfinfo returned an invalid page count") from exc
            if count < 1:
                break
            return count
    raise ValueError("pdfinfo did not report a positive page count")


def fetch_sources(
    bridge_root: Path,
    env_file: Path,
    selection_report_path: Path,
    stage_root: Path,
    chat: str = DEFAULT_CHAT,
) -> dict[str, Any]:
    """Fresh-scan then download only reviewed Status=true items into a new private tree."""
    bridge_root = bridge_root.expanduser().resolve(strict=True)
    env_file = env_file.expanduser().resolve(strict=True)
    selection_report_path = selection_report_path.expanduser()
    if not (bridge_root / "telegram_file_bridge" / "__main__.py").is_file():
        raise FileNotFoundError("the configured local Telegram bridge checkout is not available")
    if selection_report_path.is_symlink() or not selection_report_path.is_file():
        raise ValueError("selection report must be a regular local JSON file")
    selection_report_path = selection_report_path.resolve(strict=True)
    stage_root = _secure_directory(stage_root, must_be_new=True)
    report_dir = stage_root / "fresh-scan"
    download_dir = stage_root / "downloads"
    report_dir.mkdir(mode=0o700)
    download_dir.mkdir(mode=0o700)

    fresh_result = _run_bridge(
        bridge_root,
        env_file,
        ["report", "--chat", chat, "--out", str(report_dir), "--format", "json"],
    )
    report_files = fresh_result.get("report_files")
    if not isinstance(report_files, list) or len(report_files) != 1 or not isinstance(report_files[0], str):
        raise RuntimeError("Telegram bridge did not return exactly one machine-readable inventory report")
    fresh_report_path = Path(report_files[0]).resolve(strict=True)
    try:
        fresh_report_path.relative_to(report_dir.resolve())
    except ValueError as exc:
        raise ValueError("fresh Telegram inventory report escaped the private staging directory") from exc
    fresh_report = load_json(fresh_report_path)
    selection_report = load_json(selection_report_path)
    chosen = validate_fresh_selection(fresh_report, selection_report, chat)

    files: list[dict[str, Any]] = []
    seen_by_hash: dict[str, int] = {}
    for index, row in enumerate(chosen, start=1):
        message_id = row["message_id"]
        result = _run_bridge(
            bridge_root,
            env_file,
            ["download", "--chat", chat, "--message-id", str(message_id), "--out", str(download_dir)],
        )
        if result.get("action") != "download" or result.get("message_id") != message_id:
            raise RuntimeError("Telegram bridge downloaded a different source than requested")
        raw_local_path = Path(str(result.get("local_path") or "")).expanduser()
        if raw_local_path.is_symlink():
            raise ValueError("Telegram returned a symlink instead of a regular PDF")
        local_path = raw_local_path.resolve(strict=True)
        try:
            local_path.relative_to(download_dir.resolve())
        except ValueError as exc:
            raise ValueError("Telegram download escaped the private source staging directory") from exc
        if local_path.is_symlink() or not local_path.is_file() or local_path.suffix.casefold() != ".pdf":
            raise ValueError("Telegram returned a missing, unsafe, or non-PDF source file")
        try:
            os.chmod(local_path, 0o600)
        except OSError:
            pass
        with local_path.open("rb") as stream:
            data_prefix = stream.read(1024)
        if b"%PDF-" not in data_prefix:
            raise ValueError("downloaded file does not contain a PDF signature")
        digest = sha256_file(local_path)
        reviewed_digest = row.get("reviewed_sha256")
        if reviewed_digest is not None and reviewed_digest != digest:
            raise ValueError("a downloaded source checksum differs from the reviewed inventory; refresh the review")
        page_count = _pdf_page_count(local_path)
        reviewed_pages = row.get("reviewed_page_count")
        if reviewed_pages is not None and reviewed_pages != page_count:
            raise ValueError("a downloaded source page count differs from the reviewed inventory; refresh the review")
        expected_size = row.get("size_bytes")
        if expected_size is not None and (type(expected_size) is not int or expected_size != local_path.stat().st_size):
            raise ValueError("downloaded PDF size differs from the live Telegram inventory")
        if result.get("size_bytes") != local_path.stat().st_size:
            raise ValueError("download result size does not match the staged PDF")
        canonical_message = seen_by_hash.setdefault(digest, message_id)
        is_duplicate = canonical_message != message_id
        files.append({
            "message_id": message_id,
            "file_name": str(row.get("file_name") or local_path.name),
            "path": str(local_path.relative_to(stage_root)),
            "sha256": digest,
            "page_count": page_count,
            "byte_size": local_path.stat().st_size,
            "status": True,
            "duplicate": is_duplicate,
            "canonical_message_id": canonical_message,
        })
        print(f"staged reviewed source {index}/{len(chosen)}", flush=True)

    inventory = {
        "schema_version": 1,
        "source_chat": chat,
        "fresh_report_sha256": sha256_file(fresh_report_path),
        "selection_report_sha256": sha256_file(selection_report_path),
        "files": files,
    }
    inventory_path = stage_root / "telegram-inventory.json"
    atomic_write(inventory_path, json.dumps(inventory, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    try:
        os.chmod(inventory_path, 0o600)
    except OSError:
        pass
    unique = [item for item in files if not item["duplicate"]]
    return {
        "status": "staged_locally",
        "selected_status_true": len(chosen),
        "downloaded": len(files),
        "unique_pdf_contents": len(unique),
        "at_least_10_pages": sum(item["page_count"] >= 10 for item in unique),
        "inventory": str(inventory_path),
        "source_root": str(stage_root),
        "publishable": False,
    }


def rights_template(inventory_path: Path, output_path: Path) -> dict[str, Any]:
    inventory_path = inventory_path.expanduser()
    if inventory_path.is_symlink() or not inventory_path.is_file():
        raise ValueError("inventory must be a regular local JSON file")
    inventory_path = inventory_path.resolve(strict=True)
    inventory = load_json(inventory_path)
    inventory_files = _validate_inventory(inventory)
    candidates = [
        row for row in inventory_files
        if row.get("status") is True and row.get("duplicate") is False and type(row.get("page_count")) is int and row["page_count"] >= 10
    ]
    if not candidates:
        raise ValueError("no unique Status=true PDFs with at least 10 pages are available for rights review")
    rows = [{
        "message_id": row["message_id"],
        "pdf_sha256": row["sha256"],
        "allowed": False,
        "source_license": "",
        "dataset_license": "",
        "evidence_ref": "",
        "public_source_name": "",
    } for row in candidates]
    document = {
        "schema_version": 1,
        "inventory_sha256": sha256_file(inventory_path),
        "review_note": "Set allowed=true only with source-specific evidence that permits redistribution of derived line images. Keep evidence references private.",
        "files": rows,
    }
    output_path = output_path.expanduser()
    if output_path.is_symlink():
        raise ValueError("rights review output may not be a symlink")
    output_path = output_path.resolve()
    try:
        output_path.relative_to(inventory_path.parent.resolve())
    except ValueError as exc:
        raise ValueError("rights review must remain inside the private source staging root") from exc
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing rights review: {output_path}")
    atomic_write(output_path, json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass
    return {"rights_review_template": str(output_path), "source_count": len(rows), "publishable": False}


def prepare_manifest(inventory_path: Path, rights_review_path: Path, output_path: Path) -> dict[str, Any]:
    inventory_path = inventory_path.expanduser()
    rights_review_path = rights_review_path.expanduser()
    if inventory_path.is_symlink() or rights_review_path.is_symlink():
        raise ValueError("inventory and rights review may not be symlinks")
    inventory_path = inventory_path.resolve(strict=True)
    rights_review_path = rights_review_path.resolve(strict=True)
    inventory = load_json(inventory_path)
    review = load_json(rights_review_path)
    inventory_files = _validate_inventory(inventory)
    if not isinstance(review, dict) or review.get("schema_version") != 1:
        raise ValueError("unsupported rights review schema")
    if review.get("inventory_sha256") != sha256_file(inventory_path):
        raise ValueError("rights review is not bound to this exact Telegram inventory")

    candidates = [
        row for row in inventory_files
        if row.get("status") is True and row.get("duplicate") is False and type(row.get("page_count")) is int and row["page_count"] >= 10
    ]
    review_rows = _rows({"files": review.get("files")}, "rights review")
    rights_by_id: dict[int, dict[str, Any]] = {}
    for row in review_rows:
        message_id = row.get("message_id")
        if type(message_id) is not int or message_id in rights_by_id:
            raise ValueError("rights review has an invalid or duplicate Telegram message ID")
        rights_by_id[message_id] = row
    candidate_ids = {row["message_id"] for row in candidates}
    if set(rights_by_id) != candidate_ids:
        raise ValueError("rights review must cover exactly every unique eligible source in the inventory")

    stage_root = inventory_path.parent.resolve()
    try:
        rights_review_path.relative_to(stage_root)
    except ValueError as exc:
        raise ValueError("rights review must remain inside the private Telegram staging root") from exc
    output_path = output_path.expanduser()
    if output_path.is_symlink():
        raise ValueError("output manifest may not be a symlink")
    output_path = output_path.resolve()
    try:
        output_path.relative_to(stage_root)
    except ValueError as exc:
        raise ValueError("source manifest must remain inside the private Telegram staging root") from exc
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite existing source manifest: {output_path}")

    inventory_by_id = {row["message_id"]: row for row in inventory_files}
    manifest_files: list[dict[str, Any]] = []
    seen_slugs: set[str] = set()
    for message_id in sorted(candidate_ids):
        source = inventory_by_id[message_id]
        rights = rights_by_id[message_id]
        if rights.get("pdf_sha256") != source.get("sha256"):
            raise ValueError("rights review checksum does not match the staged PDF")
        if rights.get("allowed") is not True:
            raise ValueError("every eligible source needs explicit approval to redistribute derived images")
        for field in ("source_license", "evidence_ref", "public_source_name"):
            if not isinstance(rights.get(field), str) or not rights[field].strip():
                raise ValueError(f"every eligible source requires a non-empty rights-review {field}")
        book_name = unicodedata.normalize("NFKC", rights["public_source_name"]).strip()
        book_slug = slugify_public_source(book_name)
        if book_slug.casefold() in seen_slugs:
            raise ValueError("public source book names must map to distinct directory names")
        seen_slugs.add(book_slug.casefold())
        raw_local_path = stage_root / source["path"]
        if raw_local_path.is_symlink():
            raise ValueError("a staged PDF may not be a symlink")
        local_path = raw_local_path.resolve(strict=True)
        try:
            local_path.relative_to(stage_root)
        except ValueError as exc:
            raise ValueError("a staged PDF path escaped the private source root") from exc
        if local_path.is_symlink() or not local_path.is_file() or sha256_file(local_path) != source["sha256"]:
            raise ValueError("a staged PDF is missing, unsafe, or changed since the inventory was written")
        actual_pages = _pdf_page_count(local_path)
        if actual_pages != source["page_count"] or actual_pages < 10:
            raise ValueError("a staged PDF page count changed or is below the page-10 floor")
        redistribution = {
            "allowed": True,
            "license": rights["source_license"].strip(),
            "evidence_ref": rights["evidence_ref"].strip(),
        }
        dataset_license = rights.get("dataset_license")
        if dataset_license is not None and str(dataset_license).strip():
            redistribution["dataset_license"] = str(dataset_license).strip()
        manifest_files.append({
            "pdf_id": f"telegram-{message_id}",
            "pdf_sha256": source["sha256"],
            "path": source["path"],
            "page_count": actual_pages,
            "byte_size": local_path.stat().st_size,
            "status": True,
            "public_source_name": book_name,
            "public_source_slug": book_slug,
            "redistribution": redistribution,
        })

    document = {
        "schema_version": 1,
        "dataset_id": "geez-ocr-telegram-source-set",
        "revision": sha256_file(inventory_path),
        "source_chat": inventory.get("source_chat"),
        "files": manifest_files,
    }
    # Validate the manifest exactly as the later 19-worker dispatcher will.
    validate_source_manifest(document, stage_root, verify_files=True, allowed_root=stage_root)
    atomic_write(output_path, json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    try:
        os.chmod(output_path, 0o600)
    except OSError:
        pass
    return {"status": "manifest_prepared", "eligible_sources": len(manifest_files), "manifest": str(output_path), "publishable": False}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Local Telegram intake and rights-gated manifest preparation for LineForge")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="fresh-scan the channel and download only reviewed Status=true PDFs locally")
    fetch.add_argument("--bridge-root", type=Path, default=Path(os.environ.get("LINEFORGE_TELEGRAM_BRIDGE_ROOT", "/workspace/telegram-file-bridge")))
    fetch.add_argument(
        "--env-file", type=Path,
        default=Path(os.environ["LINEFORGE_TELEGRAM_ENV_FILE"]) if os.environ.get("LINEFORGE_TELEGRAM_ENV_FILE") else None,
    )
    fetch.add_argument("--selection-report", type=Path, default=Path(os.environ["LINEFORGE_TELEGRAM_SELECTION_REPORT"]) if os.environ.get("LINEFORGE_TELEGRAM_SELECTION_REPORT") else None, required=not bool(os.environ.get("LINEFORGE_TELEGRAM_SELECTION_REPORT")))
    fetch.add_argument("--stage-root", type=Path, default=Path(os.environ["LINEFORGE_TELEGRAM_STAGE_ROOT"]) if os.environ.get("LINEFORGE_TELEGRAM_STAGE_ROOT") else None, required=not bool(os.environ.get("LINEFORGE_TELEGRAM_STAGE_ROOT")))
    fetch.add_argument("--chat", default=os.environ.get("LINEFORGE_TELEGRAM_CHAT", DEFAULT_CHAT))

    template = commands.add_parser("rights-template", help="write a private per-source rights-review template")
    template.add_argument("--inventory", type=Path, required=True)
    template.add_argument("--output", type=Path, required=True)

    prepare = commands.add_parser("prepare", help="create a private dispatcher manifest after all rights entries are cleared")
    prepare.add_argument("--inventory", type=Path, required=True)
    prepare.add_argument("--rights-review", type=Path, required=True)
    prepare.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "fetch":
            env_file = args.env_file or args.bridge_root / ".env"
            result = fetch_sources(args.bridge_root, env_file, args.selection_report, args.stage_root, args.chat)
        elif args.command == "rights-template":
            result = rights_template(args.inventory, args.output)
        else:
            result = prepare_manifest(args.inventory, args.rights_review, args.output)
    except Exception as exc:
        print(f"lineforge-telegram-stage: operation stopped ({type(exc).__name__}): {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

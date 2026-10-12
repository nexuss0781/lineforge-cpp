#!/usr/bin/env python3
"""Fetch only approved Telegram PDFs at dispatch time into a private run directory.

The input manifest is metadata and rights review only. It must contain Telegram
source IDs, reviewed checksums/page counts, public labels, and explicit source-
specific redistribution rights. Telegram files are never staged in the repository.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

try:
    from .lineforge_dataset import atomic_write, load_json, sha256_file, validate_source_manifest
except ImportError:  # direct script execution
    from lineforge_dataset import atomic_write, load_json, sha256_file, validate_source_manifest  # type: ignore[no-redef]

LINEFORGE_ROOT = Path(__file__).resolve().parents[1]
TELEGRAM_ID_RE = re.compile(r"telegram-([1-9][0-9]*)\Z")
DEFAULT_CHAT = "@et_bookstore"
DOWNLOAD_CONCURRENCY = 4


def _normalized_chat(value: str) -> str:
    return value.strip().lstrip("@").casefold()


def load_reviewed_sources(
    manifest_path: Path,
    *,
    chat: str = DEFAULT_CHAT,
    expected_pdf_count: int | None = None,
    require_dataset_license: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate metadata and rights before any Telegram connection or download."""
    manifest_path = manifest_path.expanduser()
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise ValueError("reviewed source manifest must be a regular local JSON file")
    manifest_path = manifest_path.resolve(strict=True)
    document = load_json(manifest_path)
    if not isinstance(document, dict):
        raise ValueError("reviewed source manifest must be a JSON object")
    source_chat = document.get("source_chat")
    if not isinstance(source_chat, str) or _normalized_chat(source_chat) != _normalized_chat(chat):
        raise ValueError("reviewed source manifest does not match the configured Telegram channel")

    # Paths are rewritten to fresh runtime files after download. Validate all
    # other manifest fields, including explicit redistribution rights, now.
    sources = validate_source_manifest(document, manifest_path.parent, verify_files=False)
    if not sources:
        raise ValueError("reviewed source manifest has no eligible PDFs (page count must be at least 10)")

    seen_message_ids: set[int] = set()
    dataset_licenses: set[str] = set()
    for index, source in enumerate(sources, start=1):
        pdf_id = source.get("pdf_id")
        match = TELEGRAM_ID_RE.fullmatch(pdf_id) if isinstance(pdf_id, str) else None
        if match is None:
            raise ValueError(f"eligible source {index} must use a telegram-<message_id> pdf_id")
        message_id = int(match.group(1))
        if message_id in seen_message_ids:
            raise ValueError("reviewed source manifest repeats a Telegram message ID")
        seen_message_ids.add(message_id)
        if "message_id" in source and (type(source["message_id"]) is not int or source["message_id"] != message_id):
            raise ValueError("reviewed source manifest has conflicting Telegram message identifiers")
        byte_size = source.get("byte_size")
        if byte_size is not None and (type(byte_size) is not int or byte_size < 1):
            raise ValueError(f"eligible source {index} has an invalid byte_size")
        if require_dataset_license:
            rights = source.get("redistribution")
            license_id = rights.get("dataset_license") if isinstance(rights, dict) else None
            if not isinstance(license_id, str) or not license_id.strip():
                raise ValueError(f"eligible source {index} has no reviewed dataset_license clearance")
            license_id = license_id.strip()
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.+-]{0,63}", license_id):
                raise ValueError(f"eligible source {index} has an invalid dataset_license identifier")
            dataset_licenses.add(license_id)
        source["message_id"] = message_id

    if require_dataset_license and len(dataset_licenses) != 1:
        raise ValueError("all eligible sources must explicitly clear the same dataset license before publication")

    if expected_pdf_count is not None:
        if type(expected_pdf_count) is not int or expected_pdf_count < 1:
            raise ValueError("expected_pdf_count must be a positive integer")
        if len(sources) != expected_pdf_count:
            raise ValueError(
                f"reviewed manifest contains {len(sources)} eligible unique PDFs; "
                f"the dispatch expects exactly {expected_pdf_count}"
            )
    return document, sources


def _pdf_page_count(path: Path) -> int:
    try:
        result = subprocess.run(
            ["pdfinfo", str(path)], text=True, capture_output=True, check=False, timeout=60,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pdfinfo is required to verify downloaded Telegram PDFs") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("PDF page-count inspection timed out") from exc
    if result.returncode != 0:
        raise ValueError("a downloaded Telegram source is not a readable PDF")
    for line in result.stdout.splitlines():
        if line.startswith("Pages:"):
            try:
                pages = int(line.split(":", 1)[1].strip())
            except ValueError as exc:
                raise ValueError("pdfinfo returned an invalid page count") from exc
            if pages > 0:
                return pages
    raise ValueError("pdfinfo did not report a positive page count")


def _message_pdf_metadata(message: Any, source: dict[str, Any]) -> None:
    if message is None or int(getattr(message, "id", -1)) != source["message_id"]:
        raise ValueError("Telegram returned a different or missing source message")
    media_file = getattr(message, "file", None)
    document = getattr(message, "document", None)
    if document is None:
        document = getattr(getattr(message, "media", None), "document", None)
    if document is None or media_file is None:
        raise ValueError("selected Telegram message does not contain a document")
    file_name = str(getattr(media_file, "name", "") or "")
    mime_type = str(getattr(media_file, "mime_type", "") or "").casefold().split(";", 1)[0].strip()
    if not file_name.casefold().endswith(".pdf") and mime_type not in {"application/pdf", "application/x-pdf"}:
        raise ValueError("selected Telegram document is not identified as a PDF")
    live_size = getattr(media_file, "size", None)
    expected_size = source.get("byte_size")
    if expected_size is not None and live_size is not None and live_size != expected_size:
        raise ValueError("Telegram source size changed since the reviewed manifest was prepared")


def _secure_output_root(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise ValueError("Telegram runtime staging root may not be a symlink")
    path = path.resolve()
    if path == LINEFORGE_ROOT or path.is_relative_to(LINEFORGE_ROOT):
        raise ValueError("Telegram PDFs must remain outside the LineForge checkout")
    if path.exists():
        raise FileExistsError(f"refusing to reuse a Telegram runtime staging directory: {path}")
    path.mkdir(parents=True, mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path.resolve(strict=True)


async def fetch_with_client(
    client: Any,
    document: dict[str, Any],
    sources: list[dict[str, Any]],
    output_root: Path,
    *,
    chat: str = DEFAULT_CHAT,
) -> dict[str, Any]:
    """Download each validated, unique PDF once and create a private runtime manifest."""
    output_root = _secure_output_root(output_root)
    pdf_root = output_root / "pdfs"
    pdf_root.mkdir(mode=0o700)
    semaphore = asyncio.Semaphore(DOWNLOAD_CONCURRENCY)

    async def fetch_one(index: int, source: dict[str, Any]) -> dict[str, Any]:
        digest = source["pdf_sha256"]
        final_path = pdf_root / f"{digest}.pdf"
        partial_path = pdf_root / f".{digest}.partial"
        async with semaphore:
            # A retry within a single staging run reuses only bytes already
            # verified against the reviewed SHA-256; duplicate manifest hashes
            # are rejected before reaching this point.
            if final_path.exists():
                if not final_path.is_file() or sha256_file(final_path) != digest:
                    raise ValueError("cached runtime PDF does not match its reviewed checksum")
                downloaded_path = final_path
            else:
                message = await client.get_messages(chat, ids=source["message_id"])
                _message_pdf_metadata(message, source)
                try:
                    if partial_path.exists():
                        partial_path.unlink()
                    result_path = await client.download_media(message, file=str(partial_path))
                    if not result_path:
                        raise OSError("Telegram did not return a downloaded PDF path")
                    actual_path = Path(result_path).expanduser().resolve(strict=True)
                    try:
                        actual_path.relative_to(pdf_root.resolve())
                    except ValueError as exc:
                        raise ValueError("Telegram download escaped the private runtime staging directory") from exc
                    if actual_path != partial_path.resolve():
                        raise ValueError("Telegram wrote the selected PDF to an unexpected path")
                    if not actual_path.is_file():
                        raise OSError("Telegram download did not produce a regular file")
                    with actual_path.open("rb") as stream:
                        if b"%PDF-" not in stream.read(1024):
                            raise ValueError("downloaded Telegram file does not have a PDF signature")
                    actual_size = actual_path.stat().st_size
                    expected_size = source.get("byte_size")
                    if expected_size is not None and actual_size != expected_size:
                        raise ValueError("downloaded PDF size differs from the reviewed manifest")
                    if sha256_file(actual_path) != digest:
                        raise ValueError("downloaded PDF checksum differs from the reviewed manifest")
                    actual_pages = _pdf_page_count(actual_path)
                    if actual_pages != source["page_count"]:
                        raise ValueError("downloaded PDF page count differs from the reviewed manifest")
                    try:
                        os.chmod(actual_path, stat.S_IRUSR | stat.S_IWUSR)
                    except OSError:
                        pass
                    os.replace(actual_path, final_path)
                    downloaded_path = final_path
                except Exception:
                    try:
                        partial_path.unlink()
                    except FileNotFoundError:
                        pass
                    raise

        entry = dict(source)
        entry.pop("message_id", None)
        entry["path"] = str(downloaded_path.relative_to(output_root))
        print(f"Fetched and verified approved PDF {index}/{len(sources)}", flush=True)
        return entry

    fetched = await asyncio.gather(*(fetch_one(index, source) for index, source in enumerate(sources, start=1)))
    runtime_manifest = dict(document)
    runtime_manifest["files"] = fetched
    manifest_path = output_root / "manifest.json"
    atomic_write(
        manifest_path,
        json.dumps(runtime_manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8") + b"\n",
    )
    try:
        os.chmod(manifest_path, 0o600)
    except OSError:
        pass
    return {
        "status": "fetched_and_verified",
        "selected_pdf_count": len(sources),
        "downloaded_pdf_count": len(fetched),
        "unique_pdf_count": len({entry["pdf_sha256"] for entry in fetched}),
        "manifest": str(manifest_path),
        "source_root": str(output_root),
        "publishable": False,
    }


async def fetch_from_telegram(
    manifest_path: Path,
    output_root: Path,
    *,
    chat: str = DEFAULT_CHAT,
    expected_pdf_count: int | None = None,
    require_dataset_license: bool = False,
) -> dict[str, Any]:
    """Connect with a pre-authorized Telegram StringSession; never prompt interactively."""
    document, sources = load_reviewed_sources(
        manifest_path, chat=chat, expected_pdf_count=expected_pdf_count,
        require_dataset_license=require_dataset_license,
    )
    try:
        from telethon import TelegramClient
        from telethon.sessions import StringSession
    except ImportError as exc:
        raise RuntimeError("Telethon is required; install requirements-telegram-fetch.txt") from exc

    api_id_raw = os.environ.get("TELEGRAM_API_ID", "").strip()
    api_hash = os.environ.get("TELEGRAM_API_HASH", "").strip()
    session_string = os.environ.get("TELEGRAM_SESSION", "").strip()
    if not api_id_raw or not api_hash or not session_string:
        raise RuntimeError("TELEGRAM_API_ID, TELEGRAM_API_HASH, and TELEGRAM_SESSION are required")
    try:
        api_id = int(api_id_raw)
    except ValueError as exc:
        raise RuntimeError("TELEGRAM_API_ID must be an integer") from exc
    if api_id <= 0:
        raise RuntimeError("TELEGRAM_API_ID must be greater than zero")

    client = TelegramClient(StringSession(session_string), api_id, api_hash)
    try:
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("configured Telegram session is not authorized; interactive login is disabled")
        return await fetch_with_client(client, document, sources, output_root, chat=chat)
    finally:
        await client.disconnect()


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fetch approved Telegram PDFs once for a LineForge runtime")
    parser.add_argument("--manifest", required=True, type=Path, help="private rights-reviewed Telegram source metadata manifest")
    parser.add_argument("--output-root", required=True, type=Path, help="new private staging directory outside the checkout")
    parser.add_argument("--chat", default=DEFAULT_CHAT, help="Telegram source channel (must match the reviewed manifest)")
    parser.add_argument("--expected-pdf-count", type=int, default=None, help="fail closed unless exactly this many unique eligible PDFs are reviewed")
    parser.add_argument("--require-dataset-license", action="store_true", help="require one exact, shared dataset license before connecting to Telegram")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        result = asyncio.run(fetch_from_telegram(
            args.manifest, args.output_root, chat=args.chat, expected_pdf_count=args.expected_pdf_count,
            require_dataset_license=args.require_dataset_license,
        ))
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Do not print environment values, Telethon objects, source filenames, or
        # account diagnostics into the GitHub Actions log.
        print(f"Telegram fetch failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({key: value for key, value in result.items() if key not in {"manifest", "source_root"}}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

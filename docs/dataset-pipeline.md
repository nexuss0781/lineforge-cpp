# LineForge dataset pipeline

## Scope and safety boundary

This is a **LineForge-owned data-preparation workflow**, not an NTF training implementation. It extracts individual line-crop PNGs with provenance; it does not perform OCR or transcription, create character boxes, train Tesseract, or merge model weights. NTF remains an external, task-agnostic framework.

The pipeline has a local staged-PDF source interface and a local SQLite quota ledger. The LineForge dispatcher starts **exactly 19 worker processes plus one live coordinator on the same trusted host**. Workers share one filesystem and one atomic SQLite ledger, so the exact-count cap is enforced without pretending isolated hosted matrix jobs share storage. The manual GitHub Action invokes this dispatcher on a dedicated self-hosted runner. It does not access Telegram, fetch source PDFs, transfer a Telegram session, or publish dataset content.

No dispatch was started during implementation. The Action is manual-only and fails closed until the runner variables and reviewed private source staging are configured. It has no automatic push, pull-request, schedule, or Hugging Face trigger.

## Source manifest and rights gate

Keep `manifest.json` and all PDFs in a private staging directory outside the repository checkout. Every path must resolve inside the configured source root. Every input entry must provide:

- `pdf_id`: stable private source identifier;
- `pdf_sha256`: lowercase SHA-256 for the exact staged PDF bytes;
- `path`: local path, preferably relative to the manifest;
- `page_count`: verified page count;
- `status`: literal JSON `true`;
- `redistribution.allowed`: literal JSON `true`;
- `redistribution.license` and `redistribution.evidence_ref`: documented, source-specific rights evidence.

The pipeline re-hashes eligible PDFs and checks the actual PDF page count before processing. Repeated PDF hashes are rejected; PDFs shorter than 10 pages are omitted. `Status=true` is only a source-selection status and is **not** proof of permission to redistribute derived images. Rights evidence must explicitly allow that redistribution.

Example shape (placeholders only; keep the real manifest private):

```json
{
  "schema_version": 1,
  "dataset_id": "approved-source-set",
  "revision": "immutable-source-report-revision",
  "files": [
    {
      "pdf_id": "private-source-id",
      "pdf_sha256": "<64 lowercase hex characters>",
      "path": "book.pdf",
      "page_count": 240,
      "byte_size": 12345678,
      "status": true,
      "redistribution": {
        "allowed": true,
        "license": "documented license",
        "evidence_ref": "private approval record"
      }
    }
  ]
}
```

## LineForge execution

A local, fully staged run uses one command:

```bash
python3 scripts/lineforge_dataset.py dispatch \
  --input-manifest /secure/staging/manifest.json \
  --source-root /secure/staging \
  --run-dir /secure/lineforge-output/run-<unique-id> \
  --run-seed <approved-random-seed> \
  --run-id <unique-id> \
  --linecrop /path/to/linecrop \
  --target 100024 \
  --dpi 300
```

The dispatcher refuses an existing run directory and refuses source or manifest paths outside the configured source root. It checks the PDF utilities and LineForge binary, validates checksums and redistribution fields, assigns whole PDFs once across 19 deterministic page-balanced shards, creates one shared quota ledger, starts a live monitor, and launches all 19 workers. The action’s target is fixed at **100,024**; the local CLI accepts a smaller target for tests and approved pilots.

Each PDF's candidate pages are shuffled deterministically from the run seed and PDF checksum. Sampling prefers the middle 25%–75% interval; if that interval contains no page at or beyond page 10, sampling falls back to the available pages 10 onward. Page numbers are one-based. A worker renders one page at a time with `pdftoppm`, runs LineForge, and retains individual line PNGs. A conservative non-OCR screen rejects review-flagged, sparse, overly dense, non-line-shaped, and some figure-like crops. This remains a **heuristic, not a validated figure detector**; human review on a representative sample is required before production.

The ledger deduplicates image bytes and source-page-line provenance, serializes each acceptance, and refuses to commit above the target. Workers check the shared count between pages and candidates and record page number, source ID and checksum, line index, shard, engine version, source license, and rights-evidence reference. The reducer requires 19 distinct successful worker results, consistent run/input identities, verified image bytes and provenance, page >=10, and exactly the target number of unique images. Its manifest remains `publishable=false`.

## Manual GitHub Action

The manual workflow is `.github/workflows/dataset-dispatch.yml`. It is restricted to `main`, has read-only repository permissions, accepts a deterministic run seed, and executes the same local dispatcher in a single job. It uses the dedicated runner label set:

```text
self-hosted, linux, x64, lineforge-dataset
```

Before enabling dispatch, provision a dedicated and trusted runner with CMake, a C++17 compiler, libjpeg/libpng development files, Python 3 and Pillow, `pdfinfo`, and `pdftoppm`. Do not expose the runner to untrusted pull-request or arbitrary-code workflows. Configure repository Actions variables:

- `LINEFORGE_SOURCE_ROOT`: private, read-only staged source directory containing `manifest.json` and the eligible PDFs;
- `LINEFORGE_OUTPUT_ROOT`: separate persistent storage directory, outside both the source tree and the checkout.

The dispatcher records the run under `$LINEFORGE_OUTPUT_ROOT/<run-id>/`. It retains the image tree, 19 worker result files, SQLite quota ledger, dataset manifest, and dispatch summary on that trusted runner. There is deliberately **no Actions artifact upload**: the individual crop set may be large and contains private provenance. Protect, back up, and retain this output according to the source rights and the runner's data-handling rules.

The local SQLite ledger is valid only on a shared filesystem; do not split these workers into hosted matrix jobs unless a separately approved shared atomic quota and stop service replaces the local ledger and is tested end-to-end. The current design keeps the workers, monitor, and output co-located instead.

## Publication is a separate, inactive gate

`require_hf_token()` only reads the `HF_TOKEN` environment variable for a future explicitly approved publisher. There is currently **no Hugging Face upload command or Action step**, and no code path reads this value during preparation or dispatch. Do not place an HF token in the workflow-wide environment or ordinary CI. The previously exposed token must be revoked and never reused; a fresh `HF_TOKEN` Actions secret is only needed if a separately reviewed publisher is later implemented and approved.

No public data upload is authorized by running this preparation Action. Before publication, confirm source rights for every PDF, choose a dataset license consistent with those rights, review the exact output manifest and sample crops, and obtain separate approval for the exact target repository and payload. Cross-run image deduplication also requires a persistent hash index of previously published images; this pipeline currently deduplicates within one run only.

## Current blockers

- No source PDF set is staged on the dedicated runner, and no approved `LINEFORGE_SOURCE_ROOT` / `LINEFORGE_OUTPUT_ROOT` runner configuration is present here.
- The latest candidate report noted during preparation lists 421 `Status=true` entries without per-PDF checksums; the earlier figure of 407 candidates is unverified. A checksum-verified manifest and the PDF bytes are required.
- Source-specific redistribution evidence is incomplete. Figures are filtered heuristically, not validated on a human-reviewed real-corpus sample.
- The target Hugging Face dataset currently has no declared license. No publisher has been implemented, and no data upload is part of the Action.
- The exposed HF token must be revoked and rotated before any future publication workflow is considered.

Therefore, code and tests can be prepared now, but **the dataset dispatch is not ready to run** until the source files, rights evidence, dedicated runner, and private output storage are configured and reviewed. The implementation task does not start the workflow.

## Validation

The Python suite covers exact 19-shard assignment, duplicate input rejection, source-root confinement, rights and checksum checks, deterministic page sampling, page >=10, crop-filter fixtures, concurrent exact-quota enforcement, unique-image/provenance reduction, malformed or missing workers, and an end-to-end synthetic PDF dispatch that starts 19 worker processes plus one live coordinator. Synthetic tests do not establish real-book figure precision or legal redistribution rights.

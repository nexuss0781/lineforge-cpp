# LineForge dataset pipeline

## Scope and safety boundary

This is a **LineForge-owned data-preparation workflow**, not an NTF training implementation. It extracts individual line-crop PNGs with provenance; it does not perform OCR or transcription, create character boxes, train Tesseract, or merge model weights. NTF remains an external, task-agnostic framework.

The pipeline has a local staged-PDF source interface and a local SQLite quota ledger. The LineForge dispatcher starts **exactly 19 worker processes plus one live coordinator on the same trusted host**. Workers share one filesystem and one atomic SQLite ledger, so the exact-count cap is enforced without pretending isolated hosted matrix jobs share storage. The manual GitHub Action invokes this dispatcher on a dedicated self-hosted runner. It does not access Telegram, fetch source PDFs, transfer a Telegram session, or publish dataset content unless the separate publication input is selected.

No dispatch or upload was started during this implementation. The Action is manual-only, restricted to `main`, and has no automatic push, pull-request, schedule, or Hugging Face trigger. Its optional `publish_to_hf` input defaults to `false`.

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

Publication requires one additional, reviewed field on **every eligible source entry**:

- `redistribution.dataset_license`: the exact Hugging Face/SPDX-style license identifier selected for the released line-crop dataset. The publisher requires a literal exact match with `HF_DATASET_LICENSE`; it does not infer compatibility, translate licenses, or accept a merely similar identifier.

This exact-match policy is intentionally conservative. If a source is not explicitly cleared for redistribution of derived images under the chosen dataset license, publishing stops. Keep evidence references private; the publisher checks them locally but never uploads them.

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
        "license": "documented source license",
        "dataset_license": "<explicit dataset license identifier>",
        "evidence_ref": "private approval record"
      }
    }
  ]
}
```

The preparation stage can run once the existing source-rights checks pass, but Hugging Face publication remains blocked until the extra per-source `dataset_license` clearance is present and consistent.

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

The dispatcher refuses an existing run directory and refuses source or manifest paths outside the configured source root. It checks the PDF utilities and LineForge binary, validates checksums and redistribution fields, assigns whole PDFs once across 19 deterministic page-balanced shards, creates one shared quota ledger, starts a live monitor, and launches all 19 workers. The Action's target is fixed at **100,024**; the local CLI accepts a smaller target for tests and approved pilots.

Each PDF's candidate pages are shuffled deterministically from the run seed and PDF checksum. Sampling prefers the middle 25%–75% interval; if that interval contains no page at or beyond page 10, sampling falls back to the available pages 10 onward. Page numbers are one-based. A worker renders one page at a time with `pdftoppm`, runs LineForge, and retains individual line PNGs. A conservative non-OCR screen rejects review-flagged, sparse, overly dense, non-line-shaped, and some figure-like crops. This remains a **heuristic, not a validated figure detector**; human review on a representative sample is required before production.

The ledger deduplicates image bytes and source-page-line provenance, serializes each acceptance, and refuses to commit above the target. Workers check the shared count between pages and candidates and record page number, source ID and checksum, line index, shard, engine version, source license, and rights-evidence reference. The reducer requires 19 distinct successful worker results, consistent run/input identities, verified image bytes and provenance, page >=10, and exactly the target number of unique images. Its manifest remains `publishable=false`; the separate publisher uses independent preflight checks rather than trusting a publishable flag.

## Manual GitHub Action and exact setup names

The workflow is `.github/workflows/dataset-dispatch.yml`. It can only be started manually on `main`; it has read-only repository permissions and runs the same local dispatcher on one dedicated self-hosted runner:

```text
self-hosted, linux, x64, lineforge-dataset
```

Before enabling a future run, provision a dedicated and trusted runner with CMake, a C++17 compiler, libjpeg/libpng development files, Python 3 and Pillow, `pdfinfo`, and `pdftoppm`. Do not expose the runner to untrusted pull-request or arbitrary-code workflows. Configure these **repository Actions variables**:

- `LINEFORGE_SOURCE_ROOT`: private, read-only staged source directory containing `manifest.json` and eligible PDFs;
- `LINEFORGE_OUTPUT_ROOT`: separate persistent storage directory, outside both the source tree and checkout;
- `HF_DATASET_REPO`: exactly `Nexuss0781/Geez-OCR-Dataset`; the publisher rejects any other target and refuses to create the repo or change its visibility;
- `HF_DATASET_LICENSE`: one explicit license identifier; it must exactly match `redistribution.dataset_license` for every eligible source. Leave unset until rights and license review is complete.

For the optional upload path only, configure this **repository Actions secret**:

- `HF_TOKEN`: a fresh Hugging Face write token authorized only for the target dataset repository. The token previously exposed in chat must be revoked and never reused.

The workflow's manual `publish_to_hf` input defaults to **false**. When false, the dispatch job does not install the Hugging Face library, read `HF_TOKEN`, or contact Hugging Face. When explicitly true in a future approved manual run, publication starts only after the 100,024-image dispatch succeeds. `HF_TOKEN` is injected into the final publishing step only; it is not present in ordinary CI, the build/test step, the dispatch step, the dependency-install step, source control, command-line arguments, or logs. A missing token, repository variable, or license selection fails the publisher closed.

The publisher is pinned to `Nexuss0781/Geez-OCR-Dataset`, requires the existing destination to be public, and will not create it or change privacy settings. Before any upload it verifies the source-manifest checksum, all eligible per-source permissions and exact dataset-license clearances, the dispatch summary, all 19 worker results, the SQLite quota ledger and exact target, every image's provenance and SHA-256, and the rendered dataset card's license. Its public payload contains only the verified PNG line crops, a `README.md` rendered from [`huggingface-dataset-card.template.md`](huggingface-dataset-card.template.md), and a minimal image-path/checksum index. It omits source PDFs, private source IDs, source PDF checksums, rights-evidence references, worker outputs, run seeds, and quota data. The full run remains private on the trusted runner; there is no Actions artifact upload.

The source staging and output directories must remain persistent and protected. The workflow serializes runs, but it does not create a cross-run image-deduplication index; repeated publication runs may therefore send duplicate content unless separately reviewed.

## Offline publisher validation

The publisher is available as `scripts/lineforge_hf_publish.py`. A local validation run is network-free and requires neither a token nor confirmation:

```bash
python3 scripts/lineforge_hf_publish.py \
  --run-dir /secure/lineforge-output/run-<unique-id> \
  --source-manifest /secure/staging/manifest.json \
  --source-root /secure/staging \
  --repo-id Nexuss0781/Geez-OCR-Dataset \
  --dataset-license <explicit-license-id> \
  --card-template docs/huggingface-dataset-card.template.md \
  --target 100024 \
  --dry-run
```

Dry-run performs the complete local integrity, quota, provenance, rights, and card validation only. The offline tests use synthetic files and a stub Hugging Face client; they make no network calls and cannot upload data. A real upload additionally requires the distinct `--confirm-publish` CLI choice and `HF_TOKEN`, and must only be used as part of a separately approved manual publication run.

## Current blockers

- No source PDF set is staged on the dedicated runner, and no approved `LINEFORGE_SOURCE_ROOT` / `LINEFORGE_OUTPUT_ROOT` runner configuration is present here.
- No checksum-verified source manifest is staged. An immutable, checksum-verified manifest and the staged PDF bytes are required.
- Source-specific redistribution evidence is incomplete. The new exact `redistribution.dataset_license` clearances, explicit dataset license selection, and final dataset-card license are not configured. Figures are filtered heuristically, not validated on a human-reviewed real-corpus sample.
- `HF_DATASET_REPO`, `HF_DATASET_LICENSE`, and a fresh, scoped `HF_TOKEN` secret are not configured. The previously exposed token must be revoked and rotated; it is not reused.

Therefore, the publisher code and tests are prepared, but **neither dispatch nor dataset publication is ready to run** until source files, per-source redistribution clearances, a compatible explicit license/card, the dedicated runner, private output storage, and a separate manual approval are in place. This implementation did not dispatch a workflow or upload dataset files.

## Validation

The Python suite covers exact 19-shard assignment, duplicate input rejection, source-root confinement, rights and checksum checks, deterministic page sampling, page >=10, crop-filter fixtures, concurrent exact-quota enforcement, unique-image/provenance reduction, malformed or missing workers, and an end-to-end synthetic PDF dispatch that starts 19 worker processes plus one live coordinator. Publisher tests check missing/mismatched rights clearances, exact quota and SQLite integrity, worker provenance, image tampering, license-card rendering, public-payload privacy scrubbing, confirmation gating, and stub-only uploads. Synthetic tests do not establish real-book figure precision or legal redistribution rights.

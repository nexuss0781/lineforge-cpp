# LineForge dataset pipeline

## Scope and safety boundary

This is a **LineForge-owned data-preparation workflow**, not an NTF training implementation. It extracts individual line-crop PNGs with provenance; it does not perform OCR or transcription, create character boxes, train Tesseract, or merge model weights. NTF remains an external, task-agnostic framework. The LineForge Telegram intake invokes the separate local Telegram File Bridge, but owns scan/download orchestration, status overlay, hash/page verification, rights review, and private manifest preparation.

The pipeline stages Telegram PDFs locally and uses a local SQLite quota ledger. The LineForge dispatcher starts **exactly 19 worker processes plus one live coordinator on the same trusted host**. Workers share one filesystem and one atomic SQLite ledger, so the exact-count cap is enforced without pretending isolated hosted matrix jobs share storage. The manual GitHub Action invokes this dispatcher on a dedicated self-hosted runner. It never accesses Telegram or transfers a Telegram session; stage the reviewed manifest and PDF bytes locally on storage visible to that runner before dispatch.

## Isolated one-job / one-image pilot

Use `scripts/lineforge_pilot.py` only to smoke-test the local path without invoking the production dispatcher. It requires exactly one manifest entry with explicit local redistribution-rights provenance, renders only one-based page 10, invokes the supplied `linecrop` executable once, and accepts at most one image into a fresh output directory. It writes `pilot-summary.json`, `pilot-dataset-manifest.json`, one content-addressed PNG, and a local quota ledger. The summary and manifest always say `publishable: false`; this result is not accepted by the full-run publisher.

Example (keep the manifest, staged PDF, and output outside the repository):

```bash
python3 scripts/lineforge_pilot.py \\
  --input-manifest /secure/pilot-source/manifest.json \\
  --source-root /secure/pilot-source \\
  --run-dir /secure/pilot-output/one-image-run \\
  --run-id pilot-001 \\
  --linecrop /path/to/linecrop \\
  --dpi 300
```

When real-source redistribution rights are unavailable, construct a local synthetic PDF fixture and use only that fixture. The pilot does not start GitHub Actions, the 19-worker production flow, or any Hugging Face preflight or upload.

No dispatch or upload was started during this implementation. The Action is manual-only, restricted to `main`, and has no automatic push, pull-request, schedule, or Hugging Face trigger. Its optional `publish_to_hf` input defaults to `false`.

## Local Telegram intake: fetch, verify, review, prepare

All source intake code lives in `scripts/lineforge_telegram_stage.py`. It reuses the user's existing local Telegram File Bridge checkout and authorized MTProto session. It does not copy `.env` or `.session` files into LineForge or GitHub. **Do not run `fetch` as part of an offline test:** it performs a fresh Telegram history scan and downloads the reviewed `Status=true` PDFs.

Configure these **local environment variables** (or pass the same values as CLI options):

- `LINEFORGE_TELEGRAM_BRIDGE_ROOT`: bridge checkout; default `/workspace/telegram-file-bridge`;
- `LINEFORGE_TELEGRAM_ENV_FILE`: bridge configuration; default `<LINEFORGE_TELEGRAM_BRIDGE_ROOT>/.env`;
- `LINEFORGE_TELEGRAM_SELECTION_REPORT`: analyst-reviewed Telegram PDF JSON report with a literal Boolean `status` per message; required for fetch;
- `LINEFORGE_TELEGRAM_STAGE_ROOT`: new private staging directory outside the LineForge checkout; required for fetch;
- `LINEFORGE_TELEGRAM_CHAT`: source channel; default `@et_bookstore`.

The bridge's local `.env` must already have `TELEGRAM_API_ID` and `TELEGRAM_API_HASH`. Its persistent authorized session remains at the bridge's configured `SESSION_FILE` (currently `.telegram-bridge-session.session` beside the bridge) or in its local `TELEGRAM_SESSION` value. The GitHub Action does not use Telegram credentials: it consumes a locally staged, reviewed manifest and PDFs. These credentials are needed only by the local bridge and must never be copied into source control or logs; Actions secrets with these names are unused by this workflow. The current bridge checkout is `/workspace/telegram-file-bridge`.

From the LineForge checkout, the stages are:

```bash
export LINEFORGE_TELEGRAM_BRIDGE_ROOT=/workspace/telegram-file-bridge
export LINEFORGE_TELEGRAM_ENV_FILE=/workspace/telegram-file-bridge/.env
export LINEFORGE_TELEGRAM_SELECTION_REPORT=/workspace/telegram-file-bridge/reports/et_bookstore-pdf-analysis-20261011T183555Z.json
export LINEFORGE_TELEGRAM_STAGE_ROOT="$HOME/private/et-bookstore-lineforge-$(date -u +%Y%m%dT%H%M%SZ)"
export LINEFORGE_TELEGRAM_CHAT=@et_bookstore
python3 scripts/lineforge_telegram_stage.py fetch
python3 scripts/lineforge_telegram_stage.py rights-template \
  --inventory "$LINEFORGE_TELEGRAM_STAGE_ROOT/telegram-inventory.json" \
  --output "$LINEFORGE_TELEGRAM_STAGE_ROOT/rights-review.json"
# Review rights-review.json privately; do not infer rights from Status=true.
python3 scripts/lineforge_telegram_stage.py prepare \
  --inventory "$LINEFORGE_TELEGRAM_STAGE_ROOT/telegram-inventory.json" \
  --rights-review "$LINEFORGE_TELEGRAM_STAGE_ROOT/rights-review.json" \
  --output "$LINEFORGE_TELEGRAM_STAGE_ROOT/manifest.json"
```

`plan` is the offline planning step: it reads the existing reviewed PDF report, excludes records whose `status` is false and all known byte-identical copies, and writes 19 private metadata-only job files. Job counts differ by at most one (407 selected PDFs become eight jobs of 22 and eleven jobs of 21). It records a run seed; use that same seed for `prepare` or `dispatch` so the later, verified runtime shard membership matches the plan when all planned sources are eligible. The plan does not download PDFs and is explicitly marked `ready_for_processing: false`.

```bash
python3 scripts/lineforge_telegram_stage.py plan \
  --selection-report /workspace/telegram-file-bridge/reports/et_bookstore-pdf-analysis-20261011T183555Z.json \
  --output-dir "$HOME/private/et-bookstore-jobs"
```

The command generates and records a run seed in `assignment-summary.json`; reuse that value with `prepare` or `dispatch`.

`fetch` first makes a fresh, no-download inventory scan, requires its message IDs and file names to match the reviewed report, then downloads only records whose reviewed `status` is literal `true`. It recomputes the staged PDF SHA-256 and page count, checks the PDF signature and sizes, and deduplicates the **actual downloaded bytes**; it does not assume a fixed item count or trust a prior `duplicate` flag as a checksum. Stale inventories, contradictory duplicate selections, changed files, failed downloads, and path escapes stop the stage. Inventory and staged files are written with private local permissions.

The generated rights template includes one row per actual unique, Status=true PDF with at least 10 pages. For **every row**, set `allowed: true` only after source-specific evidence explicitly clears redistribution of derived line images; provide `source_license`, a private `evidence_ref`, and a manually reviewed `public_source_name`. Add `dataset_license` only when that source is explicitly cleared for the exact Hugging Face dataset license intended. `prepare` binds the review to the exact inventory checksum, rechecks each PDF hash/page count, requires complete rights coverage and distinct safe book labels, then writes a private `manifest.json`. No rights are presumed for `@et_bookstore`.

The authorized Telegram session was not used in this implementation: **no live channel fetch, real PDF download, 19-worker dispatch, or Hugging Face upload was performed**.

## Source manifest and rights gate

Keep `manifest.json` and all PDFs in a private staging directory outside the repository checkout. Every path must resolve inside the configured source root. Every input entry must provide:

- `pdf_id`: stable private source identifier;
- `pdf_sha256`: lowercase SHA-256 for the exact staged PDF bytes;
- `path`: local path, preferably relative to the manifest;
- `page_count`: verified page count;
- `status`: literal JSON `true`;
- `public_source_name` and `public_source_slug`: reviewed public book label and its unique safe directory slug;
- `redistribution.allowed`: literal JSON `true`;
- `redistribution.license` and `redistribution.evidence_ref`: documented, source-specific rights evidence.

The pipeline re-hashes eligible PDFs and checks the actual PDF page count before processing. Repeated PDF hashes are rejected; PDFs shorter than 10 pages are omitted. `Status=true` is only a source-selection status and is **not** proof of permission to redistribute derived images. Rights evidence must explicitly allow that redistribution.

Publication requires one additional, reviewed field on **every eligible source entry**:

- `redistribution.dataset_license`: the exact Hugging Face/SPDX-style license identifier selected for the released line-crop dataset. The workflow derives the selected dataset license only when every eligible source has the same exact identifier; missing or mixed values stop publication. It does not infer compatibility, translate licenses, or accept a merely similar identifier.

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
      "public_source_name": "Reviewed public book title",
      "public_source_slug": "reviewed-public-book-title",
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

The dispatcher refuses an existing run directory and refuses source or manifest paths outside the configured source root. It checks the PDF utilities and LineForge binary, validates checksums and redistribution fields, assigns whole PDFs once across 19 seeded equal-count shards, creates one shared quota ledger, starts a live monitor, and launches all 19 workers. Shard membership is derived from the stable PDF ID and run seed, so the offline job plan and processing assignment are consistent when all planned sources remain eligible. The Action's target is fixed at **100,024**; the local CLI accepts a smaller target for tests and approved pilots.

Each PDF's eligible page numbers from **10 through the final page** are shuffled deterministically from the run seed and PDF checksum, covering the opening (after page 10), middle, and ending of the book. Workers interleave those page queues in randomized rounds. Each active PDF contributes one candidate page per round before any PDF contributes its next page, reducing the chance that an early quota is filled from only a few books. Page numbers are one-based. A worker renders one page at a time with `pdftoppm`, runs LineForge, and retains individual line PNGs. A conservative non-OCR screen rejects review-flagged, sparse, overly dense, non-line-shaped, and some figure-like crops. This remains a **heuristic, not a validated figure detector**; human review on a representative sample is required before production.

The ledger deduplicates image bytes and source-page-line provenance, serializes each acceptance, and refuses to commit above the target. When the monitor sees exactly **100,024** accepted images, it writes one shared stop marker; all 19 workers check it at page and crop boundaries, stop starting new work, and write their final result records. The ledger remains the hard upper bound, so concurrent workers cannot overshoot while the stop request propagates. Workers record page number, private source ID/checksum, public book label/slug, line index, shard, engine version, source license, and rights-evidence reference. Images are stored as `images/<book-slug>/page-00010/line-000001-<sha256-prefix>.png`; the public index includes book/page/line while omitting private Telegram IDs, PDF checksums, and rights evidence. The reducer requires 19 distinct successful worker results, consistent run/input identities, verified image bytes and provenance, page >=10, and exactly the target number of unique images. Its manifest remains `publishable=false`; the separate publisher uses independent preflight checks rather than trusting a publishable flag.

## Manual GitHub Action and exact setup names

The workflow is `.github/workflows/dataset-dispatch.yml`. It can only be started manually on `main`; it has read-only repository permissions and runs the same local dispatcher on one dedicated self-hosted runner:

```text
self-hosted, linux, x64, lineforge-dataset
```

Before enabling a future run, provision a dedicated and trusted runner with CMake, a C++17 compiler, libjpeg/libpng development files, Python 3 and Pillow, `pdfinfo`, and `pdftoppm`. Do not expose the runner to untrusted pull-request or arbitrary-code workflows. Configure this **repository Actions variable**:

- `LINEFORGE_SOURCE_ROOT`: the absolute path to the private, read-only staged source directory containing `manifest.json` and eligible PDFs; set it to the same path as `LINEFORGE_TELEGRAM_STAGE_ROOT` on the self-hosted runner. The workflow automatically creates a separate persistent `lineforge-output` directory beside the source directory, outside both the source tree and checkout; protect that parent storage and allow sufficient space.

For the optional upload path only, configure this **repository Actions secret**:

- `HF_TOKEN`: a fresh Hugging Face write token authorized only for the target dataset repository. The older token exposed in chat must be revoked and never reused.

No environment-scoped secret is used. `run_seed` remains a required manual dispatch input so it can match the seed in the private Telegram `assignment-summary.json` and preserve the planned 19-shard assignment; the dispatch step masks it before use. `publish_to_hf` is optional and defaults to **false**. When false, the dispatch job does not install the Hugging Face library, read `HF_TOKEN`, or contact Hugging Face. When explicitly true, `checkpoint_images` controls cadence (default **5,000 new images**): the coordinator uploads verified batches and a sanitized index directly to public `main`. The README and index remain marked `in_progress` until the complete 100,024-image run passes final validation, when the final publisher updates them to `complete`. The destination is fixed to `Nexuss0781/Geez-OCR-Dataset`; the license is derived from the reviewed manifest and must be present and identical for every eligible source. `HF_TOKEN` is injected only into the coordinator's dispatch process and the final publisher step; the dispatcher removes it from the environment before starting the monitor and workers. It is not present in ordinary CI, the build/test step, the dependency-install step, source control, command-line arguments, or logs. Missing rights clearance, inconsistent license values, or a missing token fails the publisher closed.

The publisher is pinned to `Nexuss0781/Geez-OCR-Dataset`, requires the existing destination to be public, and will not create it or change privacy settings. Every progress checkpoint goes directly to public `main`. Before the first checkpoint, the pipeline verifies the staged source manifest and requires explicit redistribution and exact dataset-license clearance for every eligible source. Every checkpoint verifies image paths and checksums and uploads only PNGs plus a sanitized card/index containing book title, page, line, and image checksum. The final main-revision upload independently verifies the source-manifest checksum, all eligible per-source permissions and exact dataset-license clearances, the dispatch summary, all 19 worker results, the SQLite quota ledger and exact target, every image's provenance and SHA-256, and the rendered dataset card's license. Its public payload contains only the verified PNG line crops, a `README.md` rendered from [`huggingface-dataset-card.template.md`](huggingface-dataset-card.template.md), and an image-path/checksum/book/page/line index. It omits source PDFs, private source IDs, source PDF checksums, rights-evidence references, worker outputs, run seeds, and quota data. The full run remains private on the trusted runner; there is no Actions artifact upload.

The source staging and output directories must remain persistent and protected. The workflow serializes runs, but it does not create a cross-run image-deduplication index; repeated publication runs may therefore send duplicate content unless separately reviewed. When progress publishing is enabled, checkpoint images are immediately visible on public `main`, while the README and index remain `in_progress`; final validation changes them to `complete` only after all 100,024 images pass preflight. The local coordinator prints the live global count while processing.

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

Dry-run performs the complete local integrity, quota, provenance, rights, and card validation only. The offline tests use synthetic files and a stub Hugging Face client; they make no network calls and cannot upload data. A real GitHub Action upload is opt-in through the manual `publish_to_hf=true` input and requires `HF_TOKEN`; the final full-revision CLI upload also requires its distinct `--confirm-publish` choice. Neither live path has been run here.

## Current blockers

- The dedicated runner, `LINEFORGE_SOURCE_ROOT` Actions variable, and full PDF staging were not verifiable from the available repository access. The workflow requires these to resolve to persistent, trusted storage.
- The inspected workspace contains the channel inventory report, but no prepared, checksum-verified, rights-reviewed source manifest was available. An immutable manifest and the exact staged PDF bytes are required.
- Source-specific redistribution evidence is incomplete. The new exact `redistribution.dataset_license` clearances, explicit dataset license selection, and final dataset-card license are not configured. Figures are filtered heuristically, not validated on a human-reviewed real-corpus sample.
- The optional publication path still requires every source to have the same reviewed `redistribution.dataset_license` value and a fresh, scoped `HF_TOKEN` repository secret; never reuse the previously exposed token. No separate dataset-repository or dataset-license Actions variable is needed, and no environment-scoped secret is referenced by this workflow.

Therefore, the publisher code and tests are prepared, but **neither dispatch nor dataset publication is ready to run** until source files, per-source redistribution clearances, a compatible explicit license/card, the dedicated runner, private output storage, and a separate manual approval are in place. This implementation did not dispatch a workflow or upload dataset files.

## Validation

The Python suite covers exact 19-shard assignment, duplicate input rejection, source-root confinement, rights and checksum checks, deterministic page sampling, page >=10, crop-filter fixtures, concurrent exact-quota enforcement, unique-image/provenance reduction, malformed or missing workers, an end-to-end synthetic PDF dispatch that starts 19 worker processes plus one live coordinator, and offline Telegram selection/rights-template/manifest tests. CTest registers these suites for GitHub CI. Publisher tests check missing/mismatched rights clearances, exact quota and SQLite integrity, worker provenance, image tampering, license-card rendering, book/page/line public indexing, privacy scrubbing, confirmation gating, and stub-only uploads. Synthetic tests do not establish real-book figure precision or legal redistribution rights.

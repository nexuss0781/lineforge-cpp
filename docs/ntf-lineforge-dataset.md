# NTF adapter: LineForge OCR-image dataset preparation

## Scope and safety boundary

This is **dataset preparation only**. It does not train Tesseract, perform OCR/transcription, create character boxes, or merge model weights. The existing NTF v2 workflow is an iterative FedAvg trainer and is not reused for this non-training task. The adapter follows NTF's 19-worker plus coordinator shape, with deterministic input identity, disjoint PDF ownership, worker provenance, and strict reduction.

The checked-in adapter currently supports **local staged PDF files** and a local shared SQLite quota ledger. It does not contact Telegram, Hugging Face, AWS, or another cloud service. GitHub Actions jobs do not share filesystems, so this local quota ledger is deliberately **not presented as a hosted Actions control plane**. No hosted workflow has been added or run, and no source PDFs were fetched or uploaded.

## Input manifest contract

Use a private UTF-8 JSON manifest, never commit it to LineForge. The input is rejected unless every source entry has all of the following:

- `pdf_id`: stable private identifier;
- `pdf_sha256`: lowercase SHA-256 of the exact PDF bytes;
- `path`: local staged PDF path;
- `page_count`: verified count;
- `status`: literal JSON `true`;
- `redistribution.allowed`: literal JSON `true`;
- `redistribution.license` and `redistribution.evidence_ref`: source-specific rights evidence.

The adapter verifies each eligible local file's SHA-256 and byte size when supplied. Repeated PDF content hashes are rejected rather than silently assigned twice. PDFs with fewer than 10 pages are omitted; all processed pages use one-based numbering and begin at page 10 or later. `Status=true` means eligible for consideration, **not** permission to publish derived images.

Example shape (replace the placeholders only in a private manifest):

```json
{
  "schema_version": 1,
  "dataset_id": "approved-source-set",
  "revision": "immutable-source-report-revision",
  "files": [
    {
      "pdf_id": "private-source-id",
      "pdf_sha256": "<64 lowercase hex characters>",
      "path": "/secure/staging/book.pdf",
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

## Assignment and processing

1. `prepare` verifies the source manifest and writes 19 shard manifests. Whole PDFs are assigned once with a deterministic, page-count-balanced largest-processing-first rule. The common input-manifest digest and run ID are copied to every shard.
2. Each PDF's candidate pages are shuffled with a stable seed derived from the run seed and PDF checksum. Sampling prefers the middle 25%–75% page interval; when that interval contains no page at or beyond page 10, it falls back to the available pages 10 onward. No page is repeated within a run.
3. A worker verifies the PDF checksum and page count, renders one page at a time with `pdftoppm`, then invokes LineForge 1.0.1. LineForge remains line-level only and emits PNG crops plus its page manifest.
4. The acceptance filter rejects engine-review candidates, very low/high ink crops, non-line-shaped crops, sparse rules, and figure-like row patterns. This is a conservative **non-OCR heuristic**, not a proven figure detector. The current LineForge evaluation found false horizontal-strip crops through some figures; therefore real-corpus figure precision still requires a reviewed sample before production.
5. Accepted PNG bytes are keyed by SHA-256 and recorded with PDF checksum, page number, line index, shard, and engine version. SQLite enforces both unique image bytes and unique PDF/page/line provenance. With all local worker processes pointing to the same ledger, each accept is serialized and the count cannot exceed the target. Workers check the count before every page and line and stop once it reaches 100,024.
6. The reducer requires all 19 unique worker result files, common run/input identity, successful worker status, matching ledger counts, verified image checksums, page >=10, and exactly 100,024 unique output images. Its manifest sets `publishable=false`; it does not publish to Hugging Face.

Local commands:

```bash
python3 scripts/ntf_lineforge_dataset.py prepare \
  --input-manifest /secure/manifest.json \
  --output-dir /secure/run/shards \
  --run-seed approved-run-seed \
  --run-id approved-run-id

# Launch one process per shard (0..18) on a shared local filesystem, passing:
# --shard-manifest /secure/run/shards/shard-NN.json
# --linecrop /path/to/linecrop --work-dir /secure/run/work
# --output-dir /secure/run/output --ledger /secure/run/quota.sqlite

# Run a colocated coordinator concurrently; it observes the same SQLite ledger.
python3 scripts/ntf_lineforge_dataset.py monitor \
  --shards-dir /secure/run/shards \
  --results-dir /secure/run/output/workers \
  --ledger /secure/run/quota.sqlite \
  --output-dir /secure/run/output \
  --target 100024

python3 scripts/ntf_lineforge_dataset.py reduce \
  --shards-dir /secure/run/shards \
  --results-dir /secure/run/output/workers \
  --ledger /secure/run/quota.sqlite \
  --output-dir /secure/run/output \
  --target 100024 \
  --output /secure/run/output/dataset-manifest.json
```

The example paths and run identifiers above are placeholders. Never put private PDFs, report files, Telegram identifiers, signed URLs, session data, or tokens in the repository or Actions logs.

## Hosted Actions design and blockers

A future hosted workflow must be **manual-only** (`workflow_dispatch`), with exactly 19 matrix workers (`max-parallel: 19`) plus one coordinator/monitor. Its job names must match the monitor and be tested. Each worker should receive only its own deterministic PDF shard, use a unique artifact prefix, checkpoint frequently, and poll the shared stop state. The coordinator validates exactly 19 distinct successful shard results against one manifest digest and reduces only after those checks pass. No push, pull-request, schedule, or automatic dataset-publication trigger is allowed.

The local SQLite counter cannot provide the requested live cross-job count because standard GitHub-hosted jobs are isolated. A production run therefore needs **both**:

1. An approved, least-privilege source adapter or secure staging location that workers can use to obtain the PDFs. The personal Telegram MTProto session is not copied to Actions. The current task has no approved Telegram bot/source credential or staged source-manifest location.
2. A shared atomic quota/stop store accessible to all 19 workers and the coordinator. Without it, the safe alternative is to oversample privately and have the reducer publish exactly the first 100,024 unique crops after every worker finishes; that alternative does **not** meet the requested live-stop behavior. Cancellation can also lag while a worker finishes its current page/batch, so workers must check the shared state before each page and crop and never commit beyond the atomic cap.

Actions concurrency is a ceiling, not a reservation. GitHub's current [runner reference](https://docs.github.com/en/actions/reference/runners/github-hosted-runners) lists standard public `ubuntu-latest` at 4 vCPU / 16 GB RAM / 14 GB SSD; standard private Linux runners are smaller. Its current [Actions limits](https://docs.github.com/en/actions/reference/limits) list 20 concurrent standard jobs on Free, six hours maximum per GitHub-hosted job, and 500 MB of artifact storage on Free. Thus 19 workers plus a concurrent coordinator use the full 20-job Free ceiling before any other activity, and `max-parallel: 19` cannot promise that 19 workers start together; shared account usage can queue them. LineForge's crop writer is already bounded to four threads; much of page segmentation is not parallel within a worker, so do not promise that every stage saturates all four cores. Measure throughput and image volume on an approved pilot. A 100,000-PNG staging set may exceed the GitHub artifact limit; use an approved object-store/staging design rather than assuming Actions artifacts can hold the dataset.

## Rights, count, and publication gates

- The newest local `@et_bookstore` report is not a usable run manifest: it lists 421 entries as `Status=true` but has no per-PDF SHA-256 values. An earlier filtered report differs. The previously stated 407 candidates are therefore unverified until a new checksum-verified report/manifest is made.
- The Hugging Face dataset repository is currently empty and has no declared license. Before any public upload, every source needs documented rights that permit redistributing its derived line images, and the output dataset needs an explicit license/card consistent with those rights. The local SHA-256 ledger deduplicates one run; a later run must seed a persistent image-hash index from the already-published dataset before it can guarantee no cross-run duplicates.
- No Telegram source manifest, public dataset content, or cloud quota service is currently configured for the hosted workflow. The exposed HF token mentioned in the task must not be reused; a newly rotated token should be stored only in the repository's secret settings if/when publication is approved. This adapter never reads or logs it.
- Dispatch approval and public publication are separate gates. First approve the exact source set and a private pilot. After a staged run, inspect the exact count, manifest digest, rights, sample crops, and file payload; obtain separate approval before uploading that payload to Hugging Face.

## Validation

The unit suite covers exact 19-shard coverage and disjointness, status/rights/hash checks, deterministic middle-page selection, page >=10, figure-strip rejection, exact concurrent quota enforcement, image/provenance deduplication, missing/duplicate/malformed/mixed-identity workers, reducer validation, and provenance checks. Synthetic tests do not establish real-book figure precision or source redistribution rights.

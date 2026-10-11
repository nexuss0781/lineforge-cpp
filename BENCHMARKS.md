# Benchmark and validation record

**Run date:** 2026-10-11

**Engine:** `linecrop 0.3.0`

**Build:** Release, GCC 13.3.0, CMake, Ubuntu 24.04
**Synthetic command:** `python3 tests/benchmark.py --binary build/linecrop --work-dir build/benchmark-pages --output-json build/benchmark-final.json`

## Results

| Evaluation | Result | Scope and qualification |
| --- | --- | --- |
| Synthetic geometry suite | **40/40 passed** | Deterministic fixtures, not real-world accuracy or OCR quality. |
| CTest | **2/2 passed** | One 40-case geometry benchmark and four evaluator unit tests. |
| Four hand-labeled real-page ROIs | **23/23 matched** at IoU ≥ 0.50; precision **1.00**, recall **1.00**, 0 missed, 0 cut indicators, 0 merged, 0 split | Small, selected text blocks across two scanned documents. This is not whole-page or corpus-wide accuracy. Ground-truth envelopes were estimated visually from enlarged source-only regions. |
| Full second-document batch | **83 pages**, 3,815 crop candidates, 201 excluded candidates, 484 kept crops flagged for review | Candidate counts are not line-accuracy counts. The illustrated final cover remains noisy and needs human review. |

A separate AddressSanitizer/UndefinedBehaviorSanitizer build also passed both registered tests with leak detection enabled.

The four hand-labeled regions include upper paragraphs from two neighboring pages, a paragraph plus a centered heading on another page, and one paragraph in a two-page spread. The centered heading missed by the earlier threshold is now recovered; its underline is separately excluded and flagged for review. The perfect score applies only to those 23 selected boxes and must not be generalized to other regions or layouts.

For the 83-page batch, the median processing time was **162 ms/page**, with **13.79 s** summed over page processing on this computer (PDF rasterization excluded). The illustrated final page generated 89 kept candidates, 51 of them marked `review_required`; this is an observed limitation, not a confirmed line count.

## Synthetic coverage and timing

The 40 checks cover 24 pages (clean, low contrast, gradient illumination, sparse noise, blur, JPEG, mild skew, and Ethiopic-font or pseudo-Ethiopic text; three seeds per condition), two blank-page rejection cases, a 15.55-megapixel throughput case, a sparse row-noise case, a heading/underline/detached-mark/single-speck/footer case, four CLI/error-path cases, and seven layout cases. Layout fixtures verify spreads, two-column LTR/RTL ordering, a mixed newspaper headline, custom-region ordering, a single-region override, and invalid-region rejection.

- **Per-case runtime:** 105.66 ms median, 143.28 ms p95, 4.97 s summed process time.
- **15.55 MP case:** 1,238.40 ms end-to-end on this run.

The suite checks crop geometry and files, retained line counts/order, footer handling, detached-mark crop retention, noise filtering, mild-skew estimates, layout metadata, region order, version output, and invalid-input handling. These synthetic geometry tests do not measure OCR character/word accuracy.

## Implementation changes exercised

- Flatten slowly varying/color illumination before local thresholding.
- Combine local row profiles, low-density valleys, connected components, and line-spacing evidence to refine line bands.
- Join tiny same-line row fragments without merging ordinary neighboring lines; preserve complete headings while separating thin underline artifacts.
- Remove isolated single-pixel specks, retain small marks near glyphs, and keep additional grayscale padding around crops for detached Ethiopic marks.
- Mark uncertain oversized/merged/fragmented/edge-adjacent crops for inspection; record exclusions for human review.
- Evaluate line boxes against hand-labeled ROIs with one-to-one IoU matching and missed, cut, merged, and split counts.

## Limits and reproducibility

The real-page evaluation uses a small manually selected ROI sample; it does not claim 100% general accuracy. The 83-page batch reports candidate/review volume, not a true-line denominator. Illustrations, cover decoration, scan edges, weak gutters, and embedded artifacts can still create false positives. Inspect `review_required` crops and all `excluded` manifest entries before bulk processing. A broader held-out annotation set is needed for a defensible page-level accuracy estimate.

PDFs, rasterized pages, crop images, overlays, and hand-label JSON used in the local evaluation were kept under the ignored `build/` directory and are not part of the public source payload.

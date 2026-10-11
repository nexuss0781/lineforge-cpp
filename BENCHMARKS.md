# Benchmark and validation record

**Run date:** 2026-10-11
**Engine:** `linecrop 0.4.0`
**Build:** Release, GCC 13.3.0, CMake, Ubuntu 24.04

## Results

| Evaluation | Result | Scope and qualification |
| --- | --- | --- |
| Synthetic geometry suite | **42/42 passed** | Deterministic fixtures, including close-pitch vertical valleys and top-edge clipping. Not real-world accuracy or OCR quality. |
| CTest | **2/2 passed** | The 42-case geometry suite and **8 evaluator unit tests**. |
| AddressSanitizer/UndefinedBehaviorSanitizer | **2/2 passed** | Both CTest targets passed with leak detection and halt-on-error enabled. |
| Four independently labeled real-page ROIs | **32/34 predictions matched 36 labeled lines** at IoU ≥ 0.50; precision **0.941**, recall **0.889** | Two merged crops, four unmatched labeled lines, two false-positive crops, and zero cut-line indicators across four selected ROIs from two scanned documents. This is a small ROI sample, not whole-page or corpus-wide accuracy. |
| Held-out ROI review workload and error recall | **2/34 candidates flagged (5.9%)**; crop-error review recall **2/2 (1.00)**; error-page alert recall **2/2 (1.00)** | Both measurable crop errors were flagged, and both ROIs with segmentation errors had a review alert. The denominator is only two crop errors and two error-containing ROIs; this is not a calibrated guarantee for other pages or error types. |
| Full private-PDF corpus, baseline | **361 pages**, 12,452 kept crop candidates, 1,573 kept crops flagged, 508 excluded candidates | Two documents: 83 and 278 pages. Candidate and flag counts are workload measures, not ground-truth accuracy. |
| Full private-PDF corpus, 0.4.0 | **361 pages**, 12,816 kept crop candidates, 806 kept crops flagged, 514 excluded candidates | Same locally rasterized pages and 1,200-pixel rendering setting as baseline. The new detector emitted 364 more candidates (+2.9%). |

### Review-workload comparison

Excluded candidates are counted as review work, rather than being discarded from the comparison.

| Corpus | Baseline review queue | 0.4.0 review queue | Queue reduction | Candidate review rate, baseline → 0.4.0 |
| --- | ---: | ---: | ---: | ---: |
| 83-page document | 658 / 3,949 (**16.7%**) | 455 / 4,103 (**11.1%**) | **30.9% fewer items** | 16.7% → 11.1% |
| 278-page document | 1,423 / 9,011 (**15.8%**) | 865 / 9,227 (**9.4%**) | **39.2% fewer items** | 15.8% → 9.4% |
| **Combined** | **2,081 / 12,960 (16.1%)** | **1,320 / 13,330 (9.9%)** | **36.6% fewer items** | **16.1% → 9.9%** |

Across the combined corpus, kept-crop review flags declined **48.8%** (1,573 → 806). The queue includes excluded candidates (508 → 514), so the end-to-end reduction is 36.6%, not 48.8%. No flags were removed merely to improve these counts. Review rates remain nonzero; illustrated final-cover pages are still noisy and remain flagged for inspection.

On emitted crops, the primary review-reason counts also fell: possible merged/oversized crops **719 → 420**, short/fragmentary crops **379 → 208**, and page-edge warnings **767 → 230**. Reasons may overlap on a crop. The full-corpus run has no line-level ground truth, so these decreases do not establish that every removed warning was unnecessary or that every true error was detected.

## Independent held-out ROI evaluation

The four ROIs were selected on source-page rasters from two private documents. Line-row intervals were selected visually on source images and boxes were bounded from source pixels within those intervals; detector output was not used to create the labels. The evaluator uses one-to-one IoU matching at 0.50. Review workload counts `review_required` crops and excluded candidates inside each ROI. A wide crop covering multiple labeled lines is treated as a merge even when a short, indented line occupies little of that crop's area. Missed lines contribute to page-level segmentation-error recall, not crop-error recall.

| ROI | Labeled lines | Predictions | Matched | Review candidates | Crop-error review recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| Document A, page 20, right-side heading and paragraph | 8 | 7 | 6 | 1 | 1/1 |
| Document A, page 62, right-side paragraph | 10 | 10 | 10 | 0 | N/A (no measured crop error) |
| Document B, page 40, title and paragraph | 11 | 10 | 9 | 1 | 1/1 |
| Document B, page 140, title and paragraph | 7 | 7 | 7 | 0 | N/A (no measured crop error) |
| **Total** | **36** | **34** | **32** | **2** | **2/2** |

The two error-containing ROIs each had a review alert. The four unmatched labels and two false-positive predictions remain real errors in this sample; review flags expose uncertainty but do not fix segmentation. These four selected regions cannot support a general accuracy or recall claim. More independently annotated pages—especially covers, edge-truncated text, faint scans, and irregular columns—are needed for a stronger calibration set.

## Synthetic coverage and reproducibility

The 42 deterministic checks cover 24 generated pages across clean, low-contrast, gradient illumination, sparse noise, blur, JPEG, mild skew, and Ethiopic-font or pseudo-Ethiopic text (three seeds each); two blank-page rejection cases; a 15.55-megapixel throughput case; sparse row noise; close-pitch vertical rows; clipped top-edge text beside a dark scan-border stroke; heading/underline/detached-mark/speck/footer handling; four CLI/error-path cases; and seven layout cases. These tests check geometry, ordering, crop files, repair behavior, skew, reading order, and review metadata. They do not measure transcription or OCR accuracy.

Re-run with:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

The private source PDFs, raster pages, crop outputs, overlays, and annotation JSON are not in the repository. The full-corpus figures above are local workload comparisons and cannot be independently reproduced from public repository contents alone.

## Changes exercised

- Kept horizontal recursive whitespace/XY-cut layout intelligence, with columns and blocks identified before line rows.
- Refined vertical line bands using region-local row profiles, low-density valleys, connected components, and observed local line spacing.
- Constrained each line's horizontal bounds to ink within its own vertical membership; retained detached Ethiopic marks while suppressing long page-edge strokes.
- Made scale-based uncertainty checks local to each block and tightened fragment and edge flags using structural evidence.
- Extended evaluation to report review workload, crop-error review recall, and error-page alert recall, including missed lines.

## Limitations

Full-corpus candidate/flag counts are not an accuracy denominator. The held-out hand-labeled evaluation covers only 36 lines in four selected ROIs. Its observed 100% error recall is based on just two crop-error candidates and two affected ROIs, and must not be generalized. Decorative covers and irregular layouts remain high-noise; review flags and excluded candidates should still be inspected on such pages. PDFs and all derived images, crops, overlays, and labels remain private and were not committed.

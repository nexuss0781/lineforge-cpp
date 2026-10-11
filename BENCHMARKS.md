# Benchmark and validation record

**Run date:** 2026-10-11
**Engine:** `linecrop 0.4.1`
**Build:** Release, GCC 13.3.0, CMake, Ubuntu 24.04

## Results

| Evaluation | Result | Scope and qualification |
| --- | --- | --- |
| Synthetic geometry suite | **43/43 passed** | Adds a four-font-size vertical-boundary tolerance case and verifies one text-row group per crop. Not real-world accuracy or OCR quality. |
| CTest | **2/2 passed** | The 43-case geometry suite and **10 evaluator unit tests**. |
| AddressSanitizer/UndefinedBehaviorSanitizer | **2/2 passed** | Both CTest targets passed with leak detection and halt-on-error enabled. |
| Focused vertical-boundary audit, four unique real-page ROIs | **23/23 lines matched** at IoU ≥ 0.50 | On the same labels, mean absolute top error improved **4.478 → 1.304 px**, bottom **4.217 → 1.000 px**; boundaries within 3 px improved **4/23 → 21/23 top** and **3/23 → 23/23 bottom**. Two top boundaries remain over 3 px. |
| Exact brown page, high-resolution repeat | **9/9 top and 8/9 bottom boundaries within 3 px** | The same nine page-26 labels were scaled to the 1488×2103 PDF scan/page-25 raster; this is a scale-repeat check, not nine additional independent lines. Mean absolute error: top **1.556 px**, bottom **1.667 px**; one bottom edge is 4 px out. |
| Earlier 0.4.0 held-out real-page evaluation | **32/34 predictions matched 36 labeled lines** at IoU ≥ 0.50; precision **0.941**, recall **0.889** | Two merged crops, four unmatched labeled lines, two false-positive crops, and zero cut-line indicators across four selected ROIs from two scanned documents. This earlier sample is not whole-page or corpus-wide accuracy. |
| Earlier 0.4.0 review workload and error recall | **2/34 candidates flagged (5.9%)**; crop-error review recall **2/2 (1.00)**; error-page alert recall **2/2 (1.00)** | Historical result with only two crop errors and two error-containing ROIs; it is not a calibrated guarantee for other pages or error types. |
| Full private-PDF corpus, baseline | **361 pages**, 12,452 kept crop candidates, 1,573 kept crops flagged, 508 excluded candidates | Two documents: 83 and 278 pages. Candidate and flag counts are workload measures, not ground-truth accuracy. |
| Full private-PDF corpus, 0.4.0 | **361 pages**, 12,816 kept crop candidates, 806 kept crops flagged, 514 excluded candidates | Same locally rasterized pages and 1,200-pixel rendering setting as baseline. The new detector emitted 364 more candidates (+2.9%). |

## 0.4.1 vertical-boundary audit

The focused audit used the same 23 independently labeled line boxes across four selected ROIs on four real pages from two scanned documents. It compared the local 0.4.0 baseline with 0.4.1 using the evaluator's one-to-one IoU matches, then scored each matched top and bottom edge separately. The labels cover scan-A pages 25, 26, and 43, plus scan-B page 2; they are a small selected sample, not a claim about all pages or font sizes.

| Boundary measure | 0.4.0 baseline | 0.4.1 |
| --- | ---: | ---: |
| Mean absolute top error | 4.478 px | **1.304 px** |
| Median absolute top error | 4 px | **1 px** |
| Mean absolute bottom error | 4.217 px | **1.000 px** |
| Median absolute bottom error | 4 px | **1 px** |
| Top boundaries within 3 px | 4/23 | **21/23** |
| Bottom boundaries within 3 px | 3/23 | **23/23** |

All 23 labels matched one emitted crop in both versions. The two remaining top-boundary misses over 3 px are on page 43; no bottom boundary in this sample exceeds 3 px. Some revised edges trade slight extra blank margin for reduced clipping: six top edges and twelve bottom edges extend past the label, but these remain within the measured error distribution. On the exact brown source rendered at 1488×2103, the same nine page-26 labels were scaled from its 992×1402 version: 9/9 tops and 8/9 bottoms are within 3 px, with a largest bottom error of 4 px. This repeat is not counted as additional unique labels.

The refined geometry changed only vertical boundaries and crop windows in the five compared runs. Kept-line counts, horizontal coordinates, review flags/reasons, and excluded-candidate boxes were identical to 0.4.0. Four sampled output crops from the brown page and page 43 were visually inspected: the updated crops no longer show fragments of the next row at their vertical edges, and no visible ascender/descender/mark clipping was found in those examples. This is a limited visual spot check, not a full-page guarantee. Private PDFs, images, crop outputs, overlays, and labels remain outside the repository.

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

The 43 deterministic checks cover 24 generated pages across clean, low-contrast, gradient illumination, sparse noise, blur, JPEG, mild skew, and Ethiopic-font or pseudo-Ethiopic text (three seeds each); two blank-page rejection cases; a 15.55-megapixel throughput case; sparse row noise; close-pitch vertical rows; a four-font-size vertical-boundary/crop-isolation case; clipped top-edge text beside a dark scan-border stroke; heading/underline/detached-mark/speck/footer handling; four CLI/error-path cases; and seven layout cases. These tests check geometry, ordering, crop files, repair behavior, skew, reading order, and review metadata. They do not measure transcription or OCR accuracy.

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
- Split adjacent line boxes and crop windows at measured low-ink valleys, with font-scale-relative margins only at unpaired boundaries.
- Constrained each line's horizontal bounds to ink within its own vertical membership; retained detached Ethiopic marks while suppressing long page-edge strokes.
- Made scale-based uncertainty checks local to each block and tightened fragment and edge flags using structural evidence.
- Extended evaluation to report review workload, crop-error review recall, and error-page alert recall, including missed lines.

## Limitations

Full-corpus candidate/flag counts are not an accuracy denominator. The earlier held-out hand-labeled evaluation covers only 36 lines in four selected ROIs; its observed 100% error recall is based on just two crop-error candidates and two affected ROIs, and must not be generalized. The new vertical-boundary audit covers only 23 unique lines; two top edges remain more than 3 px from their labels, so performance across all fonts, pages, and irregular layouts is not established. Decorative covers and irregular layouts remain high-noise; review flags and excluded candidates should still be inspected on such pages. PDFs and all derived images, crops, overlays, and labels remain private and were not committed.

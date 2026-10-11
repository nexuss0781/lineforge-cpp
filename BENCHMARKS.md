# Benchmark and validation record

**Latest audit date:** 2026-10-11
**Current engine:** `linecrop 1.0.0`
**Build:** Release, GCC 13.3.0, CMake, Ubuntu 24.04
**Pinned code baseline for the V1 rerun:** `15fc0b878445f98fad01b38e1dfc861ac4bd464c` (`linecrop 0.5.0`)
**Historical 0.5.0 comparison baseline:** `3ac462cd4fe46373efa6637cecf979e17021cd12` (`linecrop 0.4.1`)

## 1.0.0 V1 line and crop-retention audit

| Validation | Result | Scope and qualification |
| --- | --- | --- |
| Release build and CTest | **2/2 passed** | Includes the full **49-case synthetic suite** and **13 evaluator unit tests**. |
| AddressSanitizer/UndefinedBehaviorSanitizer CTest | **2/2 passed** | Leak detection and halt-on-error enabled; full synthetic suite plus evaluator unit tests. |
| Faint/shallow synthetic retention | **3,553/3,553 source-ink pixels retained** in the recoverable case | A separate lower-contrast case remains review-flagged; synthetic evidence is not a real-scan accuracy claim. |
| Exact spread-39 rerun, 3300×2556 | **52 crops, 2 page regions, 0 centerline-crossing crops** | Physical-side crop counts are 30 left / 22 right in both LTR and RTL runs. No independently corrected line labels exist for this spread, so these are geometry counts, not precision/recall. |
| Spread-39 `line_0052` crop-edge defect | **Repaired and review-flagged** | Pinned 0.5.0 assigned a 14-pixel crop to source ink spanning 39 active rows. V1 expands the line/crop to 54 pixels, retains that full vertical source span, and records `shallow_candidate_expanded_from_local_ink`. |
| Exact brown page-26 raster comparison | **33 crops + 1 excluded candidate in both 0.5.0 and 1.0.0** | Historical 0.2.0 emitted 27 crops, including boxes spanning 244 and 128 pixels in dense text regions. V1 adds shallow-candidate review flags and expands one short interior row; this raster has no independent line labels, so counts are not accuracy or proven merges. |
| Existing independent ROI labels, same inputs and labels | **31/32 matches for both pinned 0.5.0 and V1**; precision **1.000**, recall **0.9688** | Five selected ROIs from private scans; one missed line and two evaluator merge indicators on page 43. The crop-level review recall for those two indicators is **0/2**; a page-level excluded-candidate alert is not proof that the exact crop errors were detected. No word/ink boxes are present in this label set, so word-edge retention is unscored. |
| Source-crop retention against labeled line boxes | **95.19% mean area coverage; 19/32 fully contained** | Label-box geometry, not exact glyph segmentation. Left/right edges retained for 31/32 boxes; top for 24/32 and bottom for 26/32. |
| Reading-order labels | **0/94 pairwise violations** | Across the available labeled ROI order annotations; this does not score left/right accuracy on spread 39. |
| Five-page labeled-run timing | **294.757 ms mean; 251.831 ms median; 544.234 ms p95** | Five observations only; nearest-rank p95 therefore equals the slowest observed page. Timing is environment-specific, not a throughput guarantee. |
| Additional real-page smoke runs | **Seven source rasters from two private PDFs completed**, plus the separate spread and brown-26 tests | Unlabeled pages are smoke/geometry checks only. No broad, independently labeled handwriting or workbook corpus was available. |

The five-page V1 ROI result matches a fresh build of the pinned public 0.5.0 commit on the same available rasters and labels. A separate older local metrics JSON reported 32/32 on a similarly sized set, but that result was not reproducible with the pinned public baseline and currently available source images, so it is not carried forward as a V1 accuracy claim. No universal or perfect-accuracy claim is made. Private PDFs, rasters, crops, overlays, and annotations remain outside Git.

## Historical 0.5.0 geometry-aware release audit

| Validation | Result | Scope and qualification |
| --- | --- | --- |
| Synthetic geometry suite | **48/48 passed** | Includes a spanning newspaper headline over a partial curved gutter, irregular newspaper columns, variable-height baselines, a workbook-style mixed-content page with individually labeled handwriting-like answer strokes, ruled answer rows, crop containment, fallback behavior, and legacy regression cases. Synthetic only. |
| Release CTest | **2/2 passed** | The 48-case synthetic suite and **11 real-page evaluator unit tests**. |
| AddressSanitizer/UndefinedBehaviorSanitizer CTest | **2/2 passed** | Optimized `-O1` instrumented build; leak detection and halt-on-error enabled. |
| Private held-out source audit | **32/32 labeled lines matched** at IoU ≥ 0.50; **150/150 word-like source-ink groups contained** | Four selected rasters from two private PDFs. Compared with the exact baseline: 27/32 line matches and 105/150 source-ink groups contained. This selected sample is not corpus-wide accuracy. |
| Crop structure | **3 baseline merged crops → 0**; no cut/split indicators in the 32-label upgrade sample | The merges were on the dense sepia printed page. The upgraded row-valley split recovered all six independently labeled rows in that ROI. |
| Region and reading-order checks | **0 region-assignment errors; 0 pairwise order violations** | 32 matched line labels, including both columns of one Amharic spread read right-to-left. |
| Review workload | **0/29 baseline candidates flagged**, **0/32 upgraded candidates flagged** | Baseline's three measured merged crops were not surfaced for review. This small sample shows that the existing review flags did not detect those specific merges; it is not calibrated review recall. |
| Upgrade manifest runtime | **221.440 ms mean; 212.913 ms median; 230.336 ms p95** | Four processed full-page scans. The baseline version did not record stage timings in its manifest, so no like-for-like runtime claim is made. |

### Independent private-page comparison

The audit used two rasters from the available 83-page Amharic scan (`a_20.png`, `a_43.png`) and two from the 278-page sepia scan (`b_26.png`, `b_140.png`). Independently selected source row bands were tightened against raw source pixels; this produced 32 line boxes and 150 word-like ink groups from horizontal source-ink runs. These are geometric ink annotations, **not** OCR tokens or transcription labels. The page subset includes a two-page spread, ordinary printed paragraphs, and a dense printed paragraph. The baseline and upgrade used `--no-deskew`; Amharic spread runs used `--reading-order rtl`.

| Measure | Baseline 0.4.1 | Upgrade 0.5.0 |
| --- | ---: | ---: |
| Labeled lines | 32 | 32 |
| Predicted crops | 29 | 32 |
| One-to-one matches | 27/32 | **32/32** |
| Precision / recall | 0.931 / 0.844 | **1.000 / 1.000** |
| Merged crops | 3 | **0** |
| Cut-line / split-line indicators | 0 / 0 | **0 / 0** |
| Word-like source-ink groups fully inside matched source crops | 105/150 (70.0%) | **150/150 (100.0%)** |
| Region assignment errors / reading-order violations | 0 / 0 | **0 / 0** |
| Review candidates flagged | 0/29 | 0/32 |

The 0.5.0 result is a strong pass on this small, selected sample—not a universal guarantee. In particular, baseline review flags did not catch its three measured merges. No real handwritten or exercise-book page was present in these PDFs; workbook and hand-drawn-like content are validated only by synthetic geometry tests. The private PDFs, rasters, crops, overlays, and annotation JSON remain outside this repository.

### 0.5.0 changes exercised

- Retained whitespace/XY-cut behavior while adding confidence-gated piecewise vertical boundary paths for smooth/partial curved gutters and preserving spanning text blocks.
- Added an explicit `--layout baseline` fallback, region hierarchy/relations, sampled curved-edge provenance, and run-stage timings.
- Re-estimated individual row height robustly when adjacent printed rows merge in the projection and split oversized bands at shallow local valleys.
- Added a small source-crop safety overlap at tight vertical boundaries and widened gutter-facing safety expansion without crossing the midpoint cap; independent word-like ink containment increased from 105/150 to 150/150 in this sample.
- Extended the workbook fixture to verify each generated handwriting-like answer stroke independently, alongside printed prompts and suppression of long answer rules.

## Historical 0.4.1 and 0.4.0 results

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

## Historical 0.4.1 vertical-boundary audit

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

### Historical 0.4.0 review-workload comparison

Excluded candidates are counted as review work, rather than being discarded from the comparison.

| Corpus | Baseline review queue | 0.4.0 review queue | Queue reduction | Candidate review rate, baseline → 0.4.0 |
| --- | ---: | ---: | ---: | ---: |
| 83-page document | 658 / 3,949 (**16.7%**) | 455 / 4,103 (**11.1%**) | **30.9% fewer items** | 16.7% → 11.1% |
| 278-page document | 1,423 / 9,011 (**15.8%**) | 865 / 9,227 (**9.4%**) | **39.2% fewer items** | 15.8% → 9.4% |
| **Combined** | **2,081 / 12,960 (16.1%)** | **1,320 / 13,330 (9.9%)** | **36.6% fewer items** | **16.1% → 9.9%** |

Across the combined corpus, kept-crop review flags declined **48.8%** (1,573 → 806). The queue includes excluded candidates (508 → 514), so the end-to-end reduction is 36.6%, not 48.8%. No flags were removed merely to improve these counts. Review rates remain nonzero; illustrated final-cover pages are still noisy and remain flagged for inspection.

On emitted crops, the primary review-reason counts also fell: possible merged/oversized crops **719 → 420**, short/fragmentary crops **379 → 208**, and page-edge warnings **767 → 230**. Reasons may overlap on a crop. The full-corpus run has no line-level ground truth, so these decreases do not establish that every removed warning was unnecessary or that every true error was detected.

## Historical 0.4.0 independent held-out ROI evaluation

The four ROIs were selected on source-page rasters from two private documents. Line-row intervals were selected visually on source images and boxes were bounded from source pixels within those intervals; detector output was not used to create the labels. The evaluator uses one-to-one IoU matching at 0.50. Review workload counts `review_required` crops and excluded candidates inside each ROI. A wide crop covering multiple labeled lines is treated as a merge even when a short, indented line occupies little of that crop's area. Missed lines contribute to page-level segmentation-error recall, not crop-error recall.

| ROI | Labeled lines | Predictions | Matched | Review candidates | Crop-error review recall |
| --- | ---: | ---: | ---: | ---: | ---: |
| Document A, page 20, right-side heading and paragraph | 8 | 7 | 6 | 1 | 1/1 |
| Document A, page 62, right-side paragraph | 10 | 10 | 10 | 0 | N/A (no measured crop error) |
| Document B, page 40, title and paragraph | 11 | 10 | 9 | 1 | 1/1 |
| Document B, page 140, title and paragraph | 7 | 7 | 7 | 0 | N/A (no measured crop error) |
| **Total** | **36** | **34** | **32** | **2** | **2/2** |

The two error-containing ROIs each had a review alert. The four unmatched labels and two false-positive predictions remain real errors in this sample; review flags expose uncertainty but do not fix segmentation. These four selected regions cannot support a general accuracy or recall claim. More independently annotated pages—especially covers, edge-truncated text, faint scans, and irregular columns—are needed for a stronger calibration set.

## Historical 0.4.1 synthetic coverage and reproducibility

The 43 deterministic checks cover 24 generated pages across clean, low-contrast, gradient illumination, sparse noise, blur, JPEG, mild skew, and Ethiopic-font or pseudo-Ethiopic text (three seeds each); two blank-page rejection cases; a 15.55-megapixel throughput case; sparse row noise; close-pitch vertical rows; a four-font-size vertical-boundary/crop-isolation case; clipped top-edge text beside a dark scan-border stroke; heading/underline/detached-mark/speck/footer handling; four CLI/error-path cases; and seven layout cases. These tests check geometry, ordering, crop files, repair behavior, skew, reading order, and review metadata. They do not measure transcription or OCR accuracy.

Re-run with:

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

The private source PDFs, raster pages, crop outputs, overlays, and annotation JSON are not in the repository. The full-corpus figures above are local workload comparisons and cannot be independently reproduced from public repository contents alone.

## Historical 0.4.0 changes exercised

- Kept horizontal recursive whitespace/XY-cut layout intelligence, with columns and blocks identified before line rows.
- Refined vertical line bands using region-local row profiles, low-density valleys, connected components, and observed local line spacing.
- Split adjacent line boxes and crop windows at measured low-ink valleys, with font-scale-relative margins only at unpaired boundaries.
- Constrained each line's horizontal bounds to ink within its own vertical membership; retained detached Ethiopic marks while suppressing long page-edge strokes.
- Made scale-based uncertainty checks local to each block and tightened fragment and edge flags using structural evidence.
- Extended evaluation to report review workload, crop-error review recall, and error-page alert recall, including missed lines.

## Historical evaluation limitations

Full-corpus candidate/flag counts are not an accuracy denominator. The earlier held-out hand-labeled evaluation covers only 36 lines in four selected ROIs; its observed 100% error recall is based on just two crop-error candidates and two affected ROIs, and must not be generalized. The new vertical-boundary audit covers only 23 unique lines; two top edges remain more than 3 px from their labels, so performance across all fonts, pages, and irregular layouts is not established. Decorative covers and irregular layouts remain high-noise; review flags and excluded candidates should still be inspected on such pages. PDFs and all derived images, crops, overlays, and labels remain private and were not committed.

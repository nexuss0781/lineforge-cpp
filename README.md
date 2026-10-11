# LineForge

A fast, deterministic C++ command-line tool that turns a scanned page image into ordered grayscale line crops for OCR dataset preparation. It targets Amharic/Ethiopic documents but does not depend on the script or read/transcribe the words.

**This is a new project, not OCR-ALIGNER.** Public repository: [`nexuss0781/lineforge-cpp`](https://github.com/nexuss0781/lineforge-cpp).

## What it does

- Reads JPEG and PNG pages and writes one PNG per detected text line.
- Flattens slowly varying or colored illumination before adaptive local-contrast thresholding, with an Otsu-derived guard.
- Uses a page → whitespace-defined zones/blocks → line rows → per-line ink-bounds hierarchy; combines row-profile valleys, region-local line pitch, connected components, and the line's own vertical membership to split dense rows and constrain each horizontal crop. Top/bottom line boundaries are refined at local low-ink valleys, with scale-relative margins at unpaired edges; crop windows stop at neighboring valleys rather than leaking the next line.
- For automatically detected side-by-side regions, crops that touch an inner page edge get extra glyph-scale space into the gutter, capped at the midpoint before the neighboring region. Layout bounds and reading order are unchanged.
- Removes isolated scan specks while retaining small multi-pixel marks near glyphs; grayscale crops include extra space for detached Ethiopic marks.
- Estimates mild page skew (search range ±5°) and deskews before layout and line analysis.
- Separates text regions using recursive whitespace (XY-cut) analysis, so facing-page spreads, multi-column pages, and mixed newspaper blocks can be segmented in reading order.
- Supports explicit, ordered rectangular regions for unusual/custom page designs and a left-to-right or right-to-left automatic column order.
- Conservatively identifies centered lower-page number candidates and thin non-text fragments; excluded candidates remain in the manifest for review.
- Marks kept crops that remain oversized/merged, structurally fragmentary, or actually touch a page edge with `review_required` and explainable `review_reasons`; scale checks are local to each block rather than page-wide.
- Preserves the grayscale source pixels in each crop (except optional deskew interpolation); it does not invent or alter transcription labels.

## Build

Dependencies: C++17 compiler, CMake 3.16+, libjpeg-turbo/libjpeg development headers, libpng development headers, and Python 3 + Pillow for the synthetic benchmark. The hand-label evaluator and its unit tests use only the Python standard library.

```sh
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release
cmake --build build -j
ctest --test-dir build --output-on-failure
```

## Use

```sh
./build/linecrop --input scanned-page.jpg --output line-crops/page-001
```

Options:

- `--layout auto|single` selects automatic whitespace-based layout analysis (default) or a single full-page region for legacy behavior.
- `--reading-order ltr|rtl` sets the order of automatically detected side-by-side regions (default `ltr`). Horizontal blocks are ordered top-to-bottom.
- Repeat `--region x0,y0,x1,y1` to provide custom rectangular regions in the exact order they should be read. Coordinates are half-open `[x0,y0,x1,y1]` on the internally deskewed page; regions must be in bounds and must not overlap. When supplied, custom regions take precedence over `--layout` and their command-line order is used.
- `--no-deskew` skips skew estimation/correction.
- `--keep-page-numbers` disables the conservative footer-number filter.
- `--debug` prints intermediate foreground counts, row-run diagnostics, and per-stage timings to standard error.
- `--version` prints the engine version.

Outputs are `line_0001.png`, `line_0002.png`, … and `manifest.json`. The manifest records processed-page line bounding boxes, exact `source_crop_bbox_xyxy` rectangles (the source pixels copied before the crop's white border), crop dimensions, ink-pixel counts, estimated correction angle, detected layout regions, each line's region ID, uncertainty flags/reasons, and excluded candidates. Bounding boxes use half-open coordinates `[x0, y0, x1, y1]` on the internally deskewed page. Automatic crops follow the recursive layout order (default: columns left-to-right, blocks top-to-bottom); custom-region crops follow the order supplied on the command line.

## Validation

`tests/benchmark.py` runs **44 deterministic synthetic checks**: 24 pages (clean, low contrast, gradient illumination, sparse noise, blur, JPEG, mild skew, and Ethiopic-font/pseudo-Ethiopic text; three seeds per condition), two blank-page rejection cases, one 15.5-megapixel throughput case, sparse row-noise, dense close-pitch vertical-profile and top-edge clipping cases, a four-font-size vertical-boundary/crop-isolation case, a heading/underline/detached-mark/speck/footer case, four CLI/error-path checks, and eight layout-specific cases including a faint-ink gutter regression. It checks line count/order, crop size and files, footer handling, line repair, skew estimates, layout metadata, reading order, source crop bounds and leakage, version reporting, and invalid-input handling. These tests are reproducible geometry stress tests, **not** a human-annotated accuracy benchmark or OCR evaluation.

A separate evaluator scores manifests against hand-labeled line boxes. Example:

```sh
python3 tests/real_page_eval.py \
  --annotations labels.json \
  --output-json report.json
```

The input JSON contains `iou_threshold` (default `0.5`) and a `pages` array. Each page supplies a `manifest` path, `ground_truth_xyxy` boxes, and optionally an `roi_xyxy` region. Coordinates use the same half-open `xyxy` convention as the manifest. When an ROI is supplied, predicted boxes are included only when their centers fall inside it. The evaluator performs greedy one-to-one IoU matching and reports precision, recall, false positives, missed lines, cut-line indicators, merged crops, and split lines. It independently reports signed top/bottom boundary errors, absolute errors, clipping versus excess whitespace, and the number of boundaries within three pixels. It also reports candidate review rate (including flagged excluded candidates), crop-error review recall, and error-page review recall. Crop-error recall covers measurable erroneous emitted crops; missed lines are instead counted in the page-level error/alert recall. A cut-line indicator is raised when an overlapping crop covers under 80% of a labeled line's width or under 50% of its height; split detection uses 20% coverage of a labeled line, while a merged crop must cover at least 15% of its own area or at least 50% of each affected line. Excluded candidates do not count as emitted crops, but they do count toward review workload.

Label representative, held-out pages and ROIs; review overlay imagery; report the sample size and its scope. A score from a few selected blocks must not be generalized to every page or layout. Review flags are not silently suppressed to reduce workload; tune them against held-out crop-error recall and error-page recall.

## Accuracy and limitations

No claim of perfect segmentation is made. The page-number decision is a geometry heuristic, not OCR, and can mistake a short centered footer/caption for a page number or miss a number away from the lower center. Complex overlapping articles, weak/absent gutters, tables, marginalia, decorative covers, and irregular newspaper designs may produce false positives or require explicit `--region` rectangles and visual review. Inspect `review_required` crops and every `excluded` manifest entry before bulk processing.

Deskew is limited to mild skew (±5°); perspective and page-warp correction are out of scope. Adaptive thresholding can compensate for uneven illumination but cannot recover missing or blurred source detail. Line crops are not matched to ground-truth text; create and proofread one exact UTF-8 transcription per crop separately. Large images are limited to 100 megapixels to prevent accidental memory exhaustion.

## License

MIT. See [LICENSE](LICENSE).

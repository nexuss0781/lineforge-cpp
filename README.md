# LineForge

A fast, deterministic C++ command-line tool that turns scanned page images into ordered grayscale line crops for OCR dataset preparation. It targets Amharic/Ethiopic documents but does not depend on the script or read/transcribe the words.

**This is a new project, not OCR-ALIGNER.** Public repository: [`nexuss0781/lineforge-cpp`](https://github.com/nexuss0781/lineforge-cpp).

## V1.0.1: bounded parallel batch output

- Keeps the existing line-level detector, individual `line_####.png` files, deterministic reading order, and manifest schema while encoding and writing multiple crops concurrently for large pages.
- Limits active crop pixel buffers to 64 MiB (with a single larger crop processed alone), caps the writer pool at four threads, and stages a complete output directory before publishing it so failed runs do not expose partial results.

## V1.0.0: conservative line and crop recovery

- Estimates glyph height from nearby rows inside each detected or explicitly supplied region. A suspiciously shallow candidate can expand only where dark-mask or locally faint grayscale evidence continues; short internal valleys allow detached marks without bridging a long blank gap.
- Caps recovery by the local glyph scale and the midpoint to neighboring rows. Horizontal bounds and existing gutter caps are unchanged, so a vertical repair cannot widen a crop across a spread. Every automatically expanded candidate is review-flagged; broad, ink-bearing candidates that remain unusually shallow are flagged instead of silently trusted.
- Applies the same evidence-based path to ordinary pages, columns, spreads, and custom regions; it does not recognize page IDs or hard-code a particular source scan. Unclear source detail remains a review case rather than an inferred repair.
- Extends the independent evaluator with annotated line-box coverage, left/right word-edge retention, and optional per-side precision/recall and gutter-crossing metrics.

## Geometry-aware layout in 0.5.0

- Uses a page → layout-region/block → line hierarchy. Regions carry stable IDs, parent/next relationships, reading-order indices, and geometry provenance in `manifest.json`; lines link to their containing region.
- Keeps deterministic whitespace/XY-cut analysis and adds confidence-gated, piecewise vertical separator paths for smooth or partial curved gutters. Curved regions are ordered before row segmentation, and their output crops are masked back to the detected region path.
- Preserves spanning blocks such as headlines by validating a separator across ink-bearing rows before accepting a column split. The detector rejects paths that collide with text or behave like short word gaps.
- Refines line rows from local ink profiles and valleys. When tightly joined rows form an oversized band, it uses a lower-quantile glyph-height estimate and shallow local valleys to split the band. Source crops carry a small safety overlap at tight boundaries; gutter-facing horizontal expansion remains capped at the neighboring-region midpoint.
- Keeps a deterministic legacy-style region path available as `--layout baseline`; it disables curved-seam search. The default `--layout auto` applies the geometry-aware path, while `--layout single` keeps the page as one region.
- Supports explicit rectangular custom regions and left-to-right or right-to-left order for automatically detected side-by-side content.
- Suppresses long page/table rules from text rows where supported, keeps printed prompts and variable answer-stroke geometry in the workbook stress fixture, and reports uncertain/excluded candidates for review rather than treating them as transcription.
- Preserves grayscale source pixels in each crop (apart from optional deskew interpolation and white padding). It does not invent or alter transcription labels.

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

- `--layout auto|baseline|single` selects geometry-aware automatic layout (default), the deterministic baseline-compatible path without curved-seam search, or one full-page region.
- `--reading-order ltr|rtl` sets the order of automatically detected side-by-side regions (default `ltr`). Horizontal blocks remain top-to-bottom.
- Repeat `--region x0,y0,x1,y1` to provide custom rectangular regions in the exact order they should be read. Coordinates are half-open `[x0,y0,x1,y1]` on the internally deskewed page; regions must be in bounds and must not overlap. When supplied, custom regions take precedence over `--layout` and their command-line order is used.
- `--no-deskew` skips skew estimation/correction.
- `--keep-page-numbers` disables the conservative footer-number filter.
- `--debug` prints intermediate foreground counts, row-run diagnostics, and per-stage timings to standard error.
- `--version` prints the engine version.

Outputs are `line_0001.png`, `line_0002.png`, … and `manifest.json`. The manifest records line boxes, exact source-pixel crop rectangles, crop dimensions, ink-pixel counts, estimated correction angle, layout regions and hierarchy, reading order, uncertainty flags/reasons, excluded candidates, and decode/threshold/cleanup/segmentation/output timings. Region geometry is marked axis-aligned or piecewise-vertical and curved edges are sampled in the manifest. Bounding boxes use half-open coordinates `[x0,y0,x1,y1]` on the internally processed page. Automatic crops follow the recursive layout order; custom-region crops follow the order supplied on the command line.

## Validation

`tests/benchmark.py` runs **50 deterministic synthetic checks** covering clean/degraded scans, blank pages, a 15.5-megapixel throughput case, row noise and dense rows, multiple font scales, faint and shallow line edges with source-ink retention/review assertions, clipped edge ink, partial bottom-frame scan artifacts, detached marks, spreads and columns, a newspaper layout, gutter leakage, custom/fallback modes, a curved partial gutter with a spanning headline, undulating text baselines, and a workbook-style fixture with printed prompts, variable hand-drawn-like answer strokes, and ruled answer rows. The workbook fixture independently checks that all generated answer-stroke ink remains inside output crops and that long rules are not emitted as text. These are reproducible geometry stress tests, **not** a human-annotated accuracy benchmark, handwriting benchmark, or OCR evaluation.

A separate evaluator scores manifests against independent source annotations. Example:

```sh
python3 tests/real_page_eval.py \
  --annotations labels.json \
  --output-json report.json
```

The input JSON contains `iou_threshold` (default `0.5`) and a `pages` array. Each page supplies a manifest path and `ground_truth_xyxy` boxes; optional fields include `roi_xyxy`, source-word boxes, region boxes and line-to-region IDs, a ground-truth reading order, `side_split_x` for spread-specific left/right scoring, and review/runtime observations. The evaluator reports one-to-one IoU precision/recall, false positives, misses, cut/merged/split indicators, vertical boundary errors, annotated line-box coverage by source crops, word left/right-edge retention, region assignment, pairwise reading order, review workload/reasons, optional per-side precision/recall and gutter-crossing crops, and runtime. These geometric label-box metrics are only meaningful to the extent that their source annotations are independently corrected; they do not substitute for OCR or transcription scoring. Excluded candidates do not count as emitted crops but do count toward review workload.

For any evaluation, label held-out source pages independently of detector output, review the source overlay, state the sample size and scope, and keep private scans, crops, overlays, and annotations out of version control.

## Accuracy and limitations

No claim of perfect segmentation is made. Curved separator geometry is confidence-gated and currently represents a piecewise vertical boundary (one x position per row), not an arbitrary polygon or full page-warp model. It handles only separators supported by visible source contrast; absent/ambiguous gutters, overlapping articles, marginalia, tables, decorative covers, and irregular newspaper designs may still need explicit rectangular `--region` inputs and visual review. `--layout baseline` is available to compare against or fall back from automatic curved-seam search.

The workbook/answer-row and handwriting-like tests are synthetic. No real handwritten or exercise-book pages were available in the private evaluation sample, so those layouts are **not** claimed as validated on real handwriting or workbook data. Rule suppression does not infer table cells, answer semantics, or text. LineForge is not OCR: crops are not matched to ground-truth words and need a separate exact UTF-8 transcription. The conservative page-number decision is geometric and can mistake a short centered footer/caption for a page number or miss a number away from the lower center.

Deskew is limited to mild skew (±5°); perspective correction and arbitrary page dewarping are out of scope. Adaptive thresholding can compensate for uneven illumination but cannot recover missing or blurred source detail. Large images are limited to 100 megapixels to prevent accidental memory exhaustion. Inspect `review_required` crops and every `excluded` manifest entry before bulk processing.

## Isolated one-job pilot

`scripts/lineforge_pilot.py` is a separate local smoke-test path: it accepts exactly one staged PDF, renders page 10 once, runs LineForge once, and accepts exactly one unique line image. It refuses to reuse an output directory, records local provenance, and always marks the result non-publishable. Use a generated synthetic PDF when source-specific redistribution rights are not established. This pilot does not call the 19-worker dispatcher, GitHub Actions workflow, or Hugging Face publisher.

## LineForge dataset preparation and dispatch

The LineForge-owned dataset pipeline is documented in [`docs/dataset-pipeline.md`](docs/dataset-pipeline.md). It stages local PDFs, dispatches 19 LineForge worker processes plus one live coordinator on a single trusted runner, enforces an exact shared quota, and collects per-image provenance. It does not run OCR or training. The manual GitHub Action requires a configured dedicated runner and an approved local source manifest; its Hugging Face publisher is separately gated by a default-off manual input and fails closed until output integrity, per-source rights, and an explicit compatible dataset license are verified.

## License

MIT. See [LICENSE](LICENSE).

# LineForge

A fast, deterministic C++ command-line tool that turns a scanned page image into ordered grayscale line crops for OCR dataset preparation. It targets Amharic/Ethiopic documents but does not depend on the script or read/transcribe the words.

**This is a new project, not OCR-ALIGNER.** Proposed public repository name: `lineforge-cpp`.

## What it does

- Reads JPEG and PNG pages and writes one PNG per detected text line.
- Uses local-contrast thresholding with an Otsu-derived guard, horizontal projection bands, tight ink bounds, and small white safety borders.
- Estimates mild page skew (search range ±5°) and deskews the page before finding bands.
- Automatically separates text regions using recursive whitespace (XY-cut) analysis, so facing-page spreads, multi-column pages, and mixed newspaper blocks can be segmented in reading order.
- Supports explicit, ordered rectangular regions for unusual/custom page designs and a left-to-right or right-to-left automatic column order.
- Suppresses long vertical page-edge rules from segmentation.
- Can conservatively flag isolated compact lower-page numbers centered on a page or spread leaf; every such exclusion is listed for human review in `manifest.json`.
- Preserves the grayscale source pixels in each crop (except optional deskew interpolation); it does not invent or alter transcription labels.

## Build

Dependencies: C++17 compiler, CMake 3.16+, libjpeg-turbo/libjpeg development headers, libpng development headers, and Python 3 + Pillow for the synthetic benchmark.

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
- `--debug` prints intermediate foreground counts and per-stage timings to standard error.
- `--version` prints the engine version.

Outputs are `line_0001.png`, `line_0002.png`, … and `manifest.json`. The manifest records the processed-page bounding boxes, crop dimensions, ink-pixel counts, estimated correction angle, detected layout regions, each line's region ID, and any excluded candidates. Bounding boxes use half-open coordinates `[x0, y0, x1, y1]` on the internally deskewed page. Automatic crops follow the recursive layout order (default: columns left-to-right, blocks top-to-bottom); custom-region crops follow the order supplied on the command line.

## Validation and accuracy claims

`tests/benchmark.py` runs 38 checks: 24 synthetic pages (clean, low contrast, gradient illumination, sparse noise, blur, JPEG, mild skew, and—when Noto Ethiopic fonts are installed—rendered Ethiopic text; three seeds per condition), two uniform blank-page rejection cases, one 15.5-megapixel throughput case, four CLI/error-path checks, and seven layout-specific cases covering spreads, two-column LTR/RTL order, a mixed newspaper layout, custom regions, the single-region override, and invalid region bounds. It checks line count/order, crop size/width, output files, footer-number filtering, skew-estimate magnitude, layout metadata, version reporting, and invalid-input handling. This is a reproducible geometry stress test, **not** a human-annotated real-world segmentation accuracy benchmark, OCR evaluation, or evidence of 100% accuracy. The uploaded example page is not redistributed in this repository; validate locally on your own representative pages.

No claim of perfect segmentation is made. The page-number decision is intentionally a geometry heuristic, not OCR, and can mistake a short centered footer/caption for a page number or miss a page number placed away from the lower center. Review every `excluded` manifest entry and inspect crops before training. For sensitive/high-value corpora, keep the original page, use a held-out set with human-annotated line boxes, and measure line-detection precision/recall before bulk processing.

## Current limitations

- Automatic layout detection is whitespace-heuristic based. Complex overlapping articles, pages with few/no visible gutters, tables, marginalia, and irregular newspaper designs may need explicit `--region` rectangles and visual review. It does not detect handwriting, curved baselines, or text rotated 90°.
- Deskew is limited to mild skew (±5°); perspective and page-warp correction are out of scope.
- Adaptive thresholding supports uneven illumination but cannot recover missing or blurred source detail.
- Line crops are not matched to ground-truth text. Create and proofread one exact UTF-8 transcription per crop separately.
- Large images are limited to 100 megapixels to prevent accidental memory exhaustion.

## License

MIT. See [LICENSE](LICENSE).

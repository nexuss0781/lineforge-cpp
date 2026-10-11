# Benchmark and validation record

**Run date:** 2026-10-11
**Build:** Release, GCC 13.3.0, CMake, Ubuntu 24.04
**Benchmark command:** `python3 tests/benchmark.py --binary build/linecrop --work-dir build/benchmark-pages --output-json build/benchmark-report.json`

## Results

- **31/31 checks passed**: 24 synthetic pages (clean, low contrast, gradient-lit, noise, blur, JPEG, skew, and Ethiopic-font fixtures; three seeds each), two uniform blank-page checks, one 3600×4320 (15.55 MP) throughput case, and four CLI/error-path checks.
- **CTest:** 1/1 test passed; the registered test runs the same 31-case suite.
- The supplied 1212×101 JPEG regression produced one retained text-line crop (`[297, 4, 1094, 29]`) and flagged the centered page number (`[805, 49, 820, 71]`) for exclusion review. The scan and output crop are intentionally not included in this project.
- On this benchmark run, cases had **75.70 ms median**, **115.46 ms p95**, and **3.01 s summed process time**. The 15.55 MP page took **853 ms** end-to-end. The supplied sample took **16.70 ms** in its recorded run.
- AddressSanitizer and UndefinedBehaviorSanitizer smoke runs passed on the supplied JPEG, Ethiopic-font PNG, blank PNG, and 15.55 MP PNG.

## What was verified

Fixture assertions check retained line count, reading order, crop dimensions and widths, PNG output existence, conservative footer-number handling, mild-skew estimate magnitude, blank-page rejection, `--keep-page-numbers`, `--no-deskew`, version output, and invalid-input handling. Both JPEG and PNG decoding were exercised. The example crop was visually inspected.

## Limits

These checks validate synthetic/document-layout geometry and selected regressions only. They are **not** human-annotated page-level precision/recall, a broad real-world corpus benchmark, or OCR character/word accuracy. Timings are from one local computer and are not a performance guarantee. The page-number filter is heuristic; review its manifest exclusions and inspect crops. Before production use, benchmark against representative, held-out pages with human-annotated line boxes and separately validate exact transcripts.

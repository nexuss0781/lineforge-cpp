# Benchmark and validation record

**Run date:** 2026-10-11
**Build:** Release, GCC 13.3.0, CMake, Ubuntu 24.04
**Benchmark command:** `python3 tests/benchmark.py --binary build/linecrop --work-dir build/benchmark-pages --output-json build/benchmark-report.json`

## Results

- **38/38 checks passed**: 24 synthetic pages (clean, low contrast, gradient-lit, noise, blur, JPEG, skew, and Ethiopic-font fixtures; three seeds each), two blank-page checks, one 3600×4320 (15.55 MP) throughput case, four CLI/error-path checks, and seven layout-specific checks.
- Layout fixtures verified facing-page spreads, two-column left-to-right and right-to-left ordering, a mixed newspaper page with a full-width headline, explicitly ordered custom regions, the single-region override, and rejection of an out-of-bounds custom region.
- **CTest:** 1/1 test passed; the registered test runs the same 38-case suite.
- On this run, cases had **73.05 ms median**, **116.25 ms p95**, and **3.44 s summed process time**. The 15.55 MP page took **850.93 ms** end-to-end.
- AddressSanitizer and UndefinedBehaviorSanitizer full-suite run: **38/38 checks passed**, with leak detection enabled.

## What was verified

Fixture assertions check retained line count, crop dimensions and output files, line and column reading order, region metadata, custom-region order, conservative page-number handling including both leaves of a spread, mild-skew estimates, blank-page rejection, CLI overrides, version output, invalid input, and region-bound validation. Both JPEG and PNG decoding were exercised.

## Limits

These checks validate synthetic geometry only. They are **not** human-annotated page-level precision/recall, a broad real-world corpus benchmark, or OCR character/word accuracy. The automatic layout detector relies on whitespace gutters; overlapping articles, weak/absent gutters, tables, marginalia, and irregular layouts may require explicit regions and human review. Timings are from one local computer and are not a performance guarantee. Before production use, benchmark against representative held-out pages with human-annotated line boxes and separately validate exact transcripts.

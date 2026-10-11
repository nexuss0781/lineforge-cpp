#!/usr/bin/env python3
"""Synthetic geometry benchmark (not an OCR transcription-accuracy test)."""
import argparse
import json
import math
import random
import statistics
import subprocess
import tempfile
import time
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageFilter


def pseudo_ethiopic(draw, left, top, right, baseline, rng, ink, scale=1.0):
    """Deterministic Ethiopic-like glyph shapes with ascenders, bowls and dots."""
    x = left
    while x < right:
        cell = max(3, round(rng.randint(18, 36) * scale))
        gx1 = min(right, x + cell - max(1, round(rng.randint(2, 6) * scale)))
        gy0 = top + round(rng.randint(0, 7) * scale)
        gy1 = baseline - round(rng.randint(0, 5) * scale)
        if gx1 <= x:
            x += cell
            continue
        # Several orthogonal strokes plus occasional loops and detached marks.
        stroke = max(1, round(rng.randint(2, 4) * scale))
        draw.line((x + max(1, round(2 * scale)), gy0, x + max(1, round(2 * scale)), gy1), fill=ink, width=stroke)
        if rng.random() < 0.82:
            yy = rng.randint(gy0 + 2, max(gy0 + 2, gy1 - 2))
            draw.line((x + max(1, round(2 * scale)), yy, gx1, yy), fill=ink, width=max(1, round(rng.randint(2, 4) * scale)))
        if rng.random() < 0.66:
            draw.line((gx1, gy0 + max(1, round(3 * scale)), gx1, gy1 - max(1, round(scale))), fill=ink, width=max(1, round(rng.randint(2, 4) * scale)))
        pad = max(1, round(5 * scale))
        if rng.random() < 0.42 and gx1 - 2 * scale >= x + pad:
            draw.ellipse((x + pad, gy0 + max(1, round(2 * scale)), gx1 - max(1, round(2 * scale)), min(gy1 - 1, gy0 + round(16 * scale))), outline=ink, width=max(1, round(2 * scale)))
        if rng.random() < 0.25:
            draw.ellipse((x + pad, top - max(1, round(5 * scale)), x + pad + max(2, round(4 * scale)), top - 1), fill=ink)
        x += cell + max(1, round(rng.randint(4, 11) * scale))


def make_page(path, mode, seed, skew=0.0, size=(1000, 1200)):
    rng = random.Random(seed)
    w, h = size
    bg = 245 if mode == "low_contrast" else 255
    im = Image.new("L", (w, h), bg)
    if mode == "blank":
        im.save(path, "PNG")
        return None
    draw = ImageDraw.Draw(im)
    ink = 166 if mode == "low_contrast" else 15
    scale = min(w / 1000.0, h / 1200.0)
    top0 = round(h * 0.142)
    line_step = round(h * 0.1625)
    line_height = round(h * 0.0325)
    for i in range(4):
        y0 = top0 + i * line_step
        left, right = round(w * 0.105), round(w * 0.885)
        if mode == "ethiopic":
            font_paths = (
                "/usr/share/fonts/truetype/noto/NotoSansEthiopic-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSansEthiopic-Bold.ttf",
                "/usr/share/fonts/truetype/noto/NotoSerifEthiopic-Regular.ttf",
                "/usr/share/fonts/truetype/noto/NotoSerifEthiopic-Bold.ttf",
            )
            available = [p for p in font_paths if Path(p).is_file()]
            if available:
                font = ImageFont.truetype(available[(seed + i) % len(available)], max(18, round(31 * scale)))
                phrase = ("የአማርኛ ጽሑፍ በኢትዮጵያ የብሔራዊ ቋንቋ ነው። " if i % 2 == 0 else
                          "ይህ የምሳሌ ገጽ ለመስመር መከፋፈል የሚያገለግል ነው። ")
                text = phrase * 3
                while text and draw.textlength(text, font=font) > right - left:
                    text = text[:-1]
                draw.text((left, y0), text, fill=ink, font=font)
            else:
                pseudo_ethiopic(draw, left, y0, right, y0 + line_height, rng, ink, scale)
        else:
            pseudo_ethiopic(draw, left, y0, right, y0 + line_height, rng, ink, scale)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", max(12, round(25 * scale)))
    except OSError:
        font = ImageFont.load_default()
    # Footer number deliberately sits alone in the lower-center region.
    draw.text((w // 2 - round(7 * scale), round(h * 0.842)), "3", font=font, fill=ink)
    if mode == "gradient":
        px = im.load()
        for y in range(h):
            delta = int(28 * y / max(1, h - 1))
            for x in range(w):
                px[x, y] = min(255, max(0, px[x, y] + delta))
    if mode == "noise":
        px = im.load()
        for _ in range(int(w * h * 0.003)):
            x, y = rng.randrange(w), rng.randrange(h)
            px[x, y] = rng.choice((0, 255))
    if mode == "blur":
        im = im.filter(ImageFilter.GaussianBlur(0.45))
    if skew:
        im = im.rotate(skew, resample=Image.Resampling.BICUBIC, fillcolor=bg)
    im.save(path, "PNG")
    return None


def run_case(binary, work, mode, index, size=(1000, 1200)):
    source = work / f"{mode}_{index:02d}.png"
    out = work / f"out_{mode}_{index:02d}"
    # Include a spread of small skew values in the skew category.
    skew = (index - 1) * 1.25 if mode == "skew" else 0.0
    if mode == "jpeg":
        temp_png = work / f"jpeg_source_{index:02d}.png"
        make_page(temp_png, "clean", 9000 + index, size=size)
        Image.open(temp_png).save(source.with_suffix(".jpg"), "JPEG", quality=56)
        source = source.with_suffix(".jpg")
    else:
        make_page(source, mode, 9000 + index, skew=skew, size=size)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        return {"case": f"{mode}_{index}", "pass": False, "error": proc.stderr.strip(), "elapsed_ms": elapsed_ms}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    excluded = manifest["excluded"]
    checks = {
        "four_text_lines": len(lines) == 4,
        "one_footer_candidate_excluded": len(excluded) == 1 and excluded[0]["reason"] == "likely_centered_page_number",
        "text_crops_wide": all((b["bbox_xyxy"][2] - b["bbox_xyxy"][0]) >= 0.58 * manifest["image_size"]["width"] for b in lines),
        "reasonable_line_heights": all(0.018 <= (b["bbox_xyxy"][3] - b["bbox_xyxy"][1]) / manifest["image_size"]["height"] <= 0.12 for b in lines),
        "reading_order": [b["bbox_xyxy"][1] for b in lines] == sorted(b["bbox_xyxy"][1] for b in lines),
        "nonempty_png_crops": all((out / b["file"]).exists() and (out / b["file"]).stat().st_size > 100 for b in lines),
    }
    if mode == "skew":
        checks["deskew_estimate"] = abs(abs(manifest["deskew_correction_degrees"]) - abs(skew)) <= 1.0
    return {"case": f"{mode}_{index}", "pass": all(checks.values()), "checks": checks,
            "elapsed_ms": round(elapsed_ms, 2), "deskew_degrees": manifest["deskew_correction_degrees"],
            "line_count": len(lines), "excluded_count": len(excluded)}


def run_blank_case(binary, work, dark=False):
    name = "blank_dark" if dark else "blank_white"
    source, out = work / f"{name}.png", work / f"out_{name}"
    Image.new("L", (800, 1000), 0 if dark else 255).save(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)], text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    manifest_path = out / "manifest.json"
    ok = proc.returncode == 2 and manifest_path.exists()
    if ok:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        ok = manifest["lines"] == [] and manifest["excluded"] == []
    return {"case": name, "pass": ok, "elapsed_ms": round(elapsed_ms, 2), "checks": {"empty_page_rejected": ok}}


def run_cli_cases(binary, work):
    results = []
    source = work / "clean_00.png"
    out = work / "out_keep_numbers"
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out), "--keep-page-numbers"],
                          text=True, capture_output=True, check=False)
    elapsed = (time.perf_counter() - started) * 1000
    ok = proc.returncode == 0 and (out / "manifest.json").exists()
    if ok:
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        ok = len(manifest["lines"]) == 5 and manifest["excluded"] == []
    results.append({"case": "flag_keep_page_numbers", "pass": ok, "elapsed_ms": round(elapsed, 2),
                     "checks": {"footer_preserved": ok}})

    source = work / "skew_00.png"
    out = work / "out_no_deskew"
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out), "--no-deskew"],
                          text=True, capture_output=True, check=False)
    elapsed = (time.perf_counter() - started) * 1000
    ok = proc.returncode == 0 and (out / "manifest.json").exists()
    if ok:
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        ok = len(manifest["lines"]) == 4 and abs(manifest["deskew_correction_degrees"]) < 1e-9
    results.append({"case": "flag_no_deskew", "pass": ok, "elapsed_ms": round(elapsed, 2),
                     "checks": {"deskew_disabled": ok}})

    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--version"], text=True, capture_output=True, check=False)
    elapsed = (time.perf_counter() - started) * 1000
    ok = proc.returncode == 0 and "linecrop 0.1.0" in proc.stdout
    results.append({"case": "cli_version", "pass": ok, "elapsed_ms": round(elapsed, 2),
                     "checks": {"version_reported": ok}})

    bad = work / "invalid.bin"
    bad.write_bytes(b"not an image")
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(bad), "--output", str(work / "out_invalid")],
                          text=True, capture_output=True, check=False)
    elapsed = (time.perf_counter() - started) * 1000
    ok = proc.returncode == 1 and "unsupported input" in proc.stderr
    results.append({"case": "cli_invalid_input", "pass": ok, "elapsed_ms": round(elapsed, 2),
                     "checks": {"invalid_input_rejected": ok}})
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--binary", required=True, type=Path)
    ap.add_argument("--work-dir", type=Path)
    ap.add_argument("--output-json", type=Path)
    args = ap.parse_args()
    modes = ("clean", "low_contrast", "gradient", "noise", "blur", "jpeg", "skew", "ethiopic")
    if args.work_dir:
        args.work_dir.mkdir(parents=True, exist_ok=True)
        work = args.work_dir.resolve()
        results = [run_case(args.binary.resolve(), work, mode, i) for mode in modes for i in range(3)]
        results.extend(run_blank_case(args.binary.resolve(), work, dark=dark) for dark in (False, True))
        results.append(run_case(args.binary.resolve(), work, "large", 0, size=(3600, 4320)))
        results.extend(run_cli_cases(args.binary.resolve(), work))
    else:
        with tempfile.TemporaryDirectory(prefix="lineforge-bench-") as tmp:
            work = Path(tmp)
            results = [run_case(args.binary.resolve(), work, mode, i) for mode in modes for i in range(3)]
            results.extend(run_blank_case(args.binary.resolve(), work, dark=dark) for dark in (False, True))
            results.append(run_case(args.binary.resolve(), work, "large", 0, size=(3600, 4320)))
            results.extend(run_cli_cases(args.binary.resolve(), work))
    timings = [r["elapsed_ms"] for r in results]
    report = {
        "benchmark": "synthetic line-segmentation geometry",
        "scope_note": "Synthetic geometric stress tests only; not a handwriting, OCR, or human-annotated accuracy benchmark.",
        "cases": len(results), "passed": sum(r["pass"] for r in results),
        "failed": [r for r in results if not r["pass"]],
        "runtime_ms": {"median": round(statistics.median(timings), 2), "p95": round(sorted(timings)[math.ceil(0.95 * len(timings)) - 1], 2),
                       "total": round(sum(timings), 2)},
        "case_results": results,
    }
    text = json.dumps(report, indent=2)
    if args.output_json:
        args.output_json.parent.mkdir(parents=True, exist_ok=True)
        args.output_json.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0 if report["passed"] == report["cases"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

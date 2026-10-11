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

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter


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


def make_row_noise_fixture(path):
    """Draw eight pseudo-Ethiopic rows with sparse scan dust in the gutters."""
    im = Image.new("L", (1000, 1000), 255)
    draw = ImageDraw.Draw(im)
    rng = random.Random(117)
    tops = [130 + i * 75 for i in range(8)]
    for y in tops:
        pseudo_ethiopic(draw, 100, y, 900, y + 26, rng, ink=12)
    # Repeated short scan marks keep a low global row median but can bridge
    # neighboring line bands under a fixed, very low projection threshold.
    for i in range(len(tops) - 1):
        for y in range(tops[i] + 30, tops[i + 1], 3):
            x = rng.randrange(100, 890)
            draw.line((x, y, min(899, x + 3), y), fill=35, width=1)
    im.save(path, "PNG")


def run_row_noise_case(binary, work):
    source, out = work / "row_noise_bridge.png", work / "out_row_noise_bridge"
    make_row_noise_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0 or not (out / "manifest.json").exists():
        return {"case": "row_noise_bridge", "pass": False, "error": proc.stderr.strip(),
                "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    width, height = manifest["image_size"]["width"], manifest["image_size"]["height"]
    checks = {
        "eight_rows_separated": len(lines) == 8,
        "reasonable_line_heights": all(0.015 <= (b["bbox_xyxy"][3] - b["bbox_xyxy"][1]) / height <= 0.05 for b in lines),
        "text_widths_preserved": all((b["bbox_xyxy"][2] - b["bbox_xyxy"][0]) >= 0.55 * width for b in lines),
        "reading_order": [b["bbox_xyxy"][1] for b in lines] == sorted(b["bbox_xyxy"][1] for b in lines),
        "no_false_footer": manifest["excluded"] == [],
        "nonempty_crops": all((out / b["file"]).exists() and (out / b["file"]).stat().st_size > 100 for b in lines),
    }
    return {"case": "row_noise_bridge", "pass": all(checks.values()), "checks": checks,
            "elapsed_ms": round(elapsed_ms, 2), "line_count": len(lines)}


def make_dense_vertical_fixture(path):
    """Create close-pitch rows whose projection valleys are narrow and shallow."""
    im = Image.new("L", (1000, 900), 255)
    draw = ImageDraw.Draw(im)
    rng = random.Random(314)
    for i in range(12):
        top = 100 + i * 38
        pseudo_ethiopic(draw, 90, top, 910, top + 35, rng, ink=12, scale=0.9)
    im.save(path, "PNG")


def run_dense_vertical_case(binary, work):
    source, out = work / "dense_vertical_profile.png", work / "out_dense_vertical_profile"
    make_dense_vertical_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0 or not (out / "manifest.json").exists():
        return {"case": "dense_vertical_profile", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    width, height = manifest["image_size"]["width"], manifest["image_size"]["height"]
    boxes = [b["bbox_xyxy"] for b in lines]
    checks = {
        "twelve_close_pitch_rows_recovered": len(lines) == 12,
        "line_heights_remain_local": all(0.015 <= (b[3] - b[1]) / height <= 0.06 for b in boxes),
        "row_order_is_monotonic": [b[1] for b in boxes] == sorted(b[1] for b in boxes),
        "horizontal_membership_is_preserved": all((b[2] - b[0]) >= 0.70 * width for b in boxes),
        "crops_are_nonempty": all((out / b["file"]).exists() and (out / b["file"]).stat().st_size > 100
                                   for b in lines),
    }
    return {"case": "dense_vertical_profile", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2), "line_count": len(lines)}


def make_vertical_font_scale_fixture(path):
    """Render close-spaced lines at several font sizes with known whitespace boundaries."""
    im = Image.new("L", (1200, 720), 248)
    lines = [
        (15, "Agj pqy bdfhkl  small text with ascenders and descenders"),
        (24, "Agj pqy bdfhkl  a medium text line with visible upper and lower forms"),
        (37, "Agj pqy bdfhkl  LARGE text gives this row a much taller glyph envelope"),
        (19, "Agj pqy bdfhkl  final small line after the large font"),
    ]
    draw = ImageDraw.Draw(im)
    try:
        font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf"
        fonts = [ImageFont.truetype(font_path, size) for size, _ in lines]
    except OSError:
        fonts = [ImageFont.load_default() for _ in lines]

    ink_boxes = []
    cursor_y = 145
    for (size, text), font in zip(lines, fonts):
        layer = Image.new("L", im.size, 255)
        layer_draw = ImageDraw.Draw(layer)
        layer_draw.text((90, cursor_y), text, font=font, fill=18)
        bbox = ImageChops.difference(layer, Image.new("L", im.size, 255)).getbbox()
        if bbox is None:
            raise RuntimeError("synthetic font line unexpectedly rendered empty")
        im.paste(layer.crop(bbox), bbox[:2])
        ink_boxes.append(bbox)
        # Keep line gaps shorter than a paragraph separator, while varying with
        # font size rather than holding a fixed pixel pitch.
        cursor_y = bbox[3] + max(6, round(size * 0.36))

    # A vertical illumination gradient keeps the test from depending on a flat
    # white background, without changing the hand-known glyph row envelopes.
    px = im.load()
    for y in range(im.height):
        delta = int(14 * y / max(1, im.height - 1))
        for x in range(im.width):
            px[x, y] = min(255, px[x, y] + delta)
    im.save(path, "PNG")

    # The target is the midpoint of each visible whitespace valley. At the two
    # block ends, use a scale-relative margin matching the engine's line-height
    # convention, not a fixed image-coordinate allowance.
    target = [[box[1], box[3]] for box in ink_boxes]
    for i in range(len(target) - 1):
        cut = round((ink_boxes[i][3] + ink_boxes[i + 1][1]) / 2)
        target[i][1] = cut
        target[i + 1][0] = cut
    target[0][0] = max(0, ink_boxes[0][1] - max(1, round((ink_boxes[0][3] - ink_boxes[0][1]) * 0.18)))
    target[-1][1] = ink_boxes[-1][3] + max(1, round((ink_boxes[-1][3] - ink_boxes[-1][1]) * 0.18))
    return target


def run_vertical_font_scale_case(binary, work):
    source, out = work / "vertical_font_scales.png", work / "out_vertical_font_scales"
    target = make_vertical_font_scale_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0 or not (out / "manifest.json").exists():
        return {"case": "vertical_font_scales", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    boxes = sorted((item["bbox_xyxy"] for item in manifest["lines"]), key=lambda box: box[1])
    errors = [(box[1] - expected[0], box[3] - expected[1])
              for box, expected in zip(boxes, target)]
    crop_row_groups = []
    for item in manifest["lines"]:
        crop = Image.open(out / item["file"]).convert("L")
        pixels = crop.load()
        active_rows = [y for y in range(crop.height)
                       if sum(pixels[x, y] < 150 for x in range(crop.width)) >
                       max(2, round(crop.width * 0.002))]
        crop_row_groups.append(0 if not active_rows else
                               1 + sum(y - previous > 3
                                       for previous, y in zip(active_rows, active_rows[1:])))
    checks = {
        "four_different_font_sizes_detected": len(boxes) == len(target),
        "top_boundaries_within_3px": len(boxes) == len(target) and all(abs(e[0]) <= 3 for e in errors),
        "bottom_boundaries_within_3px": len(boxes) == len(target) and all(abs(e[1]) <= 3 for e in errors),
        "neighboring_text_not_in_crops": len(crop_row_groups) == len(target) and all(n == 1 for n in crop_row_groups),
        "row_order_is_monotonic": [box[1] for box in boxes] == sorted(box[1] for box in boxes),
    }
    return {"case": "vertical_font_scales", "pass": all(checks.values()),
            "checks": checks, "top_bottom_error_px": errors,
            "dark_row_groups_per_crop": crop_row_groups,
            "elapsed_ms": round(elapsed_ms, 2), "line_count": len(boxes)}


def make_faint_edge_fixture(path, faint_ink):
    """Create normal rows plus a short, low-contrast line with known ink bounds."""
    im = Image.new("L", (1000, 650), 255)
    draw = ImageDraw.Draw(im)
    for index in range(7):
        top = 30 + index * 75
        pseudo_ethiopic(draw, 100, top, 900, top + 30,
                        random.Random(100 + index), ink=12, scale=0.9)

    top = 555
    layer = Image.new("L", im.size, 255)
    layer_draw = ImageDraw.Draw(layer)
    pseudo_ethiopic(layer_draw, 100, top, 650, top + 30,
                    random.Random(23), ink=0, scale=0.9)
    ink_mask = ImageChops.difference(layer, Image.new("L", im.size, 255))
    target = ink_mask.getbbox()
    if target is None:
        raise RuntimeError("faint-edge fixture unexpectedly rendered empty")
    mask_pixels, page_pixels = ink_mask.load(), im.load()
    for y in range(target[1], target[3]):
        for x in range(target[0], target[2]):
            if mask_pixels[x, y]:
                page_pixels[x, y] = 12 if top + 9 <= y < top + 23 else faint_ink
    im.save(path, "PNG")
    return target


def run_faint_edge_recovery_case(binary, work):
    recovered_source = work / "faint_edges_recoverable.png"
    recovered_out = work / "out_faint_edges_recoverable"
    ambiguous_source = work / "faint_edges_ambiguous.png"
    ambiguous_out = work / "out_faint_edges_ambiguous"
    target = make_faint_edge_fixture(recovered_source, faint_ink=155)
    make_faint_edge_fixture(ambiguous_source, faint_ink=165)

    started = time.perf_counter()
    recovered_proc = subprocess.run(
        [str(binary), "--input", str(recovered_source), "--output", str(recovered_out),
         "--layout", "single", "--no-deskew"], text=True, capture_output=True, check=False)
    ambiguous_proc = subprocess.run(
        [str(binary), "--input", str(ambiguous_source), "--output", str(ambiguous_out),
         "--layout", "single", "--no-deskew"], text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if (recovered_proc.returncode != 0 or ambiguous_proc.returncode != 0 or
            not (recovered_out / "manifest.json").exists() or
            not (ambiguous_out / "manifest.json").exists()):
        return {"case": "faint_edge_recovery", "pass": False,
                "error": recovered_proc.stderr.strip() or ambiguous_proc.stderr.strip(),
                "elapsed_ms": round(elapsed_ms, 2)}

    recovered_manifest = json.loads((recovered_out / "manifest.json").read_text(encoding="utf-8"))
    ambiguous_manifest = json.loads((ambiguous_out / "manifest.json").read_text(encoding="utf-8"))
    recovered_lines, ambiguous_lines = recovered_manifest["lines"], ambiguous_manifest["lines"]
    recovered_line, ambiguous_line = recovered_lines[-1], ambiguous_lines[-1]
    source_box = recovered_line["source_crop_bbox_xyxy"]
    source_image = Image.open(recovered_source).convert("L")
    source_pixels = source_image.load()
    x0, y0, x1, y1 = target
    ink_pixels = [(x, y) for y in range(y0, y1) for x in range(x0, x1)
                  if source_pixels[x, y] < 250]
    retained_ink = sum(source_box[0] <= x < source_box[2] and
                       source_box[1] <= y < source_box[3] for x, y in ink_pixels)
    contains_all_ink = bool(ink_pixels) and retained_ink == len(ink_pixels)
    review_reason = "shallow_candidate_expanded_from_local_ink"
    checks = {
        "eight_rows_detected_in_both_cases": len(recovered_lines) == 8 and len(ambiguous_lines) == 8,
        "recoverable_faint_line_retains_all_source_ink": contains_all_ink,
        "recovered_line_remains_visible_for_review": recovered_line["review_required"] and
            review_reason in recovered_line["review_reasons"],
        "ambiguous_faint_line_is_flagged_not_silently_trusted": ambiguous_line["review_required"] and
            bool(ambiguous_line["review_reasons"]),
        "row_order_is_monotonic": [line["bbox_xyxy"][1] for line in recovered_lines] ==
            sorted(line["bbox_xyxy"][1] for line in recovered_lines),
    }
    return {"case": "faint_edge_recovery", "pass": all(checks.values()),
            "checks": checks, "target_ink_bbox_xyxy": target,
            "retained_source_ink_pixels": retained_ink,
            "target_source_ink_pixels": len(ink_pixels),
            "recovered_source_crop_bbox_xyxy": source_box,
            "ambiguous_review_reasons": ambiguous_line["review_reasons"],
            "elapsed_ms": round(elapsed_ms, 2), "line_count": len(recovered_lines)}


def make_edge_clipping_fixture(path):
    im = Image.new("L", (1000, 900), 255)
    draw = ImageDraw.Draw(im)
    rng = random.Random(81)
    pseudo_ethiopic(draw, 100, -15, 900, 25, rng, ink=12, scale=0.9)
    pseudo_ethiopic(draw, 100, 200, 900, 235, rng, ink=12, scale=0.9)
    # A genuine dark scan-border stroke should be removed without extending
    # the text crop to the page edge or clearing its clipping alert.
    draw.line((999, 0, 999, 300), fill=0, width=2)
    im.save(path, "PNG")


def run_edge_clipping_case(binary, work):
    source, out = work / "edge_clipping.png", work / "out_edge_clipping"
    make_edge_clipping_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0 or not (out / "manifest.json").exists():
        return {"case": "edge_clipping", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    top = next((b for b in lines if b["bbox_xyxy"][1] < 40), None)
    lower = next((b for b in lines if b["bbox_xyxy"][1] > 100), None)
    checks = {
        "both_lines_preserved": len(lines) == 2 and top is not None and lower is not None,
        "cropped_top_line_flagged": top is not None and "possible_page_edge_clipping" in top["review_reasons"],
        "uncropped_line_not_edge_flagged": lower is not None and "possible_page_edge_clipping" not in lower["review_reasons"],
        "border_stroke_excluded_from_horizontal_bounds": top is not None and top["bbox_xyxy"][2] < manifest["image_size"]["width"] - 20,
    }
    return {"case": "edge_clipping", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2), "line_count": len(lines)}


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


def make_heading_underline_fixture(path):
    im = Image.new("L", (1000, 1200), 255)
    draw = ImageDraw.Draw(im)
    try:
        heading = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
    except OSError:
        heading = ImageFont.load_default()
    draw.text((330, 88), "SECTION TWO", font=heading, fill=12)
    draw.line((330, 132, 665, 132), fill=12, width=3)
    rng = random.Random(4401)
    tops = (240, 420, 600, 780)
    for y in tops:
        pseudo_ethiopic(draw, 110, y, 890, y + 34, rng, ink=12)
    # A detached paired mark just above the first line should stay inside its
    # grayscale crop; a distant singleton should not become a crop of its own.
    draw.rectangle((112, 233, 113, 234), fill=12)
    draw.point((950, 405), fill=12)
    try:
        footer = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 25)
    except OSError:
        footer = ImageFont.load_default()
    draw.text((493, 1010), "3", font=footer, fill=12)
    im.save(path, "PNG")


def run_heading_underline_case(binary, work):
    source, out = work / "heading_underline.png", work / "out_heading_underline"
    make_heading_underline_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        return {"case": "heading_underline_marks", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines, excluded = manifest["lines"], manifest["excluded"]
    heading_lines = [b for b in lines if b["bbox_xyxy"][1] < 180]
    first_body = next((b for b in lines if 220 <= b["bbox_xyxy"][1] <= 280), None)
    mark_retained = False
    if first_body:
        x0, y0, x1, y1 = first_body["source_crop_bbox_xyxy"]
        crop = Image.open(out / first_body["file"]).convert("L")
        border_x = (crop.width - (x1 - x0)) // 2
        border_y = (crop.height - (y1 - y0)) // 2
        dot_region = crop.crop((112 - x0 + border_x, 233 - y0 + border_y,
                                114 - x0 + border_x, 235 - y0 + border_y))
        mark_retained = dot_region.getextrema()[0] < 100
    separate_underline_crop = any(
        b["bbox_xyxy"][1] >= 125 and b["bbox_xyxy"][3] <= 140 and
        b["bbox_xyxy"][2] - b["bbox_xyxy"][0] > 200 for b in lines
    )
    false_singleton = any(
        b["bbox_xyxy"][0] <= 950 < b["bbox_xyxy"][2] and
        b["bbox_xyxy"][1] <= 405 < b["bbox_xyxy"][3] for b in lines
    )
    checks = {
        "heading_kept_once": len(heading_lines) == 1,
        "underline_not_emitted_as_own_crop": not separate_underline_crop,
        "four_body_lines_kept": len(lines) - len(heading_lines) == 4,
        "detached_mark_retained_in_crop": mark_retained,
        "singleton_speck_not_a_crop": not false_singleton,
        "one_footer_number_excluded": sum(e["reason"] == "likely_centered_page_number" for e in excluded) == 1,
    }
    return {"case": "heading_underline_marks", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2),
            "line_count": len(lines), "excluded_count": len(excluded)}


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
    ok = proc.returncode == 0 and "linecrop 1.0.0" in proc.stdout
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


def draw_fixture_line(draw, font, left, right, y, seed):
    """Draw a deterministic line-like phrase that nearly fills one text region."""
    phrases = ("ARCHIVE TEXT SAMPLE 0123456789  ",
               "COLUMN LAYOUT TEST EXAMPLE 987654321  ",
               "READING ORDER AND REGION DETECTION  ")
    text = phrases[seed % len(phrases)]
    while draw.textlength(text, font=font) < right - left:
        text += phrases[seed % len(phrases)]
    while text and draw.textlength(text, font=font) > right - left:
        text = text[:-1]
    draw.text((left, y), text, font=font, fill=12)


def make_layout_fixture(path, kind):
    if kind == "newspaper":
        size = (1200, 1000)
    elif kind == "spread":
        size = (1200, 1100)
    else:
        size = (1000, 1000)
    im = Image.new("L", size, 255)
    draw = ImageDraw.Draw(im)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 24)
    except OSError:
        font = ImageFont.load_default()
    if kind == "spread":
        for i, y in enumerate((150, 280, 410, 540)):
            draw_fixture_line(draw, font, 70, 520, y, i)
            draw_fixture_line(draw, font, 680, 1130, y, i + 1)
        # The dark scan binding must not connect both pages into one text line.
        draw.line((600, 15, 600, 1085), fill=0, width=12)
        # Short frame marks and detached specks must not become OCR line crops.
        draw.line((2, 720, 2, 780), fill=0, width=4)
        draw.rectangle((120, 800, 126, 806), fill=0)
        # Sparse dots from a scan-bed rule are not a long text line.
        for x in range(800, 1100, 30):
            draw.rectangle((x, 700, x + 2, 701), fill=0)
        # A full-width scan-bed bar must not be treated as another text line.
        draw.line((0, 1092, 1199, 1092), fill=0, width=6)
        # Facing-page footers sit in the center of each leaf, not the spread.
        draw.text((295, 930), "7", font=font, fill=12)
        draw.text((895, 930), "8", font=font, fill=12)
    elif kind in ("columns", "custom"):
        for i, y in enumerate((140, 270, 400, 530)):
            draw_fixture_line(draw, font, 55, 445, y, i)
            draw_fixture_line(draw, font, 555, 945, y, i + 1)
    elif kind == "newspaper":
        try:
            headline_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 34)
        except OSError:
            headline_font = font
        headline = "NEWSPAPER HEADLINE SPANS BOTH COLUMNS  "
        while draw.textlength(headline, font=headline_font) < 1040:
            headline += "NEWSPAPER HEADLINE  "
        while draw.textlength(headline, font=headline_font) > 1040:
            headline = headline[:-1]
        draw.text((75, 70), headline, font=headline_font, fill=12)
        for i, y in enumerate((220, 340, 460, 580)):
            draw_fixture_line(draw, font, 70, 530, y, i)
            draw_fixture_line(draw, font, 670, 1130, y + (25 if i == 2 else 0), i + 1)
    im.save(path, "PNG")


def make_gutter_bleed_fixture(path):
    """Draw a facing spread with faint right-edge ink below the mask threshold."""
    im = Image.new("L", (1600, 900), 255)
    draw = ImageDraw.Draw(im)
    for y in (150, 280, 410, 540, 670):
        for page_x in (62, 1112):
            for i in range(20):
                x = page_x + 24 * i
                draw.line((x, y + 5, x, y + 31), fill=12, width=3)
                draw.line((x, y + 15, x + 18, y + 15), fill=12, width=3)
                draw.line((x + 18, y + 5, x + 18, y + 31), fill=12, width=3)
        # Visible in the grayscale source but intentionally absent from the
        # foreground mask, simulating ink that fades into a scanned gutter.
        draw.rectangle((546, y + 17, 557, y + 20), fill=180)
    draw.line((800, 15, 800, 885), fill=0, width=10)
    im.save(path, "PNG")


def run_layout_case(binary, work, kind, reading_order="ltr"):
    source = work / f"layout_{kind}.png"
    out = work / f"out_layout_{kind}_{reading_order}"
    make_layout_fixture(source, "spread" if kind == "spread" else
                        "newspaper" if kind == "newspaper" else "columns")
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out),
                           "--reading-order", reading_order],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        return {"case": f"layout_{kind}_{reading_order}", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": elapsed_ms}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    width = manifest["image_size"]["width"]
    expected = 8 if kind in ("spread", "columns") else 9
    centers = [(b["bbox_xyxy"][0] + b["bbox_xyxy"][2]) / 2 for b in lines]
    if kind == "newspaper":
        ordered = len(centers) == 9 and centers[0] > width * 0.35 and centers[0] < width * 0.65
        left_start = 1
    else:
        ordered = len(centers) == 8
        left_start = 0
    if ordered:
        first_half = centers[left_start:left_start + 4]
        second_half = centers[left_start + 4:left_start + 8]
        if reading_order == "rtl":
            ordered = all(x > width * 0.5 for x in first_half) and all(x < width * 0.5 for x in second_half)
        else:
            ordered = all(x < width * 0.5 for x in first_half) and all(x > width * 0.5 for x in second_half)
    expected_excluded = 2 if kind == "spread" else 0
    checks = {
        "line_count": len(lines) == expected,
        "layout_regions_reported": bool(manifest["layout"]["regions"]),
        "requested_reading_order_reported": manifest["layout"]["reading_order"] == reading_order,
        "column_major_reading_order": ordered,
        "footer_candidates": sum(e["reason"] == "likely_centered_page_number" for e in manifest["excluded"]) == expected_excluded,
        "edge_artifacts_reported": kind != "spread" or sum(e["reason"] == "likely_non_text_fragment" for e in manifest["excluded"]) >= 3,
        "bottom_scan_bar_not_emitted": kind != "spread" or all(
            b["bbox_xyxy"][1] < manifest["image_size"]["height"] * 0.985 for b in lines),
        "nonempty_crops": all((out / b["file"]).exists() and (out / b["file"]).stat().st_size > 100 for b in lines),
    }
    return {"case": f"layout_{kind}_{reading_order}", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2),
            "line_count": len(lines), "excluded_count": len(manifest["excluded"]),
            "region_count": len(manifest["layout"]["regions"])}


def run_gutter_margin_case(binary, work):
    source, out = work / "layout_gutter_bleed.png", work / "out_layout_gutter_bleed"
    make_gutter_bleed_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        return {"case": "layout_gutter_bleed", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}

    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    width = manifest["image_size"]["width"]
    left = [line for line in lines
            if (line["bbox_xyxy"][0] + line["bbox_xyxy"][2]) / 2 < width / 2]
    right = [line for line in lines
             if (line["bbox_xyxy"][0] + line["bbox_xyxy"][2]) / 2 >= width / 2]
    marker_counts = {line["file"]: Image.open(out / line["file"]).convert("L").histogram()[180]
                     for line in lines}
    left_end = max((line["bbox_xyxy"][2] for line in left), default=width)
    right_start = min((line["bbox_xyxy"][0] for line in right), default=0)
    gutter_midpoint = width // 2
    crop_bounds_reported = all("source_crop_bbox_xyxy" in line for line in lines)
    left_leaks = [line["file"] for line in left if crop_bounds_reported and
                  line["source_crop_bbox_xyxy"][2] > right_start]
    right_leaks = [line["file"] for line in right if crop_bounds_reported and
                   line["source_crop_bbox_xyxy"][0] < left_end]
    left_in_reading_order = len(left) == 5 and len(right) == 5 and all(
        (line["bbox_xyxy"][0] + line["bbox_xyxy"][2]) / 2 < width / 2
        for line in lines[:5])
    checks = {
        "ten_lines_separated_by_page": len(lines) == 10 and len(left) == 5 and len(right) == 5,
        "left_to_right_page_order_preserved": left_in_reading_order,
        "source_crop_bounds_reported": crop_bounds_reported,
        "faint_gutter_tails_retained": all(marker_counts[line["file"]] == 48 for line in left),
        "no_faint_tail_copied_to_opposite_page": all(marker_counts[line["file"]] == 0 for line in right),
        "crop_windows_do_not_cross_gutter_midpoint": crop_bounds_reported and
            all(line["source_crop_bbox_xyxy"][2] <= gutter_midpoint for line in left) and
            all(line["source_crop_bbox_xyxy"][0] >= gutter_midpoint for line in right),
        "no_crop_reaches_opposite_text_column": crop_bounds_reported and not left_leaks and not right_leaks,
        "no_new_review_flags": all(not line["review_required"] for line in lines),
        "no_footer_candidates_created": not manifest["excluded"],
    }
    return {"case": "layout_gutter_bleed", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2),
            "left_line_count": len(left), "right_line_count": len(right),
            "cross_column_leaks": len(left_leaks) + len(right_leaks)}


def run_custom_region_case(binary, work):
    source, out = work / "layout_custom_regions.png", work / "out_layout_custom_regions"
    make_layout_fixture(source, "custom")
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out),
                           "--region", "500,80,990,800", "--region", "10,80,490,800"],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    ok = proc.returncode == 0 and (out / "manifest.json").exists()
    checks = {"custom_regions_accepted": ok}
    if ok:
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        centers = [(b["bbox_xyxy"][0] + b["bbox_xyxy"][2]) / 2 for b in manifest["lines"]]
        checks = {
            "custom_mode_reported": manifest["layout"]["mode"] == "custom",
            "explicit_order_reported": manifest["layout"]["reading_order"] == "explicit",
            "explicit_region_order": len(centers) == 8 and all(x > 500 for x in centers[:4]) and all(x < 500 for x in centers[4:]),
            "region_labels_present": all("region" in b for b in manifest["lines"]),
        }
    return {"case": "layout_custom_regions", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2)}


def run_single_layout_case(binary, work):
    source, out = work / "layout_single.png", work / "out_layout_single"
    make_layout_fixture(source, "columns")
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out), "--layout", "single"],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    ok = proc.returncode == 0 and (out / "manifest.json").exists()
    checks = {"single_layout_accepted": ok}
    if ok:
        manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
        boxes = [b["bbox_xyxy"] for b in manifest["lines"]]
        checks = {"single_mode_reported": manifest["layout"]["mode"] == "single",
                  "rows_remain_full_width": len(boxes) == 4 and all(b[0] < 500 < b[2] for b in boxes)}
    return {"case": "layout_single_override", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2)}


def run_bad_custom_region_case(binary, work):
    source = work / "layout_bad_custom_region.png"
    make_layout_fixture(source, "columns")
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(work / "out_bad_region"),
                           "--region", "0,0,1100,800"], text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    ok = proc.returncode == 1 and "outside the deskewed image bounds" in proc.stderr
    return {"case": "layout_custom_region_out_of_bounds", "pass": ok,
            "checks": {"out_of_bounds_region_rejected": ok}, "elapsed_ms": round(elapsed_ms, 2)}


def _font(size=24, bold=False):
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    path = f"/usr/share/fonts/truetype/dejavu/{name}"
    try:
        return ImageFont.truetype(path, size)
    except OSError:
        return ImageFont.load_default()


def _draw_annotated_phrase(image, draw, font, left, right, y, seed):
    phrases = ("IRREGULAR LAYOUT READING ORDER  ", "CONSERVATIVE TEXT BLOCK GEOMETRY  ",
               "COLUMN CONTENT REMAINS COMPLETE  ")
    text = phrases[seed % len(phrases)]
    while draw.textlength(text, font=font) < right - left:
        text += phrases[seed % len(phrases)]
    while text and draw.textlength(text, font=font) > right - left:
        text = text[:-1]
    draw.text((left, y), text, font=font, fill=10)
    box = draw.textbbox((left, y), text, font=font)
    sample = image.crop(box)
    actual = ImageChops.difference(sample, Image.new("L", sample.size, 255)).getbbox()
    return (box[0] + actual[0], box[1] + actual[1],
            box[0] + actual[2], box[1] + actual[3]) if actual else box


def make_curved_gutter_fixture(path):
    """A spanning headline over two text columns split by a smooth curved gutter."""
    im = Image.new("L", (1200, 1100), 255)
    draw = ImageDraw.Draw(im)
    title_font, font = _font(32, True), _font(23)
    title = "SPANNING HEADLINE OVER CURVED NEWSPAPER COLUMNS"
    draw.text((90, 65), title, font=title_font, fill=10)
    title_box = draw.textbbox((90, 65), title, font=title_font)
    labels = [("title", title_box)]
    ys = (220, 350, 480, 610, 740, 870)
    for i, y in enumerate(ys):
        gutter = 600 + round(82 * math.sin((y - 220) * 2 * math.pi / 720))
        labels.append(("left", _draw_annotated_phrase(im, draw, font, 65, gutter - 48, y, i)))
        labels.append(("right", _draw_annotated_phrase(im, draw, font, gutter + 48, 1135, y, i + 1)))
    im.save(path, "PNG")
    return labels


def run_curved_layout_case(binary, work):
    source, out = work / "layout_curved_gutter.png", work / "out_layout_curved_gutter"
    labels = make_curved_gutter_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out), "--no-deskew"],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        return {"case": "layout_curved_partial_gutter", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    regions = manifest["layout"]["regions"]
    geometry = any(r.get("geometry") == "piecewise_vertical_boundary" for r in regions)
    timing = manifest.get("timing_ms", {})
    timing_keys = ("decode", "threshold", "cleanup_and_deskew", "segmentation", "output",
                   "total_before_manifest_finalize")
    order_ok = len(lines) == 13
    if order_ok:
        centers = [(b["bbox_xyxy"][0] + b["bbox_xyxy"][2]) / 2 for b in lines]
        order_ok = centers[0] > 450 and centers[0] < 750 and all(x < 600 for x in centers[1:7]) and \
                   all(x >= 600 for x in centers[7:13])
    retained = 0
    for side, box in labels:
        x0, y0, x1, y1 = box
        expected_center_x, expected_center_y = (x0 + x1) / 2, (y0 + y1) / 2
        candidates = [line for line in lines
                      if abs((line["bbox_xyxy"][1] + line["bbox_xyxy"][3]) / 2 - expected_center_y) < 50 and
                      (side == "title" or (((line["bbox_xyxy"][0] + line["bbox_xyxy"][2]) / 2 < 600) == (side == "left")))]
        retained += any(line["source_crop_bbox_xyxy"][0] <= x0 and line["source_crop_bbox_xyxy"][1] <= y0 and
                        line["source_crop_bbox_xyxy"][2] >= x1 and line["source_crop_bbox_xyxy"][3] >= y1
                        for line in candidates)
    checks = {"spanning_title_preserved": bool(lines) and lines[0]["bbox_xyxy"][0] < 300 < lines[0]["bbox_xyxy"][2],
              "curved_separator_reported": geometry,
              "thirteen_blocks_lines_detected": len(lines) == 13,
              "column_major_order_after_spanning_block": order_ok,
              "all_independent_line_ink_boxes_inside_crops": retained == len(labels),
              "stage_runtime_reported": all(isinstance(timing.get(key), (int, float)) and timing[key] >= 0
                                             for key in timing_keys),
              "region_relations_emitted": all("reading_order_index" in r and "parent_id" in r for r in regions)}
    return {"case": "layout_curved_partial_gutter", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2),
            "line_count": len(lines), "region_count": len(regions), "retained_source_boxes": retained}


def run_baseline_fallback_case(binary, work):
    source, out = work / "layout_curved_gutter.png", work / "out_layout_curved_baseline"
    make_curved_gutter_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out),
                           "--layout", "baseline", "--no-deskew"],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        return {"case": "layout_baseline_fallback", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    regions = manifest["layout"]["regions"]
    checks = {"baseline_mode_reported": manifest["layout"]["mode"] == "baseline",
              "curved_edges_disabled": all(r.get("geometry") != "piecewise_vertical_boundary" for r in regions),
              "legacy_rows_remain_usable": 7 <= len(manifest["lines"]) <= 13}
    return {"case": "layout_baseline_fallback", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2),
            "line_count": len(manifest["lines"]), "region_count": len(regions)}


def make_curved_baseline_fixture(path):
    """Word-level glyph placements follow a slowly undulating writing baseline."""
    im = Image.new("L", (1200, 1050), 250)
    draw = ImageDraw.Draw(im)
    font = _font(25)
    rng = random.Random(4107)
    labels = []
    words = ("Curved", "writing", "baseline", "keeps", "every", "word", "intact")
    for row, base in enumerate((130, 300, 470, 640, 810)):
        x = 100
        row_boxes = []
        for i, word in enumerate(words):
            y = base + round(17 * math.sin((x - 100) / 175 + row * 0.31)) + rng.randint(-1, 1)
            draw.text((x, y), word, font=font, fill=12)
            box = draw.textbbox((x, y), word, font=font)
            rendered = im.crop(box)
            actual = ImageChops.difference(rendered, Image.new("L", rendered.size, 255)).getbbox()
            if actual:
                box = (box[0] + actual[0], box[1] + actual[1],
                       box[0] + actual[2], box[1] + actual[3])
            row_boxes.append(box)
            x += int(draw.textlength(word, font=font)) + 28
        labels.append((min(b[0] for b in row_boxes), min(b[1] for b in row_boxes),
                       max(b[2] for b in row_boxes), max(b[3] for b in row_boxes)))
    im.save(path, "PNG")
    return labels


def run_curved_baseline_case(binary, work):
    source, out = work / "curved_baselines.png", work / "out_curved_baselines"
    labels = make_curved_baseline_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out), "--no-deskew"],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        return {"case": "curved_variable_baselines", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    retained = sum(any(line["source_crop_bbox_xyxy"][0] <= x0 and line["source_crop_bbox_xyxy"][1] <= y0 and
                       line["source_crop_bbox_xyxy"][2] >= x1 and line["source_crop_bbox_xyxy"][3] >= y1
                       and abs((line["bbox_xyxy"][1] + line["bbox_xyxy"][3]) / 2 - (y0 + y1) / 2) < 45
                       for line in lines) for x0, y0, x1, y1 in labels)
    checks = {"five_wavy_lines_detected": len(lines) == 5,
              "all_independent_word_ink_envelopes_retained": retained == len(labels),
              "reading_order_monotonic": [b["bbox_xyxy"][1] for b in lines] == sorted(b["bbox_xyxy"][1] for b in lines)}
    return {"case": "curved_variable_baselines", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2),
            "line_count": len(lines), "retained_source_boxes": retained}


def make_workbook_fixture(path):
    """Mixed printed questions and hand-drawn-like answer strokes in ruled rows."""
    im = Image.new("L", (1200, 1100), 255)
    draw = ImageDraw.Draw(im)
    printed = _font(27)
    rng = random.Random(721)
    labels = []
    answer_ink = []
    for row, y in enumerate((150, 430, 710)):
        text = f"Exercise {row + 1}: solve the problem and show your work"
        draw.text((95, y), text, font=printed, fill=12)
        labels.append(draw.textbbox((95, y), text, font=printed))
        # A variable, jittered stroke sequence stands in for handwriting-like geometry.
        x0 = 140
        for i in range(9):
            x = x0 + i * 78
            base = y + 72 + rng.randint(-4, 4)
            height = rng.randint(18, 32)
            points = (x, base - height, x + rng.randint(9, 18), base - height // 2,
                      x + rng.randint(18, 30), base)
            stroke_width = rng.randint(2, 4)
            draw.line(points, fill=22, width=stroke_width)
            candidate = (x - 4, base - height - 4, x + 35, base + 5)
            ink_crop = im.crop(candidate)
            actual = ImageChops.difference(ink_crop, Image.new("L", ink_crop.size, 255)).getbbox()
            if actual:
                answer_ink.append((candidate[0] + actual[0], candidate[1] + actual[1],
                                   candidate[0] + actual[2], candidate[1] + actual[3]))
            if i % 2 == 0:
                ellipse = (x + 24, base - height, x + 42, base - 4)
                draw.ellipse(ellipse, outline=22, width=2)
                ink_crop = im.crop((ellipse[0] - 3, ellipse[1] - 3, ellipse[2] + 4, ellipse[3] + 4))
                actual = ImageChops.difference(ink_crop, Image.new("L", ink_crop.size, 255)).getbbox()
                if actual:
                    answer_ink.append((ellipse[0] - 3 + actual[0], ellipse[1] - 3 + actual[1],
                                       ellipse[0] - 3 + actual[2], ellipse[1] - 3 + actual[3]))
        # Thin answer/table rules are deliberately distinct from the ink words.
        draw.line((75, y + 112, 1125, y + 112), fill=35, width=2)
    draw.line((75, 125, 75, 865), fill=50, width=2)
    draw.line((1125, 125, 1125, 865), fill=50, width=2)
    im.save(path, "PNG")
    return {"prompts": labels, "answer_ink": answer_ink}


def run_workbook_case(binary, work):
    source, out = work / "exercise_workbook.png", work / "out_exercise_workbook"
    labels = make_workbook_fixture(source)
    started = time.perf_counter()
    proc = subprocess.run([str(binary), "--input", str(source), "--output", str(out)],
                          text=True, capture_output=True, check=False)
    elapsed_ms = (time.perf_counter() - started) * 1000
    if proc.returncode != 0:
        return {"case": "workbook_mixed_content", "pass": False,
                "error": proc.stderr.strip(), "elapsed_ms": round(elapsed_ms, 2)}
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    lines = manifest["lines"]
    prompt_covered = sum(any(line["source_crop_bbox_xyxy"][0] <= box[0] and line["source_crop_bbox_xyxy"][1] <= box[1] and
                             line["source_crop_bbox_xyxy"][2] >= box[2] and line["source_crop_bbox_xyxy"][3] >= box[3]
                             for line in lines) for box in labels["prompts"])
    answer_ink_covered = sum(any(line["source_crop_bbox_xyxy"][0] <= box[0] and line["source_crop_bbox_xyxy"][1] <= box[1] and
                                 line["source_crop_bbox_xyxy"][2] >= box[2] and line["source_crop_bbox_xyxy"][3] >= box[3]
                                 for line in lines) for box in labels["answer_ink"])
    rule_crops = [line for line in lines if line["bbox_xyxy"][2] - line["bbox_xyxy"][0] > 300 and
                  line["bbox_xyxy"][3] - line["bbox_xyxy"][1] <= 7]
    checks = {"all_three_printed_prompts_preserved": prompt_covered == 3,
              "all_handwriting_like_answer_strokes_retained": answer_ink_covered == len(labels["answer_ink"]),
              "work_rows_not_collapsed_into_one_page_crop": 3 <= len(lines) <= 9,
              "thin_answer_rules_not_emitted_as_text": not rule_crops,
              "crop_and_region_relationships_reported": all("region" in line and "reading_order_index" in line for line in lines),
              "review_candidates_still_visible": isinstance(manifest["excluded"], list)}
    return {"case": "workbook_mixed_content", "pass": all(checks.values()),
            "checks": checks, "elapsed_ms": round(elapsed_ms, 2),
            "line_count": len(lines), "excluded_count": len(manifest["excluded"]),
            "answer_ink_groups": len(labels["answer_ink"]), "retained_answer_ink_groups": answer_ink_covered}


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
        results.append(run_row_noise_case(args.binary.resolve(), work))
        results.append(run_dense_vertical_case(args.binary.resolve(), work))
        results.append(run_vertical_font_scale_case(args.binary.resolve(), work))
        results.append(run_faint_edge_recovery_case(args.binary.resolve(), work))
        results.append(run_edge_clipping_case(args.binary.resolve(), work))
        results.append(run_heading_underline_case(args.binary.resolve(), work))
        results.extend(run_cli_cases(args.binary.resolve(), work))
        results.extend((run_layout_case(args.binary.resolve(), work, "spread"),
                       run_layout_case(args.binary.resolve(), work, "columns"),
                       run_layout_case(args.binary.resolve(), work, "columns", "rtl"),
                       run_layout_case(args.binary.resolve(), work, "newspaper"),
                       run_gutter_margin_case(args.binary.resolve(), work),
                       run_custom_region_case(args.binary.resolve(), work),
                       run_single_layout_case(args.binary.resolve(), work),
                       run_bad_custom_region_case(args.binary.resolve(), work),
                       run_curved_layout_case(args.binary.resolve(), work),
                       run_baseline_fallback_case(args.binary.resolve(), work),
                       run_curved_baseline_case(args.binary.resolve(), work),
                       run_workbook_case(args.binary.resolve(), work)))
    else:
        with tempfile.TemporaryDirectory(prefix="lineforge-bench-") as tmp:
            work = Path(tmp)
            results = [run_case(args.binary.resolve(), work, mode, i) for mode in modes for i in range(3)]
            results.extend(run_blank_case(args.binary.resolve(), work, dark=dark) for dark in (False, True))
            results.append(run_case(args.binary.resolve(), work, "large", 0, size=(3600, 4320)))
            results.append(run_row_noise_case(args.binary.resolve(), work))
            results.append(run_dense_vertical_case(args.binary.resolve(), work))
            results.append(run_vertical_font_scale_case(args.binary.resolve(), work))
            results.append(run_faint_edge_recovery_case(args.binary.resolve(), work))
            results.append(run_edge_clipping_case(args.binary.resolve(), work))
            results.append(run_heading_underline_case(args.binary.resolve(), work))
            results.extend(run_cli_cases(args.binary.resolve(), work))
            results.extend((run_layout_case(args.binary.resolve(), work, "spread"),
                           run_layout_case(args.binary.resolve(), work, "columns"),
                           run_layout_case(args.binary.resolve(), work, "columns", "rtl"),
                           run_layout_case(args.binary.resolve(), work, "newspaper"),
                           run_gutter_margin_case(args.binary.resolve(), work),
                           run_custom_region_case(args.binary.resolve(), work),
                           run_single_layout_case(args.binary.resolve(), work),
                           run_bad_custom_region_case(args.binary.resolve(), work),
                           run_curved_layout_case(args.binary.resolve(), work),
                           run_baseline_fallback_case(args.binary.resolve(), work),
                           run_curved_baseline_case(args.binary.resolve(), work),
                           run_workbook_case(args.binary.resolve(), work)))
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

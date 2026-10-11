#!/usr/bin/env python3
"""High-crop-count output regression tests for bounded parallel PNG saving."""
import argparse
import json
import shutil
import subprocess
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont


def make_fixture(path: Path, line_count: int = 180) -> None:
    width, pitch, top = 1000, 22, 16
    height = top + line_count * pitch + 20
    image = Image.new("L", (width, height), 255)
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 14)
    except OSError:
        font = ImageFont.load_default()
    phrase = "Batch PNG output throughput fixture 0123456789. "
    for row in range(line_count):
        text = phrase * 5
        while text and draw.textlength(text, font=font) > width - 100:
            text = text[:-1]
        draw.text((50, top + row * pitch), text, fill=12, font=font)
    image.save(path, "PNG")


def run(binary: Path, source: Path, output: Path) -> subprocess.CompletedProcess:
    return subprocess.run([str(binary), "--input", str(source), "--output", str(output)],
                          text=True, capture_output=True, check=False)


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--binary", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    args = parser.parse_args()
    work = args.work_dir
    if work.exists():
        shutil.rmtree(work)
    work.mkdir(parents=True)
    source = work / "high_crop_count.png"
    first, second = work / "out_first", work / "out_second"
    make_fixture(source)

    proc = run(args.binary.resolve(), source.resolve(), first.resolve())
    require(proc.returncode == 0, f"first batch run failed: {proc.stderr.strip()}")
    proc2 = run(args.binary.resolve(), source.resolve(), second.resolve())
    require(proc2.returncode == 0, f"second batch run failed: {proc2.stderr.strip()}")

    manifest_a = json.loads((first / "manifest.json").read_text(encoding="utf-8"))
    manifest_b = json.loads((second / "manifest.json").read_text(encoding="utf-8"))
    lines_a, lines_b = manifest_a["lines"], manifest_b["lines"]
    require(len(lines_a) >= 120, f"stress fixture yielded only {len(lines_a)} line crops")
    expected_names = [f"line_{index:04d}.png" for index in range(1, len(lines_a) + 1)]
    require([line["file"] for line in lines_a] == expected_names,
            "manifest filenames are not contiguous and reading-order stable")
    require([line["file"] for line in lines_b] == expected_names,
            "repeated run changed crop filenames or order")
    require(sorted(path.name for path in first.glob("line_*.png")) == expected_names,
            "output crop count differs from the manifest")

    semantic_a, semantic_b = dict(manifest_a), dict(manifest_b)
    semantic_a.pop("timing_ms", None)
    semantic_b.pop("timing_ms", None)
    require(semantic_a == semantic_b, "manifest content changed across identical runs")
    for name in expected_names:
        with Image.open(first / name) as left, Image.open(second / name) as right:
            left.load()
            right.load()
            require(left.mode == right.mode and left.size == right.size and
                    ImageChops.difference(left, right).getbbox() is None,
                    f"crop pixels differ across identical runs: {name}")

    (first / "stale.txt").write_text("old result", encoding="utf-8")
    proc3 = run(args.binary.resolve(), source.resolve(), first.resolve())
    require(proc3.returncode == 0, f"output replacement failed: {proc3.stderr.strip()}")
    require(not (first / "stale.txt").exists(), "successful replacement retained stale output files")

    blocked = work / "not_a_directory"
    sentinel = b"keep this existing file intact"
    blocked.write_bytes(sentinel)
    failure = run(args.binary.resolve(), source.resolve(), blocked.resolve())
    require(failure.returncode == 1, "publication to a non-directory did not report failure")
    require("--output exists and is not a directory" in failure.stderr,
            f"failure message did not explain the output problem: {failure.stderr.strip()}")
    require(blocked.read_bytes() == sentinel, "failed run changed the pre-existing output path")
    require(not list(work.glob("not_a_directory.linecrop-stage-*")),
            "failed run left a hidden partial staging directory")

    print(json.dumps({"pass": True, "fixture_lines": 180, "emitted_crops": len(lines_a),
                      "deterministic_runs": 2, "failure_cleanup": True}, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print(f"batch_output.py: {error}", flush=True)
        raise SystemExit(1)

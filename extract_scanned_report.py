#!/usr/bin/env python3
"""Extract text and candidate tables from a scanned report PDF.

The pipeline renders every page, runs local Tesseract OCR, preserves word
coordinates, reconstructs conservative table candidates, and splits pages into
train/validation manifests. It never overwrites the input PDF.
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import shutil
import subprocess
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def command(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"Required command not found: {name}")
    return found


def render_pages(pdf: Path, image_dir: Path, dpi: int) -> list[Path]:
    image_dir.mkdir(parents=True, exist_ok=True)
    prefix = image_dir / "page"
    subprocess.run(
        [command("pdftoppm"), "-r", str(dpi), "-png", str(pdf), str(prefix)],
        check=True,
    )
    return sorted(image_dir.glob("page-*.png"))


def ocr_image(
    image: Path,
    psm: int = 6,
    *,
    language: str = "rus+eng",
    whitelist: str | None = None,
) -> list[dict[str, Any]]:
    """Run Tesseract TSV OCR and return normalised bottom-left boxes."""
    arguments = [
        command("tesseract"), str(image), "stdout", "-l", language,
        "--psm", str(psm),
    ]
    if whitelist:
        arguments += ["-c", f"tessedit_char_whitelist={whitelist}"]
    arguments.append("tsv")
    completed = subprocess.run(
        arguments,
        check=True,
        capture_output=True,
        text=True,
    )
    try:
        from PIL import Image
    except ImportError as error:
        raise RuntimeError("Pillow is required; install it with: python3 -m pip install Pillow") from error
    width, height = Image.open(image).size
    by_line: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    # OCR text can legitimately contain a quote. TSV does not quote/escape its
    # final text column, so treat every quote as ordinary text rather than
    # allowing csv to merge subsequent TSV rows into one corrupted token.
    reader = csv.DictReader(completed.stdout.splitlines(), delimiter="\t", quoting=csv.QUOTE_NONE)
    for row in reader:
        text = (row.get("text") or "").strip()
        if not text or row.get("level") != "5":
            continue
        x, y, w, h = (float(row[name]) for name in ("left", "top", "width", "height"))
        confidence = max(0.0, float(row.get("conf", 0))) / 100
        word = {
            "text": text,
            "confidence": confidence,
            "box": {"x": x / width, "y": 1 - (y + h) / height, "width": w / width, "height": h / height},
        }
        line_key = tuple(row[name] for name in ("block_num", "par_num", "line_num", "page_num"))
        by_line[line_key].append(word)
    lines: list[dict[str, Any]] = []
    for words in by_line.values():
        words.sort(key=lambda word: word["box"]["x"])
        left = min(word["box"]["x"] for word in words)
        bottom = min(word["box"]["y"] for word in words)
        right = max(word["box"]["x"] + word["box"]["width"] for word in words)
        top = max(word["box"]["y"] + word["box"]["height"] for word in words)
        lines.append({
            "text": " ".join(word["text"] for word in words),
            "confidence": sum(word["confidence"] for word in words) / len(words),
            "box": {"x": left, "y": bottom, "width": right - left, "height": top - bottom},
            "words": words,
        })
    lines.sort(key=lambda line: (-line["box"]["y"], line["box"]["x"]))
    return lines


def run_ocr(image: Path, raw_dir: Path) -> list[dict[str, Any]]:
    """Write the original OCR result without mixing it with verification data."""
    raw_dir.mkdir(parents=True, exist_ok=True)
    destination = raw_dir / f"{image.stem}.json"
    lines = ocr_image(image, psm=6)
    destination.write_text(json.dumps(lines, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return lines


def group_rows(lines: list[dict[str, Any]], tolerance: float = 0.015) -> list[list[dict[str, Any]]]:
    """Group OCR words into visual rows, ordered top-to-bottom."""
    words = [word for line in lines for word in line.get("words", [])]
    words.sort(key=lambda w: -(w["box"]["y"] + w["box"]["height"] / 2))
    rows: list[list[dict[str, Any]]] = []
    centers: list[float] = []
    for word in words:
        cy = word["box"]["y"] + word["box"]["height"] / 2
        if rows and abs(cy - centers[-1]) <= tolerance:
            rows[-1].append(word)
            centers[-1] = sum(w["box"]["y"] + w["box"]["height"] / 2 for w in rows[-1]) / len(rows[-1])
        else:
            rows.append([word])
            centers.append(cy)
    for row in rows:
        row.sort(key=lambda w: w["box"]["x"])
    return rows


def has_columns(row: list[dict[str, Any]], min_gap: float = 0.025) -> bool:
    """A table row has at least two substantial spaces between word groups."""
    if len(row) < 3:
        return False
    gaps = [
        next_word["box"]["x"] - (word["box"]["x"] + word["box"]["width"])
        for word, next_word in zip(row, row[1:])
    ]
    return sum(gap >= min_gap for gap in gaps) >= 2


def clusters(values: list[float], tolerance: float = 0.045) -> list[float]:
    """Merge similar x positions into stable, left-aligned column anchors."""
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - sum(groups[-1]) / len(groups[-1]) > tolerance:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def extract_table_runs(lines: list[dict[str, Any]]) -> list[list[list[str]]]:
    """Extract conservative text tables from aligned OCR words.

    This targets typed tables. Ruled tables, merged cells, handwriting and very
    faint cells are deliberately left as review candidates instead of inventing
    values. Every OCR word remains available in raw_ocr for manual correction.
    """
    rows = group_rows(lines)
    runs: list[list[list[dict[str, Any]]]] = []
    current: list[list[dict[str, Any]]] = []
    for row in rows:
        if has_columns(row):
            current.append(row)
        else:
            if len(current) >= 3:
                runs.append(current)
            current = []
    if len(current) >= 3:
        runs.append(current)

    extracted: list[list[list[str]]] = []
    for run in runs:
        anchors = clusters([word["box"]["x"] for row in run for word in row])
        if len(anchors) < 3:
            continue
        table: list[list[str]] = []
        for row in run:
            cells = ["" for _ in anchors]
            for word in row:
                column = min(range(len(anchors)), key=lambda i: abs(word["box"]["x"] - anchors[i]))
                cells[column] = (cells[column] + " " + word["text"]).strip()
            table.append(cells)
        nonempty = sum(bool(cell) for row in table for cell in row)
        if nonempty >= len(table) * 2:
            extracted.append(table)
    return extracted


def write_tables(page_number: int, lines: list[dict[str, Any]], tables_dir: Path) -> list[str]:
    tables_dir.mkdir(parents=True, exist_ok=True)
    paths: list[str] = []
    for index, table in enumerate(extract_table_runs(lines), start=1):
        path = tables_dir / f"page-{page_number:03d}-table-{index:02d}.csv"
        with path.open("w", newline="", encoding="utf-8") as stream:
            csv.writer(stream).writerows(table)
        paths.append(str(path))
    return paths


def page_split(page_count: int, validation_ratio: float, seed: int) -> set[int]:
    validation_count = max(1, round(page_count * validation_ratio)) if page_count > 1 else 0
    indices = list(range(1, page_count + 1))
    random.Random(seed).shuffle(indices)
    return set(indices[:validation_count])


def main() -> None:
    parser = argparse.ArgumentParser(description="OCR a scanned report and create ML-ready manifests.")
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("--output", type=Path, default=Path("output/scanned_report"))
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--validation-ratio", type=float, default=0.20)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reuse-images", action="store_true", help="Reuse existing rendered page PNGs in --output/images.")
    args = parser.parse_args()
    if not args.input_pdf.is_file():
        parser.error(f"PDF not found: {args.input_pdf}")
    if not 0 < args.validation_ratio < 1:
        parser.error("--validation-ratio must be between 0 and 1")

    output = args.output.resolve()
    existing_images = sorted((output / "images").glob("page-*.png"))
    if args.reuse_images:
        if not existing_images:
            parser.error("--reuse-images was requested but no images exist in --output/images")
        images = existing_images
    else:
        images = render_pages(args.input_pdf.resolve(), output / "images", args.dpi)
    # Table CSVs are derived artifacts; remove only stale files created by a
    # previous run so the manifest and directory always agree.
    for stale_table in (output / "tables").glob("*.csv"):
        stale_table.unlink()
    validation_pages = page_split(len(images), args.validation_ratio, args.seed)
    manifests: dict[str, list[dict[str, Any]]] = {"train": [], "validation": []}
    all_records: list[dict[str, Any]] = []

    for page_number, image in enumerate(images, start=1):
        lines = run_ocr(image, output / "raw_ocr")
        split = "validation" if page_number in validation_pages else "train"
        record = {
            "document_id": args.input_pdf.stem,
            "page": page_number,
            "split": split,
            "image": str(image),
            "ocr_json": str(output / "raw_ocr" / f"{image.stem}.json"),
            "text": "\n".join(line["text"] for line in lines),
            "tables": write_tables(page_number, lines, output / "tables"),
        }
        manifests[split].append(record)
        all_records.append(record)
        print(f"OCR page {page_number}/{len(images)} -> {split}")

    for name, records in manifests.items():
        path = output / "splits" / f"{name}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records), encoding="utf-8")
    (output / "pages.jsonl").write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in all_records), encoding="utf-8"
    )
    summary = {
        "source_pdf": str(args.input_pdf.resolve()),
        "page_count": len(images),
        "split_strategy": "random page-level split; seed stored below",
        "seed": args.seed,
        "train_pages": [r["page"] for r in manifests["train"]],
        "validation_pages": [r["page"] for r in manifests["validation"]],
        "table_files": [path for record in all_records for path in record["tables"]],
        "review_note": "OCR output is a draft. Correct validation transcriptions and table CSV cells before measuring or training a model.",
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Done: {output}")


if __name__ == "__main__":
    main()

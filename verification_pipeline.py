#!/usr/bin/env python3
"""Verification, silver-label, ground-truth, and evaluation stages for OCR output.

This module does not overwrite ``raw_ocr``. It creates a separate preprocessed
OCR candidates, conservative automatic labels with abstention, an optional
review queue, fixed-geometry cell records for the two known table pages, and
evaluation artifacts.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from PIL import Image, ImageEnhance, ImageFilter, ImageOps

from extract_scanned_report import ocr_image


TABLE_SPECS: dict[int, dict[str, list[float]]] = {
    # Coordinates are normalized, origin bottom-left. Edges are deliberately
    # explicit because these two archival tables use merged headers and sparse
    # ruling that generic whitespace clustering cannot preserve reliably.
    11: {
        "x_edges": [0.135, 0.285, 0.385, 0.585, 0.690, 0.775, 0.985],
        "y_edges": [0.790, 0.735, 0.690, 0.661, 0.634, 0.606, 0.580, 0.555,
                    0.526, 0.499, 0.475, 0.449, 0.422, 0.395, 0.369, 0.343,
                    0.313, 0.280],
    },
    13: {
        "x_edges": [0.225, 0.292, 0.365, 0.438, 0.510, 0.580, 0.650, 0.735, 0.815, 0.965],
        "y_edges": [0.875, 0.815, 0.788, 0.761, 0.734, 0.706, 0.679,
                    0.654, 0.628, 0.603, 0.578, 0.554, 0.525],
    },
}

# Include this in cell OCR cache paths so geometry changes cannot accidentally
# reuse OCR generated for older, differently cropped cells.
TABLE_GEOMETRY_VERSION = "v3"

CYRILLIC_RE = re.compile(r"[А-Яа-яЁё]")
LATIN_RE = re.compile(r"[A-Za-z]")
NUMERIC_RE = re.compile(r"^[+-]?\d+(?:[,.]\d+)?$")
ALLOWED_RE = re.compile(r"^[\w\s.,:;!?()\[\]{}%+/\\№#'\"=<>|*\-–—]+$", re.UNICODE)
KNOWN_FORMULAS = {
    "SIO2": "SiO2", "TIO2": "TiO2", "AL2O3": "Al2O3", "FE2O3": "Fe2O3",
    "FEO": "FeO", "MNO": "MnO", "MGO": "MgO", "CAO": "CaO",
    "NA2O": "Na2O", "K2O": "K2O", "P2O5": "P2O5", "H2O": "H2O",
    "PB": "Pb", "CU": "Cu", "ZN": "Zn", "S": "S",
}
TABLE_FORMULA_ROWS: dict[int, list[str | None]] = {
    # Row 0/1 are headers on page 11; row 0 is the header on page 13.
    11: [
        "SiO2", "TiO2", "Al2O3", "Fe2O3", "FeO", "MnO", "MgO", "CaO",
        "Na2O", "K2O", "Pb", "Cu", "Zn", None, "H2O",
    ],
    13: [
        "SiO2", "Al2O3", "Fe2O3", "TiO2", "CaO", "MgO", None,
        "P2O5", "S", None, "H2O",
    ],
}
CYRILLIC_LOOKALIKES = str.maketrans({
    "А": "A", "а": "a", "В": "B", "в": "b", "Е": "E", "е": "e",
    "К": "K", "к": "k", "М": "M", "м": "m", "Н": "H", "н": "h",
    "О": "O", "о": "o", "Р": "P", "р": "p", "С": "C", "с": "c",
    "Т": "T", "т": "t", "Х": "X", "х": "x",
})


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records),
        encoding="utf-8",
    )


def preprocess(source: Path, destination: Path) -> Path:
    """Apply conservative, deterministic cleanup without changing dimensions."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    with Image.open(source) as image:
        cleaned = ImageOps.grayscale(image)
        cleaned = ImageOps.autocontrast(cleaned, cutoff=1)
        cleaned = ImageEnhance.Contrast(cleaned).enhance(1.12)
        cleaned = cleaned.filter(ImageFilter.UnsharpMask(radius=1.0, percent=120, threshold=4))
        cleaned.save(destination, optimize=False)
    return destination


def normalized_formula(value: str) -> str | None:
    compact = re.sub(r"[^0-9A-Za-zА-Яа-я]", "", value).translate(CYRILLIC_LOOKALIKES)
    return KNOWN_FORMULAS.get(compact.upper())


def normalize_value(value: str) -> str:
    """Return a proposed normalized value; the raw OCR remains untouched."""
    numero_placeholder = "\ue000"
    normalized = unicodedata.normalize("NFKC", value.replace("№", numero_placeholder))
    normalized = normalized.replace(numero_placeholder, "№")
    normalized = re.sub(r"\s+", " ", normalized).strip()
    formula = normalized_formula(normalized)
    if formula:
        return formula
    if re.fullmatch(r"[+-]?\d+\.\d+", normalized):
        return normalized.replace(".", ",")
    return normalized


def validation_reasons(
    raw: str,
    confidence: float,
    alternative: str = "",
    alternative_confidence: float = 0.0,
    *,
    table_cell: bool = False,
) -> list[str]:
    reasons: list[str] = []
    compact = raw.strip()
    if not compact and alternative:
        reasons.append("missing_raw_ocr")
    elif compact and confidence < (0.55 if table_cell else 0.45):
        reasons.append("low_confidence")
    if alternative and normalize_value(alternative) != normalize_value(raw):
        if table_cell or alternative_confidence >= max(0.72, confidence + 0.10):
            reasons.append("ocr_engines_disagree")
    if CYRILLIC_RE.search(compact) and LATIN_RE.search(compact):
        reasons.append("cyrillic_latin_mix")
    if compact and not ALLOWED_RE.fullmatch(compact):
        reasons.append("unexpected_characters")
    if re.fullmatch(r"[+-]?\d+\.\d+", compact):
        reasons.append("decimal_point_in_russian_number")
    if table_cell and any(ch.isdigit() for ch in compact):
        token = compact.strip(" |()[]{}:;")
        if not NUMERIC_RE.fullmatch(token) and normalized_formula(token) is None:
            digit_ratio = sum(ch.isdigit() for ch in token) / max(1, len(token))
            if digit_ratio >= 0.25:
                reasons.append("malformed_table_value")
    if compact and len(compact) >= 3:
        unusual = sum(not (ch.isalnum() or ch in "-.,()/% ") for ch in compact) / len(compact)
        if unusual > 0.25:
            reasons.append("ocr_noise")
    return list(dict.fromkeys(reasons))


def box_to_pixels(box: dict[str, float], width: int, height: int) -> tuple[int, int, int, int]:
    left = round(box["x"] * width)
    right = round((box["x"] + box["width"]) * width)
    top = round((1 - box["y"] - box["height"]) * height)
    bottom = round((1 - box["y"]) * height)
    return left, top, right, bottom


def make_crop(image: Image.Image, box: dict[str, float], destination: Path, padding: int = 24) -> Path:
    left, top, right, bottom = box_to_pixels(box, image.width, image.height)
    left, top = max(0, left - padding), max(0, top - padding)
    right, bottom = min(image.width, right + padding), min(image.height, bottom + padding)
    crop = image.crop((left, top, right, bottom))
    crop = ImageOps.autocontrast(ImageOps.grayscale(crop), cutoff=1)
    destination.parent.mkdir(parents=True, exist_ok=True)
    crop.save(destination)
    return destination


def flatten_words(lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [word for line in lines for word in line.get("words", [])]


def intersection_score(first: dict[str, float], second: dict[str, float]) -> float:
    left = max(first["x"], second["x"])
    right = min(first["x"] + first["width"], second["x"] + second["width"])
    bottom = max(first["y"], second["y"])
    top = min(first["y"] + first["height"], second["y"] + second["height"])
    intersection = max(0.0, right - left) * max(0.0, top - bottom)
    first_area = max(1e-9, first["width"] * first["height"])
    return intersection / first_area


def alternative_for(word: dict[str, Any], alternatives: list[dict[str, Any]]) -> dict[str, Any] | None:
    candidates = [(intersection_score(word["box"], candidate["box"]), candidate) for candidate in alternatives]
    score, candidate = max(candidates, default=(0.0, None), key=lambda pair: pair[0])
    return candidate if candidate is not None and score >= 0.25 else None


def inside_known_table(page: int, word: dict[str, Any]) -> bool:
    spec = TABLE_SPECS.get(page)
    if not spec:
        return False
    center_x = word["box"]["x"] + word["box"]["width"] / 2
    center_y = word["box"]["y"] + word["box"]["height"] / 2
    return spec["x_edges"][0] <= center_x <= spec["x_edges"][-1] and spec["y_edges"][-1] <= center_y <= spec["y_edges"][0]


def record_status(confidence: float, raw: str, alternative: str, reasons: list[str]) -> str:
    if reasons:
        return "needs_review"
    if confidence >= 0.90 and (not alternative or normalize_value(raw) == normalize_value(alternative)):
        return "auto_verified"
    return "raw_ocr"


def review_priority(reasons: list[str], item_type: str) -> int:
    severe = {
        "missing_raw_ocr", "ocr_engines_disagree", "cyrillic_latin_mix",
        "unexpected_characters", "malformed_table_value", "ocr_noise",
    }
    if any(reason in severe for reason in reasons):
        return 1
    return 2 if item_type == "table_cell" else 3


def expected_formula(page: int, row: int, column: int) -> str | None:
    if page == 11 and row >= 2 and column in {0, 3}:
        index = row - 2
    elif page == 13 and row >= 1 and column == 0:
        index = row - 1
    else:
        return None
    formulas = TABLE_FORMULA_ROWS[page]
    return formulas[index] if 0 <= index < len(formulas) else None


def is_numeric_cell(page: int, row: int, column: int) -> bool:
    if page == 11:
        return row >= 2 and column in {1, 4}
    if page == 13:
        return row >= 1 and 1 <= column <= 7
    return False


def numeric_candidate(value: str) -> str | None:
    """Normalize one unambiguous OCR number using Russian decimal commas."""
    compact = unicodedata.normalize("NFKC", value).strip()
    compact = compact.strip(" |()[]{}:;'‘’“”")
    compact = re.sub(r"\s+", "", compact)
    if not any(character.isdigit() for character in compact):
        return None
    compact = compact.translate(str.maketrans({
        "O": "0", "o": "0", "О": "0", "о": "0",
        "I": "1", "l": "1", "І": "1", "|": "1",
    }))
    if re.fullmatch(r"[+-]?\d+(?:[,.]\d+)?", compact):
        return compact.replace(".", ",")
    matches = list(re.finditer(r"[+-]?\d+(?:[,.]\d+)?", compact))
    if len(matches) != 1:
        return None
    match = matches[0]
    leftover = compact[:match.start()] + compact[match.end():]
    if leftover and not re.fullmatch(r"[-.,]+", leftover):
        return None
    return match.group().replace(".", ",")


def compact_formula_candidate(value: str) -> str:
    return re.sub(r"[^0-9A-Za-zА-Яа-я]", "", value).translate(CYRILLIC_LOOKALIKES).upper()


def average_confidence(words: list[dict[str, Any]]) -> float:
    return sum(word["confidence"] for word in words) / len(words) if words else 0.0


def text_from_words(words: list[dict[str, Any]]) -> str:
    return " ".join(word["text"] for word in words).strip()


def candidate_key(record: dict[str, Any], value: str) -> str:
    if is_numeric_cell(record["page"], record.get("row") or 0, record.get("column") or 0):
        special = normalize_value(value).strip(" |.").casefold()
        if special in {"-", "–", "—"}:
            return "number:-"
        if special in {"след", "следы", "нет"}:
            return f"number:{special}"
        numeric = numeric_candidate(value)
        # The measured table values are decimal-comma quantities. Requiring the
        # expected shape prevents two OCR modes from agreeing on a shared bad
        # integer after a decimal separator was lost.
        if numeric is not None and re.fullmatch(r"[+-]?\d{1,3},\d{1,3}", numeric):
            return f"number:{numeric}"
        return ""
    formula = normalized_formula(value)
    if formula:
        return f"formula:{formula.upper()}"
    normalized = normalize_value(value).strip(" |").casefold()
    return f"text:{normalized}" if normalized else ""


def decide_silver(record: dict[str, Any], candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Choose a conservative silver value or abstain, preserving all evidence."""
    usable = []
    raw_length = len(record.get("raw_ocr", ""))
    for candidate in candidates:
        value = candidate.get("value", "").strip()
        if not value:
            continue
        if record["type"] == "word" and raw_length and len(value) > max(raw_length * 3, raw_length + 12):
            continue
        key = candidate_key(record, value)
        if key:
            usable.append({**candidate, "key": key})

    formula = expected_formula(record["page"], record.get("row") or 0, record.get("column") or 0)
    if formula:
        # Fixed row labels are table structure, not inferred measurements. The
        # schema decision is explicit and never overwrites raw OCR.
        return {
            "silver_status": "silver_rule_corrected",
            "silver_value": formula,
            "silver_confidence": 0.9,
            "acceptance_reason": "expected_formula_from_fixed_table_schema",
            "consensus_sources": [],
            "candidates": candidates,
        }

    groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in usable:
        groups[candidate["key"]].append(candidate)
    agreeing = [
        group for group in groups.values()
        if len({candidate["source"] for candidate in group}) >= 2
    ]
    if agreeing:
        group = max(
            agreeing,
            key=lambda items: (len({item["source"] for item in items}), sum(item["confidence"] for item in items)),
        )
        best = max(group, key=lambda item: item["confidence"])
        group_confidence = sum(item["confidence"] for item in group) / len(group)
        is_table = record["type"] == "table_cell"
        is_numeric = is_numeric_cell(
            record["page"], record.get("row") or 0, record.get("column") or 0
        )
        sources = {item["source"] for item in group}
        numeric_views_safe = (
            not is_numeric
            or {"original_page_psm6", "numeric_original_psm6"}.issubset(sources)
        )
        severe_reasons = {
            "cyrillic_latin_mix", "unexpected_characters",
            "malformed_table_value", "ocr_noise",
        }
        reasons = set(validation_reasons(
            best["value"], group_confidence, table_cell=is_table
        ))
        consensus_is_safe = (
            (group_confidence >= (0.50 if is_numeric else (0.75 if is_table else 0.65)))
            and numeric_views_safe
            and not (reasons & severe_reasons)
        )
        if not consensus_is_safe:
            agreeing = []
        else:
            key_value = best["key"].split(":", 1)[1]
            if best["key"].startswith("formula:"):
                silver_value = KNOWN_FORMULAS.get(key_value, normalize_value(best["value"]))
            elif best["key"].startswith("number:"):
                silver_value = key_value
            else:
                silver_value = normalize_value(best["value"])
            return {
                "silver_status": "silver_consensus",
                "silver_value": silver_value,
                "silver_confidence": round(group_confidence, 6),
                "acceptance_reason": "two_or_more_ocr_passes_agree",
                "consensus_sources": sorted(sources),
                "candidates": candidates,
            }
    if record["type"] == "word" and record.get("confidence", 0.0) >= 0.96:
        raw = record.get("raw_ocr", "")
        if re.fullmatch(r"[А-Яа-яЁёA-Za-z-]{3,}", raw):
            raw_key = candidate_key(record, raw)
            strong_conflicts = [
                candidate for candidate in usable
                if candidate["source"] != "original_page_psm6"
                and candidate["confidence"] >= 0.75
                and candidate["key"] != raw_key
            ]
            if not strong_conflicts:
                return {
                    "silver_status": "silver_high_confidence",
                    "silver_value": normalize_value(raw),
                    "silver_confidence": round(record["confidence"], 6),
                    "acceptance_reason": "very_high_confidence_clean_word_without_strong_conflict",
                    "consensus_sources": ["original_page_psm6"],
                    "candidates": candidates,
                }

    return {
        "silver_status": "abstained",
        "silver_value": None,
        "silver_confidence": 0.0,
        "acceptance_reason": "insufficient_independent_evidence",
        "consensus_sources": [],
        "candidates": candidates,
    }


def words_in_cell(words: list[dict[str, Any]], box: dict[str, float]) -> list[dict[str, Any]]:
    selected = []
    for word in words:
        center_x = word["box"]["x"] + word["box"]["width"] / 2
        center_y = word["box"]["y"] + word["box"]["height"] / 2
        if box["x"] <= center_x <= box["x"] + box["width"] and box["y"] <= center_y <= box["y"] + box["height"]:
            selected.append(word)
    selected.sort(key=lambda word: (-(word["box"]["y"] + word["box"]["height"] / 2), word["box"]["x"]))
    return selected


def table_cell_records(
    page: int,
    original_image: Image.Image,
    preprocessed_image: Image.Image,
    raw_words: list[dict[str, Any]],
    verification_dir: Path,
) -> list[dict[str, Any]]:
    spec = TABLE_SPECS[page]
    records: list[dict[str, Any]] = []
    x_edges, y_edges = spec["x_edges"], spec["y_edges"]
    for row in range(len(y_edges) - 1):
        for column in range(len(x_edges) - 1):
            box = {
                "x": x_edges[column],
                "y": y_edges[row + 1],
                "width": x_edges[column + 1] - x_edges[column],
                "height": y_edges[row] - y_edges[row + 1],
            }
            cell_id = f"p{page:03d}-r{row:02d}-c{column:02d}"
            crop_path = verification_dir / "review" / "crops" / "tables" / f"{cell_id}.png"
            make_crop(original_image, box, crop_path, padding=8)
            alternative_crop = verification_dir / "tmp_cell_crops" / f"{cell_id}.png"
            make_crop(preprocessed_image, box, alternative_crop, padding=2)
            alternate_lines = ocr_image(alternative_crop, psm=7)
            alternate_words = flatten_words(alternate_lines)
            alternative = " ".join(word["text"] for word in alternate_words).strip()
            alternative_confidence = (
                sum(word["confidence"] for word in alternate_words) / len(alternate_words) if alternate_words else 0.0
            )
            selected = words_in_cell(raw_words, box)
            raw = " ".join(word["text"] for word in selected).strip()
            confidence = sum(word["confidence"] for word in selected) / len(selected) if selected else 0.0
            reasons = validation_reasons(
                raw, confidence, alternative, alternative_confidence, table_cell=True
            )
            if reasons and not alternative:
                reasons.append("alternative_unavailable")
            status = record_status(confidence, raw, alternative, reasons)
            records.append({
                "id": cell_id,
                "type": "table_cell",
                "page": page,
                "row": row,
                "column": column,
                "record_version": f"table_geometry_{TABLE_GEOMETRY_VERSION}",
                "bbox": box,
                "raw_ocr": raw,
                "alternative_ocr": alternative,
                "confidence": round(confidence, 6),
                "alternative_confidence": round(alternative_confidence, 6),
                "normalized_value": normalize_value(raw),
                "verified_value": None,
                "review_status": status,
                "verification_source": "automatic_rules" if status == "auto_verified" else "unverified",
                "flag_reasons": reasons,
                "priority": review_priority(reasons, "table_cell"),
                "crop": str(crop_path.relative_to(verification_dir)),
            })
    return records


def reconstruct_csv(records: list[dict[str, Any]], path: Path, value_field: str = "raw_ocr") -> None:
    if not records:
        return
    max_row = max(record["row"] for record in records)
    max_column = max(record["column"] for record in records)
    grid = [["" for _ in range(max_column + 1)] for _ in range(max_row + 1)]
    for record in records:
        grid[record["row"]][record["column"]] = record.get(value_field) or ""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as stream:
        csv.writer(stream).writerows(grid)


def prepare(args: argparse.Namespace) -> None:
    source = args.source.resolve()
    verification_dir = source / "verification"
    images = sorted((source / "images").glob("page-*.png"))
    if not images:
        raise SystemExit(f"No page images found under {source / 'images'}")

    word_records: list[dict[str, Any]] = []
    cell_records: list[dict[str, Any]] = []
    review_queue: list[dict[str, Any]] = []
    validation_pages = {
        record["page"] for record in read_jsonl(source / "splits" / "validation.jsonl")
    }
    for page, image_path in enumerate(images, start=1):
        raw_path = source / "raw_ocr" / f"{image_path.stem}.json"
        if not raw_path.exists():
            raise SystemExit(f"Missing immutable raw OCR: {raw_path}")
        raw_lines = json.loads(raw_path.read_text(encoding="utf-8"))
        preprocessed_path = verification_dir / "preprocessed" / image_path.name
        candidate_path = verification_dir / "candidate_ocr" / f"{image_path.stem}.json"
        if not args.reuse_candidate or not preprocessed_path.exists() or not candidate_path.exists():
            preprocess(image_path, preprocessed_path)
            candidate_lines = ocr_image(preprocessed_path, psm=3)
            candidate_path.parent.mkdir(parents=True, exist_ok=True)
            candidate_path.write_text(json.dumps(candidate_lines, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        else:
            candidate_lines = json.loads(candidate_path.read_text(encoding="utf-8"))

        raw_words = flatten_words(raw_lines)
        candidate_words = flatten_words(candidate_lines)
        with Image.open(image_path) as original_image:
            for index, word in enumerate(raw_words):
                if inside_known_table(page, word):
                    continue
                alternate = alternative_for(word, candidate_words)
                alternative = alternate["text"] if alternate else ""
                alternative_confidence = alternate["confidence"] if alternate else 0.0
                reasons = validation_reasons(
                    word["text"], word["confidence"], alternative, alternative_confidence
                )
                status = record_status(word["confidence"], word["text"], alternative, reasons)
                item_id = f"p{page:03d}-w{index:04d}"
                crop_rel = Path("review") / "crops" / "words" / f"{item_id}.png"
                queued_for_review = status == "needs_review" and page in validation_pages
                if queued_for_review:
                    crop_path = make_crop(original_image, word["box"], verification_dir / crop_rel)
                    if not alternative:
                        isolated_words = flatten_words(ocr_image(crop_path, psm=8))
                        alternative = " ".join(item["text"] for item in isolated_words).strip()
                        alternative_confidence = (
                            sum(item["confidence"] for item in isolated_words) / len(isolated_words)
                            if isolated_words else 0.0
                        )
                        reasons = validation_reasons(
                            word["text"], word["confidence"], alternative, alternative_confidence
                        )
                        if not alternative:
                            reasons.append("alternative_unavailable")
                record = {
                    "id": item_id,
                    "type": "word",
                    "page": page,
                    "row": None,
                    "column": None,
                    "bbox": word["box"],
                    "raw_ocr": word["text"],
                    "alternative_ocr": alternative,
                    "confidence": round(word["confidence"], 6),
                    "alternative_confidence": round(alternative_confidence, 6),
                    "normalized_value": normalize_value(word["text"]),
                    "verified_value": None,
                    "review_status": status,
                    "verification_source": "automatic_rules" if status == "auto_verified" else "unverified",
                    "flag_reasons": reasons,
                    "priority": review_priority(reasons, "word"),
                    "crop": str(crop_rel) if queued_for_review else None,
                }
                if queued_for_review:
                    review_queue.append(record)
                word_records.append(record)

        if page in TABLE_SPECS:
            with Image.open(image_path) as original_image, Image.open(preprocessed_path) as preprocessed_image:
                page_cells = table_cell_records(
                    page, original_image, preprocessed_image, raw_words, verification_dir
                )
            cell_records.extend(page_cells)
            review_queue.extend(record for record in page_cells if record["review_status"] == "needs_review")
            write_jsonl(verification_dir / "tables" / f"page-{page:03d}-cells.jsonl", page_cells)
            reconstruct_csv(page_cells, verification_dir / "tables" / f"page-{page:03d}-raw.csv")
        print(f"Prepared verification page {page}/{len(images)}")

    write_jsonl(verification_dir / "records" / "words.jsonl", word_records)
    write_jsonl(verification_dir / "records" / "table_cells.jsonl", cell_records)
    review_queue.sort(key=lambda record: (record["priority"], record["type"] != "table_cell", record["page"], record["id"]))
    write_jsonl(verification_dir / "review" / "queue.jsonl", review_queue)
    decisions = verification_dir / "review" / "decisions.jsonl"
    if not decisions.exists():
        write_jsonl(decisions, [])
    counts = Counter(record["review_status"] for record in word_records + cell_records)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "raw_ocr_preserved": True,
        "pages": len(images),
        "word_records": len(word_records),
        "table_cell_records": len(cell_records),
        "review_queue_items": len(review_queue),
        "review_scope": {
            "ordinary_text_pages": sorted(validation_pages),
            "table_pages": sorted(TABLE_SPECS),
        },
        "status_counts": dict(counts),
        "table_pages": sorted(TABLE_SPECS),
        "note": "Only human_verified records are gold ground truth.",
    }
    (verification_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def add_candidate(candidates: list[dict[str, Any]], source: str, value: str, confidence: float) -> None:
    value = (value or "").strip()
    if value:
        candidates.append({
            "source": source,
            "value": value,
            "confidence": round(float(confidence), 6),
            "normalized_value": normalize_value(value),
        })


def page_variant(
    image: Path,
    destination: Path,
    *,
    psm: int,
    reuse: bool,
    language: str = "rus+eng",
    whitelist: str | None = None,
) -> list[dict[str, Any]]:
    if reuse and destination.exists():
        return json.loads(destination.read_text(encoding="utf-8"))
    lines = ocr_image(image, psm=psm, language=language, whitelist=whitelist)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(lines, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return lines


def build_silver(args: argparse.Namespace) -> None:
    source = args.source.resolve()
    verification_dir = source / "verification"
    silver_dir = verification_dir / "silver"
    word_records = read_jsonl(verification_dir / "records" / "words.jsonl")
    cell_records = read_jsonl(verification_dir / "records" / "table_cells.jsonl")
    if not word_records or not cell_records:
        raise SystemExit("Run `verification_pipeline.py prepare` before building silver data.")

    words_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    cells_by_page: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for record in word_records:
        words_by_page[record["page"]].append(record)
    for record in cell_records:
        cells_by_page[record["page"]].append(record)

    silver_records: list[dict[str, Any]] = []
    images = sorted((source / "images").glob("page-*.png"))
    for page, image_path in enumerate(images, start=1):
        preprocessed_path = verification_dir / "preprocessed" / image_path.name
        psm3_path = verification_dir / "candidate_ocr" / f"{image_path.stem}.json"
        if not preprocessed_path.exists() or not psm3_path.exists():
            raise SystemExit("Missing verification candidates. Run the prepare command first.")
        psm3_lines = json.loads(psm3_path.read_text(encoding="utf-8"))
        psm11_lines = page_variant(
            preprocessed_path,
            silver_dir / "ocr_psm11" / f"{image_path.stem}.json",
            psm=11,
            reuse=args.reuse_variants,
        )
        psm6_lines = page_variant(
            preprocessed_path,
            silver_dir / "ocr_psm6_preprocessed" / f"{image_path.stem}.json",
            psm=6,
            reuse=args.reuse_variants,
        )
        psm3_words = flatten_words(psm3_lines)
        psm11_words = flatten_words(psm11_lines)
        psm6_words = flatten_words(psm6_lines)

        for record in words_by_page.get(page, []):
            candidates: list[dict[str, Any]] = []
            add_candidate(candidates, "original_page_psm6", record["raw_ocr"], record["confidence"])
            psm3_match = alternative_for({"box": record["bbox"]}, psm3_words)
            if psm3_match:
                add_candidate(candidates, "preprocessed_page_psm3", psm3_match["text"], psm3_match["confidence"])
            psm6_match = alternative_for({"box": record["bbox"]}, psm6_words)
            if psm6_match:
                add_candidate(candidates, "preprocessed_page_psm6", psm6_match["text"], psm6_match["confidence"])
            psm11_match = alternative_for({"box": record["bbox"]}, psm11_words)
            if psm11_match:
                add_candidate(candidates, "preprocessed_page_psm11", psm11_match["text"], psm11_match["confidence"])
            if not psm3_match and record.get("alternative_ocr"):
                add_candidate(
                    candidates, "isolated_crop_psm8", record["alternative_ocr"], record["alternative_confidence"]
                )
            silver_records.append({
                "id": record["id"], "type": record["type"], "page": page,
                "row": None, "column": None, "bbox": record["bbox"],
                "raw_ocr": record["raw_ocr"], "normalized_value": record["normalized_value"],
                "verified_value": record.get("verified_value"),
                **decide_silver(record, candidates),
            })

        for record in cells_by_page.get(page, []):
            candidates = []
            add_candidate(candidates, "original_page_psm6", record["raw_ocr"], record["confidence"])
            add_candidate(
                candidates, "preprocessed_cell_psm7", record["alternative_ocr"], record["alternative_confidence"]
            )
            for source_name, variant_words in [
                ("preprocessed_page_psm3", psm3_words),
                ("preprocessed_page_psm6", psm6_words),
                ("preprocessed_page_psm11", psm11_words),
            ]:
                selected = words_in_cell(variant_words, record["bbox"])
                add_candidate(candidates, source_name, text_from_words(selected), average_confidence(selected))

            crop_path = verification_dir / record["crop"]
            cell_variant_path = (
                silver_dir / "ocr_cell_psm8" / TABLE_GEOMETRY_VERSION / f"{record['id']}.json"
            )
            cell_variant_lines = page_variant(
                crop_path, cell_variant_path, psm=8, reuse=args.reuse_variants
            )
            cell_variant_words = flatten_words(cell_variant_lines)
            add_candidate(
                candidates,
                "isolated_original_cell_psm8",
                text_from_words(cell_variant_words),
                average_confidence(cell_variant_words),
            )
            if is_numeric_cell(page, record["row"], record["column"]):
                numeric_whitelist = "0123456789,.-+"
                specialized_variants = [
                    (crop_path, "numeric_original_psm6", 6),
                    (crop_path, "numeric_original_psm10", 10),
                    (verification_dir / "tmp_cell_crops" / f"{record['id']}.png", "numeric_preprocessed_psm6", 6),
                ]
                for numeric_image, source_name, numeric_psm in specialized_variants:
                    variant_lines = page_variant(
                        numeric_image,
                        silver_dir / "ocr_numeric" / TABLE_GEOMETRY_VERSION
                        / source_name / f"{record['id']}.json",
                        psm=numeric_psm,
                        reuse=args.reuse_variants,
                        language="eng",
                        whitelist=numeric_whitelist,
                    )
                    variant_words = flatten_words(variant_lines)
                    add_candidate(
                        candidates,
                        source_name,
                        text_from_words(variant_words),
                        average_confidence(variant_words),
                    )
            silver_records.append({
                "id": record["id"], "type": record["type"], "page": page,
                "row": record["row"], "column": record["column"], "bbox": record["bbox"],
                "record_version": record.get("record_version"),
                "raw_ocr": record["raw_ocr"], "normalized_value": record["normalized_value"],
                "verified_value": record.get("verified_value"),
                "expected_formula": expected_formula(page, record["row"], record["column"]),
                "numeric_cell": is_numeric_cell(page, record["row"], record["column"]),
                **decide_silver(record, candidates),
            })
        print(f"Built silver candidates for page {page}/{len(images)}")

    accepted = [record for record in silver_records if record["silver_status"].startswith("silver_")]
    abstained = [record for record in silver_records if record["silver_status"] == "abstained"]
    write_jsonl(silver_dir / "records.jsonl", silver_records)
    write_jsonl(silver_dir / "accepted.jsonl", accepted)
    write_jsonl(silver_dir / "abstained.jsonl", abstained)
    for page in TABLE_SPECS:
        page_records = [record for record in silver_records if record["type"] == "table_cell" and record["page"] == page]
        reconstruct_csv(page_records, silver_dir / "tables" / f"page-{page:03d}-silver.csv", "silver_value")
    status_counts = Counter(record["silver_status"] for record in silver_records)
    type_counts = {
        item_type: dict(Counter(record["silver_status"] for record in silver_records if record["type"] == item_type))
        for item_type in {"word", "table_cell"}
    }
    reason_counts = Counter(record["acceptance_reason"] for record in silver_records)
    summary = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "records": len(silver_records),
        "accepted": len(accepted),
        "abstained": len(abstained),
        "coverage": len(accepted) / max(1, len(silver_records)),
        "status_counts": dict(status_counts),
        "status_by_type": type_counts,
        "acceptance_reasons": dict(reason_counts),
        "raw_ocr_preserved": True,
        "label_quality": "silver_not_gold",
        "warning": "Coverage and OCR-pass agreement are not accuracy. Evaluate against human gold if available.",
    }
    (silver_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def latest_decisions(path: Path) -> dict[str, dict[str, Any]]:
    return {decision["id"]: decision for decision in read_jsonl(path)}


def build_ground_truth(args: argparse.Namespace) -> None:
    verification_dir = args.source.resolve() / "verification"
    records = read_jsonl(verification_dir / "records" / "words.jsonl")
    records += read_jsonl(verification_dir / "records" / "table_cells.jsonl")
    silver = {record["id"]: record for record in read_jsonl(verification_dir / "silver" / "records.jsonl")}
    decisions = latest_decisions(verification_dir / "review" / "decisions.jsonl")
    merged: list[dict[str, Any]] = []
    for original in records:
        record = dict(original)
        if record["id"] in silver:
            silver_record = silver[record["id"]]
            record["silver_status"] = silver_record["silver_status"]
            record["silver_value"] = silver_record["silver_value"]
            record["silver_confidence"] = silver_record["silver_confidence"]
            record["silver_acceptance_reason"] = silver_record["acceptance_reason"]
        decision = decisions.get(record["id"])
        if (
            decision
            and record["type"] == "table_cell"
            and decision.get("record_version") != record.get("record_version")
        ):
            # Keep old decisions in the append-only log, but never apply a
            # label made against a different crop geometry.
            decision = None
        if decision and decision["action"] in {"accept", "correct"}:
            record["verified_value"] = decision["verified_value"]
            record["review_status"] = "human_verified"
            record["verification_source"] = "human_accept" if decision["action"] == "accept" else "human_correction"
            record["reviewed_at"] = decision["reviewed_at"]
        elif decision and decision["action"] == "skip":
            record["review_status"] = "needs_review"
            record["verification_source"] = "human_skipped"
            record["reviewed_at"] = decision["reviewed_at"]
        merged.append(record)

    ground_truth_dir = verification_dir / "ground_truth"
    write_jsonl(ground_truth_dir / "all_records.jsonl", merged)
    gold = [record for record in merged if record["review_status"] == "human_verified"]
    write_jsonl(ground_truth_dir / "gold.jsonl", gold)
    for page in TABLE_SPECS:
        table = [record for record in merged if record["type"] == "table_cell" and record["page"] == page]
        working = []
        verified = []
        for record in table:
            working_record = dict(record)
            working_record["display_value"] = record["verified_value"] or record["normalized_value"] or record["raw_ocr"]
            working.append(working_record)
            verified_record = dict(record)
            verified_record["display_value"] = record["verified_value"] if record["review_status"] == "human_verified" else ""
            verified.append(verified_record)
        reconstruct_csv(working, ground_truth_dir / "tables" / f"page-{page:03d}-working.csv", "display_value")
        reconstruct_csv(verified, ground_truth_dir / "tables" / f"page-{page:03d}-gold.csv", "display_value")
    summary = {
        "records": len(merged),
        "human_verified_gold": len(gold),
        "needs_review": sum(record["review_status"] == "needs_review" for record in merged),
        "auto_verified_not_gold": sum(record["review_status"] == "auto_verified" for record in merged),
        "silver_accepted_not_gold": sum((record.get("silver_status") or "").startswith("silver_") for record in merged),
    }
    (ground_truth_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


def edit_distance(source: list[str] | str, target: list[str] | str) -> tuple[int, list[tuple[str, str]]]:
    rows, columns = len(source) + 1, len(target) + 1
    matrix = [[0] * columns for _ in range(rows)]
    for row in range(rows):
        matrix[row][0] = row
    for column in range(columns):
        matrix[0][column] = column
    for row in range(1, rows):
        for column in range(1, columns):
            substitution = 0 if source[row - 1] == target[column - 1] else 1
            matrix[row][column] = min(
                matrix[row - 1][column] + 1,
                matrix[row][column - 1] + 1,
                matrix[row - 1][column - 1] + substitution,
            )
    errors: list[tuple[str, str]] = []
    row, column = len(source), len(target)
    while row or column:
        if row and column and source[row - 1] == target[column - 1]:
            row, column = row - 1, column - 1
        elif row and column and matrix[row][column] == matrix[row - 1][column - 1] + 1:
            errors.append((str(source[row - 1]), str(target[column - 1])))
            row, column = row - 1, column - 1
        elif row and matrix[row][column] == matrix[row - 1][column] + 1:
            errors.append((str(source[row - 1]), ""))
            row -= 1
        else:
            errors.append(("", str(target[column - 1])))
            column -= 1
    return matrix[-1][-1], errors


def is_numeric(value: str) -> bool:
    return bool(NUMERIC_RE.fullmatch(value.strip(" |()[]{}:;").replace(".", ",")))


def error_categories(errors: list[tuple[str, str]]) -> dict[str, int]:
    categories = Counter()
    for predicted, truth in errors:
        if not predicted:
            categories["missing_character"] += 1
        elif not truth:
            categories["extra_character"] += 1
        elif predicted.isdigit() != truth.isdigit():
            categories["digit_letter_confusion"] += 1
        elif (CYRILLIC_RE.fullmatch(predicted) and LATIN_RE.fullmatch(truth)) or (
            LATIN_RE.fullmatch(predicted) and CYRILLIC_RE.fullmatch(truth)
        ):
            categories["cyrillic_latin_confusion"] += 1
        elif unicodedata.category(predicted).startswith("P") or unicodedata.category(truth).startswith("P"):
            categories["punctuation_error"] += 1
        else:
            categories["character_substitution"] += 1
    return dict(categories)


def evaluate(args: argparse.Namespace) -> None:
    verification_dir = args.source.resolve() / "verification"
    gold = read_jsonl(verification_dir / "ground_truth" / "gold.jsonl")
    silver_records = read_jsonl(verification_dir / "silver" / "records.jsonl")
    silver_lookup = {record["id"]: record for record in silver_records}
    silver_accepted = [record for record in silver_records if record["silver_status"].startswith("silver_")]
    all_records = read_jsonl(verification_dir / "records" / "words.jsonl")
    all_cells = read_jsonl(verification_dir / "records" / "table_cells.jsonl")
    all_records += all_cells
    queue_size = len(read_jsonl(verification_dir / "review" / "queue.jsonl"))
    alternatives = [record for record in all_records if record.get("alternative_ocr")]
    agreement = sum(
        normalize_value(record["raw_ocr"]) == normalize_value(record["alternative_ocr"])
        for record in alternatives
    )
    expected_cells = sum(
        (len(spec["x_edges"]) - 1) * (len(spec["y_edges"]) - 1) for spec in TABLE_SPECS.values()
    )
    unique_coordinates = {(record["page"], record["row"], record["column"]) for record in all_cells}
    operational = {
        "total_traceable_records": len(all_records),
        "review_queue_items": queue_size,
        "manual_review_reduction_vs_all_records": 1 - queue_size / max(1, len(all_records)),
        "auto_verified_not_gold": sum(record["review_status"] == "auto_verified" for record in all_records),
        "alternative_ocr_matches": len(alternatives),
        "normalized_engine_agreement": agreement / len(alternatives) if alternatives else None,
        "table_structure_coverage": len(unique_coordinates) / expected_cells,
        "silver_records": len(silver_records),
        "silver_accepted": len(silver_accepted),
        "silver_coverage": len(silver_accepted) / max(1, len(silver_records)) if silver_records else None,
    }
    report_path = verification_dir / "evaluation" / "report.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    if not gold:
        report = {
            "status": "awaiting_human_verified_ground_truth",
            "evaluated_records": 0,
            "ocr": {"cer": None, "wer": None},
            "tables": {
                "cell_exact_match_accuracy": None,
                "numeric_value_accuracy": None,
                "row_column_structure_accuracy": None,
            },
            "silver_vs_human_gold": {
                "coverage_on_gold": None,
                "cer_on_accepted": None,
                "wer_on_accepted": None,
                "table_cell_exact_match_on_accepted": None,
                "numeric_value_accuracy_on_accepted": None,
            },
            "operational_quality_signals_not_accuracy": operational,
            "message": (
                "Automatic silver output is available without review. CER/WER and true table accuracy "
                "require an independent human-verified sample; add one only if measured accuracy is needed."
            ),
        }
    else:
        raw_text = " ".join(record["raw_ocr"] for record in gold)
        alternative_text = " ".join(record["alternative_ocr"] or record["raw_ocr"] for record in gold)
        truth_text = " ".join(record["verified_value"] for record in gold)
        raw_distance, raw_errors = edit_distance(raw_text, truth_text)
        alternative_distance, _ = edit_distance(alternative_text, truth_text)
        raw_word_distance, _ = edit_distance(raw_text.split(), truth_text.split())
        table_gold = [record for record in gold if record["type"] == "table_cell"]
        numeric_gold = [record for record in table_gold if is_numeric(record["verified_value"])]
        exact = sum(record["raw_ocr"].strip() == record["verified_value"].strip() for record in table_gold)
        numeric_exact = sum(
            normalize_value(record["raw_ocr"]).strip(" |()[]{}:;")
            == normalize_value(record["verified_value"]).strip(" |()[]{}:;")
            for record in numeric_gold
        )
        covered_gold = [
            record for record in gold
            if record["id"] in silver_lookup
            and silver_lookup[record["id"]]["silver_status"].startswith("silver_")
        ]
        silver_text = " ".join(silver_lookup[record["id"]]["silver_value"] for record in covered_gold)
        silver_truth = " ".join(record["verified_value"] for record in covered_gold)
        silver_char_distance, _ = edit_distance(silver_text, silver_truth)
        silver_word_distance, _ = edit_distance(silver_text.split(), silver_truth.split())
        covered_table_gold = [record for record in covered_gold if record["type"] == "table_cell"]
        covered_numeric_gold = [record for record in covered_table_gold if is_numeric(record["verified_value"])]
        silver_table_exact = sum(
            silver_lookup[record["id"]]["silver_value"].strip() == record["verified_value"].strip()
            for record in covered_table_gold
        )
        silver_numeric_exact = sum(
            normalize_value(silver_lookup[record["id"]]["silver_value"]).strip(" |()[]{}:;")
            == normalize_value(record["verified_value"]).strip(" |()[]{}:;")
            for record in covered_numeric_gold
        )
        report = {
            "status": "evaluated",
            "evaluated_records": len(gold),
            "human_verified_words": sum(record["type"] == "word" for record in gold),
            "human_verified_table_cells": len(table_gold),
            "ocr": {
                "cer": raw_distance / max(1, len(truth_text)),
                "wer": raw_word_distance / max(1, len(truth_text.split())),
                "alternative_cer": alternative_distance / max(1, len(truth_text)),
                "cer_absolute_improvement": (raw_distance - alternative_distance) / max(1, len(truth_text)),
            },
            "tables": {
                "cell_exact_match_accuracy": exact / len(table_gold) if table_gold else None,
                "numeric_value_accuracy": numeric_exact / len(numeric_gold) if numeric_gold else None,
                "row_column_structure_accuracy": len(unique_coordinates) / expected_cells,
                "evaluated_numeric_cells": len(numeric_gold),
                "segmentation_errors": expected_cells - len(unique_coordinates),
            },
            "silver_vs_human_gold": {
                "coverage_on_gold": len(covered_gold) / len(gold),
                "evaluated_accepted_records": len(covered_gold),
                "cer_on_accepted": silver_char_distance / max(1, len(silver_truth)) if covered_gold else None,
                "wer_on_accepted": silver_word_distance / max(1, len(silver_truth.split())) if covered_gold else None,
                "table_cell_exact_match_on_accepted": (
                    silver_table_exact / len(covered_table_gold) if covered_table_gold else None
                ),
                "numeric_value_accuracy_on_accepted": (
                    silver_numeric_exact / len(covered_numeric_gold) if covered_numeric_gold else None
                ),
            },
            "operational_quality_signals_not_accuracy": operational,
            "error_breakdown": {
                **error_categories(raw_errors),
                "table_segmentation_errors": expected_cells - len(unique_coordinates),
            },
        }
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    markdown = ["# OCR verification evaluation", "", f"Status: `{report['status']}`", ""]
    quality = report["operational_quality_signals_not_accuracy"]
    markdown += [
        "## Operational quality signals (not accuracy)", "",
        f"- Traceable word/cell records: {quality['total_traceable_records']}",
        f"- Review queue: {quality['review_queue_items']}",
        f"- Manual-review reduction: {quality['manual_review_reduction_vs_all_records']:.1%}",
        f"- Auto-verified records (not gold): {quality['auto_verified_not_gold']}",
        f"- Table structure coverage: {quality['table_structure_coverage']:.1%}", "",
    ]
    if quality["silver_records"]:
        markdown += [
            f"- Silver accepted: {quality['silver_accepted']} of {quality['silver_records']}",
            f"- Silver coverage: {quality['silver_coverage']:.1%}", "",
        ]
    if report["status"] == "evaluated":
        markdown += [
            "## Verified accuracy", "",
            f"- Human-verified records: {report['evaluated_records']}",
            f"- CER: {report['ocr']['cer']:.4f}",
            f"- WER: {report['ocr']['wer']:.4f}",
            f"- Alternative OCR CER: {report['ocr']['alternative_cer']:.4f}",
            f"- Absolute CER improvement: {report['ocr']['cer_absolute_improvement']:.4f}",
            f"- Table cell exact match: {report['tables']['cell_exact_match_accuracy']}",
            f"- Numeric value accuracy: {report['tables']['numeric_value_accuracy']}",
            f"- Row/column structure accuracy: {report['tables']['row_column_structure_accuracy']:.4f}",
            "",
            "## Silver labels versus human gold", "",
            f"- Coverage on gold: {report['silver_vs_human_gold']['coverage_on_gold']:.1%}",
            f"- CER on accepted silver: {report['silver_vs_human_gold']['cer_on_accepted']}",
            f"- WER on accepted silver: {report['silver_vs_human_gold']['wer_on_accepted']}",
            f"- Table exact match on accepted silver: {report['silver_vs_human_gold']['table_cell_exact_match_on_accepted']}",
            f"- Numeric accuracy on accepted silver: {report['silver_vs_human_gold']['numeric_value_accuracy_on_accepted']}",
            "",
            "## Error breakdown",
            "",
        ]
        markdown += [f"- {name}: {count}" for name, count in report["error_breakdown"].items()]
    else:
        markdown.append(report["message"])
    (report_path.parent / "report.md").write_text("\n".join(markdown) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


def parser() -> argparse.ArgumentParser:
    main = argparse.ArgumentParser(description="Prepare review data, build ground truth, and evaluate OCR.")
    subparsers = main.add_subparsers(dest="command", required=True)
    for name, function in [
        ("prepare", prepare),
        ("build-silver", build_silver),
        ("build-ground-truth", build_ground_truth),
        ("evaluate", evaluate),
    ]:
        command_parser = subparsers.add_parser(name)
        command_parser.add_argument("--source", type=Path, default=Path("output/scanned_report"))
        if name == "prepare":
            command_parser.add_argument("--reuse-candidate", action="store_true")
        if name == "build-silver":
            command_parser.add_argument("--reuse-variants", action="store_true")
        command_parser.set_defaults(function=function)
    return main


def main() -> None:
    args = parser().parse_args()
    args.function(args)


if __name__ == "__main__":
    main()

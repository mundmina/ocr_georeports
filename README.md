# Scanned Russian report OCR verification pipeline

This repository processes `8003_Report with tables.pdf` while preserving every
OCR prediction and source bounding box. It supports two verification paths:

```text
PDF -> page images -> raw OCR -> preprocessing -> alternative OCR
    -> automatic validation -> conservative consensus
        -> accepted silver labels / abstained records       (no review)
        -> optional suspicious-item review -> human gold    (measured accuracy)
```

The automatic path does not pretend that OCR output is ground truth. It accepts
only values supported by repeated OCR evidence or fixed table-schema rules and
abstains on everything else. These records are labelled **silver**, not gold.
Only records with `review_status: human_verified` enter the gold dataset.

## Requirements

The pipeline uses Python 3, Pillow, Poppler, and Tesseract with Russian language
data. On macOS:

```bash
brew install poppler tesseract tesseract-lang
python3 -m pip install Pillow
```

## 1. Create or refresh the original OCR

```bash
python3 extract_scanned_report.py \
  '8003_Report with tables.pdf' \
  --output output/scanned_report
```

Use `--reuse-images` to repeat OCR without rendering the PDF again:

```bash
python3 extract_scanned_report.py \
  '8003_Report with tables.pdf' \
  --output output/scanned_report \
  --reuse-images
```

This stage uses Tesseract `rus+eng`, page segmentation mode 6. It stores
confidence scores from 0 to 1 and normalized bounding boxes with a bottom-left
origin:

```json
{"x": 0.25, "y": 0.50, "width": 0.10, "height": 0.02}
```

The original 80/20 page split is reproducible with seed 42: training pages
1, 2, 4, 5, 6, 9, 10, 11, 12, 13 and validation pages 3, 7, 8. For a future
multi-report collection, split by report rather than page to prevent template
leakage.

## 2. Prepare verification data

```bash
python3 verification_pipeline.py prepare --source output/scanned_report
```

This command:

- preserves `raw_ocr/` unchanged;
- creates contrast-normalized, sharpened page images under
  `verification/preprocessed/`;
- runs a separate Tesseract PSM 3 candidate under
  `verification/candidate_ocr/`;
- compares predictions using bounding-box overlap;
- flags confidence, OCR disagreement, malformed numbers, decimal points,
  mixed Cyrillic/Latin characters, unexpected characters, OCR noise, and
  malformed table values;
- creates crops only for items placed in the review queue.

To reuse preprocessing and alternative OCR after changing validation rules or
table geometry:

```bash
python3 verification_pipeline.py prepare \
  --source output/scanned_report \
  --reuse-candidate
```

All records are retained, but ordinary-text review is limited to held-out pages
3, 7, and 8. Pages 11 and 13 are reviewed as table cells. This keeps the current
queue to roughly two hundred suspicious items instead of asking a person to
transcribe all 13 pages.

## 3. Run without manual review

```bash
python3 verification_pipeline.py build-silver \
  --source output/scanned_report
```

This runs additional page and isolated-cell OCR layouts, groups normalized
predictions, applies decimal-comma/formula/table-shape checks, and either emits
a silver value or abstains. Numeric table cells require agreement between the
full-page context and a separately cropped numeric pass. Raw OCR remains
unchanged. To reuse cached OCR
variants after adjusting consensus rules:

```bash
python3 verification_pipeline.py build-silver \
  --source output/scanned_report \
  --reuse-variants
```

Automatic outputs:

- `verification/silver/records.jsonl`: every record, all candidates, decision,
  confidence, provenance, and abstention reason;
- `verification/silver/accepted.jsonl`: conservative accepted subset;
- `verification/silver/abstained.jsonl`: uncertain subset with no guessed value;
- `verification/silver/tables/page-011-silver.csv` and
  `page-013-silver.csv`: table structure preserved, with blanks for abstentions;
- `verification/silver/summary.json`: coverage and decision counts.

Automatic statuses are `silver_consensus`, `silver_rule_corrected`,
`silver_high_confidence`, and `abstained`. Silver labels can be used for search,
indexing, bootstrapping, or weak supervision. They must not be presented as
measured gold accuracy.

## 4. Optional review of abstained/suspicious items

```bash
python3 review_server.py --source output/scanned_report --port 8765
```

Open [http://127.0.0.1:8765](http://127.0.0.1:8765). Each item shows its page,
crop, raw OCR, alternative OCR, confidence, and flag reasons.

- **Accept** confirms the raw OCR exactly.
- **Correct** saves the entered value.
- **Skip** leaves the item as `needs_review`.

Keyboard shortcuts are `A`, `C`, and `S`. Decisions are appended to
`verification/review/decisions.jsonl`; rerunning preparation does not erase
them.

## 5. Build structured ground truth

```bash
python3 verification_pipeline.py build-ground-truth \
  --source output/scanned_report
```

Important outputs:

- `verification/ground_truth/all_records.jsonl`: all raw, automatic, skipped,
  and human-reviewed records with traceability;
- `verification/ground_truth/gold.jsonl`: only human-verified records;
- `verification/ground_truth/tables/page-011-working.csv` and
  `page-013-working.csv`: reconstructed tables using the best available value;
- `verification/ground_truth/tables/page-011-gold.csv` and
  `page-013-gold.csv`: blanks for cells that have not been human verified.

Each record keeps `page`, `row`, `column`, `bbox`, `raw_ocr`,
`alternative_ocr`, `normalized_value`, `verified_value`, `confidence`,
`verification_source`, `review_status`, validation reasons, and—when the silver
stage was run—`silver_value`, `silver_status`, `silver_confidence`, and its
acceptance reason.

The statuses are:

- `raw_ocr`: unverified prediction that was not queued;
- `auto_verified`: high-confidence agreement, useful operationally but not gold;
- `needs_review`: suspicious or skipped;
- `human_verified`: accepted or corrected by a person and eligible as gold.

## Table handling

Pages 11 and 13 bypass the old whitespace-table heuristic. Their table geometry
is stored explicitly in normalized coordinates so merged headers and sparse
horizontal ruling do not collapse the structure.

- Page 11: 17 rows x 6 columns (102 cell records).
- Page 13: 12 rows x 9 columns (108 cell records).

Every cell has its own crop, bounding box, raw page OCR, isolated-cell OCR,
confidence, normalized proposal, review status, and verified value. No
normalization silently changes `raw_ocr`.

## 6. Evaluate

```bash
python3 verification_pipeline.py evaluate --source output/scanned_report
```

The command writes:

- `verification/evaluation/report.json`
- `verification/evaluation/report.md`

Metrics are calculated only from human-verified records:

- character error rate (CER);
- word error rate (WER);
- alternative-OCR CER and absolute CER improvement;
- table cell exact-match accuracy;
- normalized numeric-value accuracy;
- row/column structure coverage;
- substitutions, missing/extra characters, digit/letter confusion,
  Cyrillic/Latin confusion, punctuation, and segmentation errors.

Before any review decisions exist, the report deliberately returns `null`
accuracy values and `awaiting_human_verified_ground_truth`. It still reports
silver coverage and table-structure coverage. OCR confidence, pass agreement,
and cross-validation on OCR-generated labels are not accuracy.

There is no statistically valid way to calculate CER/WER from this PDF alone
without some independent reference text. Cross-fold validation does not create
that reference: all folds would still be scored against OCR-generated labels,
and page-level folds from one 13-page report share the same scan/template. If a
defensible accuracy number is later required, verify a small stratified sample
of words and table cells; the evaluator will score both raw OCR and accepted
silver labels against that sample.

## Tests

```bash
python3 -m unittest -v test_verification_pipeline.py
```

## Output separation

- `raw_ocr/`: immutable original Tesseract output.
- `verification/candidate_ocr/`: preprocessed alternative output.
- `verification/records/`: word and cell records.
- `verification/silver/`: automatic accepted/abstained records and table CSVs.
- `verification/review/`: suspicious-item queue, crops, and append-only decisions.
- `verification/ground_truth/`: merged traceability data and human-only gold.
- `verification/evaluation/`: reproducible metric reports.

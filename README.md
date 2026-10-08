# OCR GeoReports

A reproducible OCR and verification pipeline for scanned geological,
archival, and technical reports. It extracts text with coordinates, reconstructs
structured tables, detects uncertain readings, and produces traceable data for
search, analysis, or future model evaluation.

The project keeps every original OCR prediction. It never silently replaces a
reading with a corrected value.

```text
PDF -> rendered images -> raw OCR -> preprocessing -> additional OCR passes
    -> validation and table-cell extraction -> conservative consensus
       -> silver labels / abstentions
       -> optional human verification -> gold ground truth -> evaluation
```

## What it provides

- OCR with word-level confidence scores and normalized bounding boxes;
- deterministic image preprocessing and alternative OCR passes;
- suspicious-value detection for low confidence, number formatting, character
  confusion, mixed Cyrillic/Latin text, and OCR noise;
- structured table extraction with cell coordinates, raw OCR, normalized values,
  verified values, and review state;
- conservative automatic **silver labels**: accept only supported readings and
  abstain when evidence is insufficient;
- a lightweight local review interface for optional human verification;
- ground-truth export and OCR/table evaluation metrics.

## Requirements

- Python 3.10+
- [Poppler](https://poppler.freedesktop.org/) for PDF rendering
- [Tesseract OCR](https://github.com/tesseract-ocr/tesseract), with required
  language data installed (for example `rus` and `eng`)

On macOS:

```bash
brew install poppler tesseract tesseract-lang
python3 -m pip install -r requirements.txt
```

## Quick start

Put a scanned PDF anywhere on your machine, then run:

```bash
python3 extract_scanned_report.py \
  '/path/to/report.pdf' \
  --output output/my_report

python3 verification_pipeline.py prepare \
  --source output/my_report

python3 verification_pipeline.py build-silver \
  --source output/my_report
```

The OCR stage uses Tesseract and stores confidence scores from `0` to `1`.
Bounding boxes are normalized with a bottom-left origin:

```json
{"x": 0.25, "y": 0.50, "width": 0.10, "height": 0.02}
```

Use `--reuse-images`, `--reuse-candidate`, or `--reuse-variants` to reuse
generated artifacts on later runs.

## Automatic verification: silver labels

The pipeline reads a word or table cell through several OCR views, including the
full page, a preprocessed page, and isolated cell crops. A value is accepted as
**silver** only when the evidence agrees or when an explicit table-schema rule
applies. For numeric cells, it requires agreement between a full-page reading
and a separately cropped numeric reading.

If the readings conflict, the result is **abstained** instead of guessed.

```text
OCR pass 1: 0,54)
OCR pass 2: 0,54
OCR pass 3: 0,54
             ↓
silver value: 0,54
```

Silver data is useful for extraction, indexing, weak supervision, and building
a review queue. It is not a measured accuracy reference.

## Tables

Known table layouts are configured in `TABLE_SPECS` in
`verification_pipeline.py`. For a new report template, add or adjust the
normalized row and column edges there. Every generated cell retains:

- page, row, column, and bounding box;
- raw OCR and confidence;
- normalized and optional verified values;
- silver decision, evidence, and review status.

Reconstructed CSV files preserve empty cells when OCR abstains.

## Optional human verification: gold labels

If you need actual accuracy figures, review a representative subset rather than
the whole report:

```bash
python3 review_server.py --source output/my_report --port 8765
```

Open `http://127.0.0.1:8765`. The reviewer sees only suspicious words or cells
and can accept, correct, or skip each one.

Then build gold ground truth and evaluate:

```bash
python3 verification_pipeline.py build-ground-truth \
  --source output/my_report

python3 verification_pipeline.py evaluate \
  --source output/my_report
```

Only records marked `human_verified` are gold ground truth. The evaluator
calculates CER, WER, table cell exact-match accuracy, numeric-value accuracy,
table structure coverage, and an error breakdown.

Cross-validation on OCR-generated labels does not measure true OCR accuracy:
an independent verified reference is still needed for CER/WER.

## Output layout

```text
output/my_report/
├── raw_ocr/                 # Original OCR output; never overwritten
├── images/                  # Rendered PDF pages
├── verification/
│   ├── candidate_ocr/       # Preprocessed OCR candidates
│   ├── records/             # Word and table-cell records
│   ├── silver/              # Accepted and abstained automatic labels
│   ├── review/              # Optional review queue and decisions
│   ├── ground_truth/        # Merged data and human-only gold records
│   └── evaluation/          # JSON and Markdown metric reports
└── tables/                  # Initial OCR table candidates
```

## Tests

```bash
python3 -m unittest -v test_verification_pipeline.py
```

## Data policy

Source PDFs and generated OCR artifacts are excluded from Git by default.
Keep scans and outputs in secure storage appropriate for the source material.

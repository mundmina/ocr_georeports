# Project Completion Report: OCR Extraction and Verification Pipeline

**Project:** Scanned Russian report digitisation and OCR verification  
**Source document:** `8003_Report with tables.pdf`  
**Report date:** 5 September 2026  
**Current project state:** implementation complete; human verification and final accuracy evaluation are still in progress.

## 1. Executive summary

This project built a reproducible local pipeline for converting a scanned 13-page Russian-language PDF into traceable OCR data, candidate tables, review-ready records, and evaluation outputs. The central design principle is that **an OCR result is a prediction, not ground truth**. Every prediction remains linked to its page position, confidence, source image, and later verification decision.

The pipeline has completed the automated stages: PDF rendering, baseline OCR, conservative table extraction, preprocessing, alternative OCR, validation, review-queue generation, structured table-cell reconstruction, silver-label generation, and unit testing. The current output contains **1,932 traceable records**: 1,722 ordinary-word records and 210 explicit table-cell records. All table positions on pages 11 and 13 are represented, giving **100% table-structure coverage**.

The system reduced the immediate manual-review workload from all 1,932 records to **204 high-risk items** (an **89.4% reduction**). This is an operational-efficiency result, not an OCR-accuracy result. Eleven review decisions have been recorded (10 corrections and 1 acceptance), but the current human-gold dataset and evaluation report were generated before these decisions were merged. Therefore, the project must not yet claim CER, WER, or table accuracy; the final `build-ground-truth` and `evaluate` steps remain necessary after review is complete.

## 2. Problem and objectives

The input is a scanned, image-based PDF containing Russian text and difficult tabular material. The document is not a reliable machine-readable source: recognition can be affected by scan quality, Cyrillic/Latin look-alikes, decimals, formula notation, sparse table rules, and merged headers.

The project objectives were to:

1. Render every PDF page at a stable resolution and preserve the source document unchanged.
2. Extract Russian/English OCR with word-level confidence and page coordinates.
3. Produce reproducible train/validation manifests and conservative candidate CSV tables.
4. Detect suspect OCR automatically instead of manually transcribing the full document.
5. Treat complex tables as explicitly structured grids rather than relying on whitespace alone.
6. Provide a lightweight local interface for a person to accept, correct, or skip doubtful values.
7. Keep raw OCR, automatic proposals, silver labels, and human gold labels separate.
8. Evaluate only against human-verified ground truth, with clear error analysis.

## 3. Input and scope

| Item | Detail |
| --- | --- |
| Source PDF | `8003_Report with tables.pdf` |
| PDF pages | 13 |
| Source size | 5.5 MB |
| Original page dimensions | 1852 × 2598 points |
| Rendered page dimensions | 7717 × 10825 pixels |
| OCR languages | Russian and English (`rus+eng`) |
| Main OCR engine | Local Tesseract |
| Baseline page segmentation mode | PSM 6 |
| Rendering resolution | 300 DPI |

The scope is data extraction and verification, not semantic interpretation of the report. The system preserves the original scan and its raw OCR rather than silently replacing it with cleaned or corrected text.

## 4. End-to-end workflow

```text
Scanned PDF
  -> PNG page images
  -> immutable baseline OCR (PSM 6)
  -> conservative table candidates + train/validation manifests
  -> deterministic preprocessing + alternative OCR (PSM 3)
  -> validation rules and review queue
  -> explicit table-cell OCR and crops
  -> optional multi-pass silver labels
  -> human accept/correct/skip decisions
  -> merged human gold ground truth
  -> reproducible accuracy and error evaluation
```

This separation is deliberate. A later process can always inspect the initial Tesseract prediction, compare it with alternative OCR, and see whether a human changed it.

## 5. Work completed

### 5.1 PDF rendering and baseline OCR

The extractor renders the 13 PDF pages to PNG images and runs Tesseract using `rus+eng` and PSM 6. The output is saved page by page under `output/scanned_report/raw_ocr/`. Each recognised word includes:

- the raw OCR text;
- a confidence score scaled from 0 to 1;
- a normalised bounding box (`x`, `y`, `width`, `height`);
- line grouping information derived from the word boxes.

Bounding boxes use a bottom-left coordinate origin, which is convenient for consistent document geometry. The baseline OCR corpus contains 271 recognised lines, 1,971 baseline OCR tokens, 9,725 OCR characters, and a mean token confidence of 0.6531. These figures describe OCR output volume and confidence only; they are not accuracy measurements.

Raw OCR is treated as immutable. The pipeline writes verification data to separate folders and never overwrites `raw_ocr/` with normalised or corrected text.

### 5.2 Reproducible dataset split and initial table candidates

The extractor writes document/page manifests and uses a seeded 80/20 page-level split. With seed 42, the pages are split as follows:

| Split | Pages |
| --- | --- |
| Training | 1, 2, 4, 5, 6, 9, 10, 11, 12, 13 |
| Validation | 3, 7, 8 |

The initial table extractor groups aligned OCR words into conservative table candidates. It generated four candidate CSV files, on pages 1, 5, 11, and 13. This extraction is intentionally cautious: it does not invent values when scanned rules, merged headers, or faint cells make geometry uncertain.

Important limitation: for a future collection containing multiple reports, splitting should be performed by report rather than by page. A page-level split can leak repeated templates or styles from a single report into both training and validation data.

### 5.3 Image preprocessing and alternative OCR

The verification pipeline makes a deterministic preprocessed copy of every page without changing its dimensions. The transformation sequence is:

1. grayscale conversion;
2. auto-contrast with a 1% cutoff;
3. 1.12× contrast enhancement;
4. unsharp masking (radius 1.0, 120%, threshold 4).

The preprocessed page is processed separately with Tesseract PSM 3. The alternative OCR is never substituted automatically for raw OCR; it is preserved as a competing candidate and compared by overlapping bounding boxes.

For review crops where no page-level alternative is available, the pipeline may run an isolated crop OCR pass in PSM 8. Additional PSM 6/11 page variants and specialised numeric-cell OCR variants are produced by the silver-label stage.

### 5.4 Automatic validation and prioritised review queue

Automatic validation identifies records that need human attention. The rules flag, among other conditions:

- low OCR confidence;
- disagreement between baseline and alternative OCR;
- missing baseline OCR where an alternative exists;
- mixed Cyrillic and Latin look-alike characters;
- unexpected characters or high OCR-noise ratios;
- decimal points in Russian-style numeric values;
- malformed values in table cells.

The review queue prioritises severe cases first. A record is automatically marked `auto_verified` only when it has no validation warnings, high confidence, and no contradictory alternative. Such records remain operational predictions—not human ground truth.

The present review scope is deliberately focused:

- ordinary text: suspicious items from validation pages 3, 7, and 8;
- tables: all suspicious cells from pages 11 and 13.

This strategy reduces review effort while preserving records across the whole document for later analysis.

### 5.5 Explicit handling of complex tables

The most difficult tables are on pages 11 and 13. Instead of relying on whitespace grouping, the project defines stable normalised grid geometry for each page:

| Page | Grid shape | Cell records |
| --- | ---: | ---: |
| 11 | 17 rows × 6 columns | 102 |
| 13 | 12 rows × 9 columns | 108 |
| Total | — | 210 |

Every cell is assigned its own bounding box, crop, raw OCR proposal, isolated-cell OCR proposal, confidence values, normalised proposal, row/column coordinates, validation reasons, and later verified value. This gives 100% coordinate coverage for the expected 210 table cells.

For known formula positions, the silver-label logic can apply a conservative fixed-schema correction. Numeric cells use constrained OCR variants with an English language model and a numeric whitelist (`0123456789,.-+`), plus Russian decimal validation. The raw OCR text is still kept intact even when a normalised or silver proposal is available.

### 5.6 Local human-review interface

A dependency-free local web application was implemented in `review_server.py`. Each queued item shows:

- page number, item type, and table row/column where relevant;
- a cropped image of the original evidence;
- raw OCR and alternative OCR candidates;
- confidence values and automatic flag reasons;
- a populated correction field;
- Accept, Correct, and Skip actions.

Keyboard shortcuts are `A` for accept, `C` to enter a correction, and `S` to skip. Each decision is written as an append-only JSONL record with a UTC timestamp. This preserves the audit trail and avoids silently rewriting prior decisions.

### 5.7 Silver-label generation

The project also includes an optional silver-data stage. It combines several independent OCR candidates: baseline page PSM 6, preprocessed page PSM 3/6/11, isolated-cell PSM 8, and specialised numeric-cell variants. A silver value is accepted only for high-confidence clean words, agreement between two or more passes, fixed-schema formula positions, or tightly validated numeric cells. Otherwise, the process abstains.

Current silver records total 1,932. Of these, 536 are accepted as silver labels (27.7% coverage) and 1,396 are abstained. Accepted records consist of 63 high-confidence words, 434 consensus cases, and 39 conservative rule-corrected table values. These labels are explicitly marked **silver, not gold** and must not be reported as final accuracy.

### 5.8 Ground truth and evaluation implementation

The ground-truth builder merges original records, silver information, and the latest human decisions. Only `human_verified` entries form the gold dataset. The evaluator is designed to calculate:

- character error rate (CER);
- word error rate (WER);
- alternative-OCR CER and absolute CER improvement;
- table-cell exact-match accuracy;
- numeric-value accuracy;
- row/column structure accuracy;
- error categories: substitutions, missing/extra characters, digit/letter confusion, Cyrillic/Latin confusion, punctuation errors, and table segmentation errors.

The evaluator deliberately returns null accuracy values when no human gold data is present. This prevents confidence, agreement, or silver coverage from being misrepresented as OCR accuracy.

## 6. Current quantitative status

The figures below are calculated from the current primary record and review files.

| Measure | Current result | Interpretation |
| --- | ---: | --- |
| Traceable records | 1,932 | 1,722 word records + 210 table cells |
| Needs review | 634 | All flagged records, including out-of-scope training-page text |
| Raw/unverified records | 594 | Not queued or not auto-verified |
| Auto-verified records | 704 | Operationally clean/high-confidence; not gold |
| Current review queue | 204 | 73 word items + 131 table cells |
| Manual-review reduction | 89.4% | Queue versus all traceable records |
| Queue priority 1 | 130 | Severe validation conditions |
| Queue priority 2 | 25 | Table-oriented lower-severity cases |
| Queue priority 3 | 49 | Ordinary-text lower-severity cases |
| Records with an alternative OCR value | 141 | Candidate evidence, not accuracy |
| Normalised candidate agreement | 11 of 141 (7.8%) | A diagnostic signal only |
| Table structure coverage | 210 of 210 (100%) | Expected row/column coordinates are represented |
| Human decisions recorded | 11 | 10 corrections and 1 acceptance |

The most frequent review reasons are low confidence (140 queue items), unavailable alternative OCR (72), OCR-engine disagreement (71), malformed table values (45), OCR noise (20), unexpected characters (18), and missing raw OCR (16). A small number of mixed-script and decimal-format warnings are also present.

## 7. Validation and test evidence

The automated test suite was run successfully with:

```bash
python3 -m unittest -v test_verification_pipeline.py
```

All 8 tests passed. The tests cover:

- semantic-preserving normalisation, including Russian decimals, chemical formulas, and the `№` symbol;
- validation-rule detection;
- edit-distance and error-category logic;
- silver-label consensus and abstention behaviour;
- conservative table-specific rules;
- stable table-grid dimensions;
- append-only review decisions;
- evaluation based strictly on human-verified records.

This confirms the core workflow and guardrails behave as intended. It does not establish OCR accuracy, which requires completed human verification.

## 8. Deliverables and file structure

| Deliverable | Location | Purpose |
| --- | --- | --- |
| Source PDF | `8003_Report with tables.pdf` | Original scanned document |
| OCR extractor | `extract_scanned_report.py` | Rendering, OCR, split manifests, initial tables |
| Verification pipeline | `verification_pipeline.py` | Preprocessing, validation, tables, silver data, ground truth, evaluation |
| Review application | `review_server.py` | Local human-review UI |
| Tests | `test_verification_pipeline.py` | Automated functional checks |
| Usage guide | `README.md` | Commands and workflow documentation |
| Page images | `output/scanned_report/images/` | Rendered source pages |
| Immutable baseline OCR | `output/scanned_report/raw_ocr/` | Raw text, boxes, confidences |
| Split manifests | `output/scanned_report/splits/` | Reproducible train/validation pages |
| Candidate tables | `output/scanned_report/tables/` | Conservative first-pass CSVs |
| Verification records | `output/scanned_report/verification/records/` | Word and table-cell records |
| Review queue and decisions | `output/scanned_report/verification/review/` | Human-review evidence and audit log |
| Structured table outputs | `output/scanned_report/verification/tables/` | Cell grids and reconstructed CSVs |
| Silver labels | `output/scanned_report/verification/silver/` | Multi-pass, non-gold proposals |
| Gold data | `output/scanned_report/verification/ground_truth/` | Human-only labels and table exports |
| Evaluation reports | `output/scanned_report/verification/evaluation/` | Reproducible accuracy output |

## 9. Current limitations and data-freshness note

The implementation is complete, but the final quality measurement is not complete. This distinction should be stated plainly in any presentation.

1. **Human verification is incomplete.** The review queue currently contains 204 items. Only human-confirmed records can be used as gold ground truth.
2. **Final accuracy is pending.** The current gold file contains zero merged records because it predates the 11 decisions now present in the append-only decision log. As a result, the stored evaluation report correctly remains in an `awaiting_human_verified_ground_truth` state.
3. **Derived artifacts need one final refresh.** The primary records were refreshed after some downstream files were created. Before publishing final metrics, rebuild ground truth and rerun evaluation so all derived reports reflect the latest records and decisions.
4. **Page-level splitting is only a provisional evaluation setup.** It is reproducible, but document-level splitting is safer for a larger multi-report dataset.
5. **Silver labels are not final labels.** Consensus, confidence, and schema rules improve operational coverage, but their quality must be measured against human gold before use in a model-training or accuracy claim.

These limitations are responsible reporting, not failures of the pipeline. The system was specifically designed to make them visible and fixable.

## 10. Recommended finalisation procedure

1. Start the review interface and finish the remaining review items:

   ```bash
   python3 review_server.py --source output/scanned_report --port 8765
   ```

2. Rebuild the human-ground-truth files from the append-only decisions:

   ```bash
   python3 verification_pipeline.py build-ground-truth --source output/scanned_report
   ```

3. Calculate the final evidence-backed metrics:

   ```bash
   python3 verification_pipeline.py evaluate --source output/scanned_report
   ```

4. Present the final values from `output/scanned_report/verification/evaluation/report.md` and `report.json`. Do not substitute review-reduction, confidence, alternative agreement, or silver coverage for accuracy.

5. If retraining or expansion is planned, collect multiple reports and split by document, then use the verified gold dataset for validation and model assessment.

## 11. Presentation-ready speaking notes

### Opening (about 30 seconds)

“The project converts a 13-page scanned Russian report into traceable digital data. The key point is that we do not treat OCR as automatically correct. Every OCR prediction keeps its original image location and confidence, and difficult values are sent to a human review process.”

### Method (about 45 seconds)

“We rendered every page at 300 DPI, ran Russian-English Tesseract OCR, and preserved the raw output. Then we generated a preprocessed alternative OCR pass, compared the candidates, and used validation rules for low confidence, disagreement, mixed Cyrillic-Latin letters, numerical formatting, and noisy table cells. For the two complex tables, we created explicit grids: 102 cells on page 11 and 108 cells on page 13.”

### Results (about 45 seconds)

“The system now contains 1,932 traceable records. Instead of checking everything manually, the reviewer receives 204 high-risk records—a reduction of 89.4% in immediate manual work. All 210 expected table cells are structurally represented. We also built a local review interface and verified the code with eight passing automated tests.”

### Honest status and next step (about 30 seconds)

“We intentionally do not report OCR accuracy yet because the final accuracy metrics must come from human-verified ground truth. Eleven decisions have been recorded, but the remaining queue must be reviewed, then we rebuild the gold dataset and calculate CER, WER, and table accuracy. This keeps the final result scientifically defensible.”

## 12. Likely questions and concise answers

**Why not just trust high-confidence OCR?**  
Confidence measures the engine’s internal certainty, not proven correctness. The project therefore separates automatic confidence from human gold labels.

**Why is manual review still needed?**  
The source is scanned and contains tables, numbers, formulas, and Cyrillic/Latin look-alikes. These are precisely the cases where silent OCR errors are costly.

**What is the biggest achieved benefit?**  
The pipeline makes every result traceable and reduces immediate review from 1,932 records to 204 prioritised items while preserving all original evidence.

**Can the output be used for model training?**  
Yes, after human review is completed. Only the `human_verified` subset should be treated as gold data; silver labels can be used cautiously and separately.

**What proves that the tables were not lost?**  
Pages 11 and 13 have explicit row/column grids totaling 210 expected cells, and all 210 are represented in the structured output.

## 13. Conclusion

The project has delivered a robust, auditable OCR-verification workflow rather than a one-time text dump. It preserves the source and raw OCR, identifies uncertainty, supports human correction, reconstructs difficult tables explicitly, and prevents unverified outputs from being presented as truth. The software implementation and automated processing are complete; the remaining operational task is to complete human review, refresh the derived ground-truth artifacts, and publish the final accuracy metrics.

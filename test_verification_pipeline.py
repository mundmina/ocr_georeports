import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from review_server import ReviewState
from verification_pipeline import (
    TABLE_SPECS,
    build_ground_truth,
    decide_silver,
    edit_distance,
    error_categories,
    evaluate,
    normalize_value,
    numeric_candidate,
    validation_reasons,
)


class VerificationPipelineTests(unittest.TestCase):
    def test_normalization_preserves_raw_semantics(self):
        self.assertEqual(normalize_value("76.40"), "76,40")
        self.assertEqual(normalize_value("СаО"), "CaO")
        self.assertEqual(normalize_value("№ 735"), "№ 735")
        self.assertEqual(normalize_value("  обычный   текст "), "обычный текст")

    def test_validation_rules(self):
        self.assertIn("cyrillic_latin_mix", validation_reasons("CаO", 0.95))
        self.assertIn("malformed_table_value", validation_reasons("0,2g", 0.8, table_cell=True))
        self.assertEqual(validation_reasons("", 0.0, "", 0.0, table_cell=True), [])

    def test_metrics_and_error_types(self):
        distance, errors = edit_distance("кот1", "китI")
        self.assertEqual(distance, 2)
        self.assertEqual(error_categories(errors)["digit_letter_confusion"], 1)

    def test_silver_consensus_and_abstention(self):
        word = {"type": "word", "page": 3, "row": None, "column": None, "raw_ocr": "текст"}
        consensus = decide_silver(word, [
            {"source": "psm6", "value": "текст", "confidence": 0.88},
            {"source": "psm11", "value": "текст", "confidence": 0.91},
        ])
        self.assertEqual(consensus["silver_status"], "silver_consensus")
        self.assertEqual(decide_silver(word, [
            {"source": "psm6", "value": "текст", "confidence": 0.88},
        ])["silver_status"], "abstained")

    def test_table_rules_are_conservative(self):
        formula_cell = {"type": "table_cell", "page": 11, "row": 2, "column": 0, "raw_ocr": "SiO,"}
        result = decide_silver(formula_cell, [
            {"source": "cell_psm7", "value": "SiO,", "confidence": 0.8},
        ])
        self.assertEqual(result["silver_status"], "silver_rule_corrected")
        self.assertEqual(result["silver_value"], "SiO2")
        self.assertEqual(numeric_candidate("(76.40)"), "76,40")

        numeric_cell = {"type": "table_cell", "page": 13, "row": 4, "column": 3, "raw_ocr": "0,08"}
        single_pass = decide_silver(numeric_cell, [
            {"source": "page_psm6", "value": "0,08", "confidence": 0.99},
        ])
        self.assertEqual(single_pass["silver_status"], "abstained")
        cross_context = decide_silver(numeric_cell, [
            {"source": "original_page_psm6", "value": "0,08", "confidence": 0.80},
            {"source": "numeric_original_psm6", "value": "0.08", "confidence": 0.82},
        ])
        self.assertEqual(cross_context["silver_status"], "silver_consensus")

    def test_table_specs_have_stable_shapes(self):
        shapes = {
            page: (len(spec["y_edges"]) - 1, len(spec["x_edges"]) - 1)
            for page, spec in TABLE_SPECS.items()
        }
        self.assertEqual(shapes, {11: (17, 6), 13: (12, 9)})

    def test_review_decisions_are_append_only(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            review = root / "review"
            review.mkdir()
            item = {"id": "p001-w0001", "raw_ocr": "текст"}
            (review / "queue.jsonl").write_text(json.dumps(item, ensure_ascii=False) + "\n", encoding="utf-8")
            (review / "decisions.jsonl").write_text("", encoding="utf-8")
            state = ReviewState(root)
            decision = state.save({"id": item["id"], "action": "correct", "corrected_text": "тест"})
            self.assertEqual(decision["verified_value"], "тест")
            self.assertEqual(len((review / "decisions.jsonl").read_text().splitlines()), 1)

    def test_evaluation_uses_human_verified_truth(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            verification = source / "verification"
            gold = {
                "id": "p001-w0001", "type": "word", "raw_ocr": "кот",
                "alternative_ocr": "кит", "verified_value": "кит",
                "review_status": "human_verified",
            }
            records = verification / "records"
            ground_truth = verification / "ground_truth"
            review = verification / "review"
            records.mkdir(parents=True); ground_truth.mkdir(); review.mkdir()
            line = json.dumps(gold, ensure_ascii=False) + "\n"
            (records / "words.jsonl").write_text(line, encoding="utf-8")
            (records / "table_cells.jsonl").write_text("", encoding="utf-8")
            (ground_truth / "gold.jsonl").write_text(line, encoding="utf-8")
            (review / "queue.jsonl").write_text(line, encoding="utf-8")
            evaluate(SimpleNamespace(source=source))
            report = json.loads((verification / "evaluation" / "report.json").read_text())
            self.assertEqual(report["status"], "evaluated")
            self.assertAlmostEqual(report["ocr"]["cer"], 1 / 3)
            self.assertEqual(report["ocr"]["alternative_cer"], 0)

    def test_stale_table_geometry_decision_is_not_gold(self):
        with tempfile.TemporaryDirectory() as temp:
            source = Path(temp)
            verification = source / "verification"
            records = verification / "records"
            review = verification / "review"
            records.mkdir(parents=True)
            review.mkdir()
            cell = {
                "id": "p013-r01-c01", "type": "table_cell", "page": 13,
                "row": 1, "column": 1, "record_version": "table_geometry_v3",
                "bbox": {"x": 0.1, "y": 0.1, "width": 0.1, "height": 0.1},
                "raw_ocr": "6,59", "normalized_value": "6,59",
                "verified_value": None, "review_status": "needs_review",
            }
            (records / "words.jsonl").write_text("", encoding="utf-8")
            (records / "table_cells.jsonl").write_text(
                json.dumps(cell, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            stale = {
                "id": cell["id"], "action": "accept", "verified_value": "6,59",
                "record_version": "table_geometry_v2", "reviewed_at": "2026-01-01T00:00:00Z",
            }
            (review / "decisions.jsonl").write_text(
                json.dumps(stale, ensure_ascii=False) + "\n", encoding="utf-8"
            )
            build_ground_truth(SimpleNamespace(source=source))
            gold = (verification / "ground_truth" / "gold.jsonl").read_text(encoding="utf-8")
            self.assertEqual(gold, "")


if __name__ == "__main__":
    unittest.main()

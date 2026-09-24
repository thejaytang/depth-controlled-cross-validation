"""Behavior checks using synthetic PDF pages."""

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from reportlab.pdfgen import canvas

from pdf_validator.evidence import Observation
from pdf_validator.input import InputError
from pdf_validator.validate import validate



def sample_pdf(path: Path) -> dict:
    pdf = canvas.Canvas(str(path), pagesize=(612, 792))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(60, 720, "Policy")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(60, 670, "Dose must not exceed 10 mg.")
    for x in (60, 160, 260):
        pdf.line(x, 520, x, 600)
    for y in (520, 560, 600):
        pdf.line(60, y, 260, y)
    for x, y, label in ((70, 575, "A"), (170, 575, "B"),
                        (70, 535, "1"), (170, 535, "2")):
        pdf.drawString(x, y, label)
    pdf.drawString(300, 30, "1")
    pdf.save()
    return {"document_type": "general", "printed_page_label": None,
            "pages": [{"page_index": 0, "printed_page_label": "1", "blocks": [
                {"id": "h", "type": "heading", "text": "Policy", "bbox": [55, 60, 160, 82],
                 "heading_level": 1, "order": 0},
                {"id": "p", "type": "paragraph", "text": "Dose must not exceed 10 mg.",
                 "bbox": [55, 105, 260, 132], "parent_id": "h", "order": 1},
                {"id": "t", "type": "table", "bbox": [60, 192, 260, 272], "order": 2,
                 "cells": [
                     {"id": "a", "row": 0, "column": 0, "text": "A", "bbox": [60, 192, 160, 232]},
                     {"id": "b", "row": 0, "column": 1, "text": "B", "bbox": [160, 192, 260, 232]},
                     {"id": "one", "row": 1, "column": 0, "text": "1", "bbox": [60, 232, 160, 272]},
                     {"id": "two", "row": 1, "column": 1, "text": "2", "bbox": [160, 232, 260, 272]},
                 ]},
                {"id": "n", "type": "page_number", "text": "1", "bbox": [285, 742, 320, 772]},
            ]}]}


class ValidatorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.pdf = self.root / "source.pdf"
        self.data = sample_pdf(self.pdf)
        self.json_path = self.root / "input.json"

    def run_validation(self, depth: int = 2) -> dict:
        self.json_path.write_text(json.dumps(self.data), encoding="utf-8")
        return validate(self.pdf, self.json_path, depth)

    def test_critical_number_difference_has_two_evidence_families(self) -> None:
        self.data["pages"][0]["blocks"][1]["text"] = "Dose must not exceed 100 mg."
        report = self.run_validation()
        matches = [f for f in report["findings"] if f["kind"] == "critical_text_difference"]
        self.assertTrue(matches, report["findings"])
        self.assertEqual(matches[0]["upstream_ids"], ["p"])
        self.assertEqual(matches[0]["disposition"], "failed")
        self.assertEqual(report["document"]["status"], "failed")
        self.assertIsNone(report["document"]["calibrated_correctness"])
        self.assertEqual(set(report["pages"][0]["evidence_families"]),
                         {"embedded_text", "visual_ocr"})

    def test_small_changes_inside_long_blocks_are_found(self) -> None:
        lines = [
            "The monthly control record confirms that every sample is checked before the laboratory result is approved: 1,500 units.",
            "The auditor shall inspect submitted reports and retain the approved copy for the annual review cycle.",
        ]
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        pdf.setFont("Helvetica", 8)
        pdf.drawString(60, 700, lines[0])
        pdf.drawString(60, 660, lines[1])
        pdf.save()
        self.data = {"pages": [{"page_index": 0, "blocks": [
            {"id": "number", "type": "paragraph", "text": lines[0].replace("1,500", "15,000"),
             "bbox": [55, 80, 560, 110]},
            {"id": "word", "type": "paragraph", "text": lines[1].replace("reports", "records"),
             "bbox": [55, 120, 560, 150]},
        ]}]}
        report = self.run_validation(depth=1)
        kinds = {(f["upstream_ids"][0], f["kind"]) for f in report["findings"]
                 if f["upstream_ids"]}
        self.assertIn(("number", "critical_text_difference"), kinds)
        self.assertIn(("word", "text_difference"), kinds)

    def test_correct_control_page_passes(self) -> None:
        report = self.run_validation(depth=3)
        self.assertEqual(report["document"]["status"], "pass")
        self.assertEqual(report["document"]["overall_support_score"], 100.0)
        self.assertFalse(report["findings"])
        self.assertTrue(report["upstream_lineage_unknown"])

    def test_omitted_pdf_region_is_located(self) -> None:
        self.data["pages"][0]["blocks"].pop(1)
        report = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "pdf_region_missing_from_json" and f["bbox"]
                            for f in report["findings"]))

    def test_substantive_top_margin_text_is_not_skipped(self) -> None:
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        pdf.drawString(60, 760, "Important scope limitation")
        pdf.save()
        self.data = {"pages": [{"page_index": 0, "blocks": []}]}
        report = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "pdf_region_missing_from_json" and
                            "Important scope limitation" in str(f["evidence"])
                            for f in report["findings"]))

    def test_repeated_margin_decoration_is_not_called_omitted(self) -> None:
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        for _ in range(2):
            pdf.drawString(60, 760, "Confidential")
            pdf.showPage()
        pdf.save()
        self.data = {"pages": [{"page_index": i, "blocks": []} for i in range(2)]}
        report = self.run_validation(depth=1)
        self.assertFalse(any(f["kind"] == "pdf_region_missing_from_json"
                             for f in report["findings"]))

    def test_table_wrong_cell_and_geometry_are_located(self) -> None:
        table = self.data["pages"][0]["blocks"][2]
        table["cells"][2]["text"] = "9"
        table["cells"][2]["bbox"] = [160, 232, 260, 272]
        table["cells"][3]["bbox"] = [60, 232, 160, 272]
        report = self.run_validation()
        kinds = {f["kind"] for f in report["findings"]}
        self.assertIn("cell_content_difference", kinds)
        self.assertIn("cell_row_column_conflict", kinds)
        self.assertTrue(any(f["upstream_ids"] == ["t", "one"]
                            for f in report["findings"] if f["kind"] == "cell_content_difference"))

    def test_unlocated_table_cells_are_checked_as_one_region(self) -> None:
        cells = self.data["pages"][0]["blocks"][2]["cells"]
        for cell in cells:
            cell.pop("bbox")
        report = self.run_validation(depth=1)
        table_findings = [f for f in report["findings"] if f["dimension"] == "tables"]
        self.assertEqual([f["kind"] for f in table_findings], ["table_cells_unlocated"])
        self.assertNotEqual(report["document"]["status"], "failed")
        cells[2]["text"] = "9"
        report = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "table_critical_token_difference"
                            for f in report["findings"]))
        cells[2]["text"] = "1"
        cells[0]["text"] = "C"
        report = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "table_aggregate_text_difference"
                            for f in report["findings"]))

    def test_table_rows_can_be_checked_without_upstream_cell_boxes(self) -> None:
        cells = self.data["pages"][0]["blocks"][2]["cells"]
        for cell in cells:
            cell.pop("bbox")
        clean = self.run_validation(depth=3)
        self.assertEqual(clean["pages"][0]["status"], "pass")
        cells[2]["column"], cells[3]["column"] = 1, 0
        changed = self.run_validation(depth=3)
        rows = [f for f in changed["findings"] if f["kind"] == "table_row_alignment_disagreement"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["evidence"]["row"], 1)
        self.assertIsNotNone(rows[0]["bbox"])

    def test_duplicate_table_coordinate_without_boxes_fails(self) -> None:
        cells = self.data["pages"][0]["blocks"][2]["cells"]
        for cell in cells:
            cell.pop("bbox")
        cells[3]["column"] = 0
        report = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "duplicate_cell_coordinate" and
                            f["disposition"] == "failed" for f in report["findings"]))

    def test_duplicate_text_inside_table_is_flagged(self) -> None:
        self.data["pages"][0]["blocks"].append(
            {"id": "dup", "type": "paragraph", "text": "A B 1 2",
             "bbox": [62, 198, 258, 268]})
        report = self.run_validation(depth=1)
        duplicates = [f for f in report["findings"] if f["kind"] == "duplicate_table_content"]
        self.assertEqual(len(duplicates), 1)
        self.assertEqual(duplicates[0]["upstream_ids"], ["t", "dup"])

    def test_shared_upstream_reader_is_replaced(self) -> None:
        self.data["parser_engines"] = ["pdftotext"]
        report = self.run_validation(depth=1)
        self.assertEqual(report["pages"][0]["effective_methods"], ["tesseract"])
        self.assertEqual(report["pages"][0]["evidence_families"], ["visual_ocr"])

    def test_shared_text_layer_does_not_confirm_critical_error(self) -> None:
        self.data["parser_engines"] = ["pdfplumber"]
        self.data["pages"][0]["blocks"][1]["text"] = "Dose must not exceed 100 mg."
        report = self.run_validation(depth=2)
        finding = next(f for f in report["findings"] if f["kind"] == "critical_text_difference")
        self.assertEqual(finding["disposition"], "review_required")
        self.assertTrue(report["pages"][0]["methods"][0]["family_overlaps_upstream"])

    def test_structure_and_page_number_pollution(self) -> None:
        blocks = self.data["pages"][0]["blocks"]
        blocks[1].update(type="heading", heading_level=3, parent_id="missing")
        blocks[3]["type"] = "paragraph"
        report = self.run_validation(depth=1)
        kinds = {f["kind"] for f in report["findings"]}
        self.assertIn("invalid_parent_relation", kinds)
        self.assertIn("heading_level_jump", kinds)
        self.assertIn("page_number_in_body", kinds)

    def test_missing_reading_order_is_explicitly_unverified(self) -> None:
        for block in self.data["pages"][0]["blocks"]:
            block.pop("order", None)
        report = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "reading_order_unverified" for f in report["findings"]))
        self.assertEqual(report["pages"][0]["status"], "review_required")

    def test_unpositioned_source_text_cannot_prove_pdf_coverage(self) -> None:
        self.data["pages"][0]["blocks"] = []
        self.json_path.write_text(json.dumps(self.data), encoding="utf-8")
        observation = Observation("pdfium", "embedded_text", 0, "ok", text="Visible PDF text")
        with patch("pdf_validator.validate.read_page", return_value=[observation]):
            report = validate(self.pdf, self.json_path, 1)
        self.assertTrue(any(f["kind"] == "pdf_coverage_unverified" for f in report["findings"]))
        self.assertNotEqual(report["pages"][0]["status"], "pass")

    def test_unpositioned_upstream_text_is_not_called_omitted(self) -> None:
        paragraph = self.data["pages"][0]["blocks"][1]
        paragraph.pop("bbox")
        report = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "pdf_region_position_unverified" and
                            "p" in f["upstream_ids"] for f in report["findings"]))
        self.assertFalse(any(f["kind"] == "pdf_region_missing_from_json" and
                             "Dose must not exceed" in str(f["evidence"])
                             for f in report["findings"]))

    def test_page_of_total_footer_is_kept_out_of_body(self) -> None:
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        pdf.drawString(280, 30, "Page 1 of 30")
        pdf.save()
        self.data = {"pages": [{"page_index": 0, "blocks": []}]}
        missing = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "printed_page_number_not_separated"
                            for f in missing["findings"]))
        self.data["pages"][0]["blocks"] = [
            {"id": "bad-body", "type": "paragraph", "text": "Page 1 of 30",
             "bbox": [270, 744, 360, 770]}]
        polluted = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "page_number_in_body" for f in polluted["findings"]))
        self.data["pages"][0]["printed_page_label"] = "2"
        mismatched = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "printed_page_label_mismatch"
                            for f in mismatched["findings"]))

    def test_page_number_in_header_is_not_body(self) -> None:
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        pdf.drawString(280, 760, "Page 1 of 30")
        pdf.save()
        self.data = {"pages": [{"page_index": 0, "blocks": [
            {"id": "bad-body", "type": "paragraph", "text": "Page 1 of 30",
             "bbox": [270, 20, 360, 45]}]}]}
        polluted = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "page_number_in_body" for f in polluted["findings"]))
        self.data["pages"][0]["blocks"][0]["type"] = "page_number"
        separated = self.run_validation(depth=1)
        self.assertFalse(any(f["kind"] in {"page_number_in_body", "printed_page_number_not_separated"}
                             for f in separated["findings"]))

    def test_formula_and_vector_figure_remain_unverified(self) -> None:
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        pdf.drawString(60, 700, "E = mc2")
        pdf.rect(60, 590, 120, 60)
        pdf.save()
        self.data = {"pages": [{"page_index": 0, "blocks": [
            {"id": "e", "type": "equation", "text": "E = mc2", "bbox": [55, 80, 180, 110]},
            {"id": "f", "type": "figure", "bbox": [60, 142, 180, 202]},
        ]}]}
        report = self.run_validation(depth=3)
        kinds = {f["kind"] for f in report["findings"]}
        self.assertIn("formula_semantics_unverified", kinds)
        self.assertIn("figure_presence_unverified", kinds)
        self.assertIsNone(report["document"]["calibrated_correctness"])

    def test_no_text_evidence_does_not_pass(self) -> None:
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        pdf.showPage()
        pdf.save()
        report = self.run_validation(depth=1)
        self.assertEqual(report["pages"][0]["effective_depth"], 0)
        self.assertNotEqual(report["document"]["status"], "pass")
        self.assertTrue(any(f["kind"] == "requested_depth_unmet" for f in report["findings"]))

    def test_bad_input_type_is_diagnosed(self) -> None:
        self.data["pages"][0]["blocks"][0]["type"] = []
        with self.assertRaisesRegex(InputError, "unsupported block type"):
            self.run_validation(depth=1)

    def test_declared_source_hash_must_match_pdf(self) -> None:
        self.data["source_sha256"] = "0" * 64
        with self.assertRaisesRegex(InputError, "does not match"):
            self.run_validation(depth=1)

    def test_swapped_json_pages_are_detected(self) -> None:
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        pdf.drawString(60, 700, "Alpha requires 10 units")
        pdf.showPage()
        pdf.drawString(60, 700, "Beta requires 20 units")
        pdf.save()
        self.data = {"pages": [
            {"page_index": 0, "blocks": [{"id": "a", "type": "paragraph",
                                          "text": "Beta requires 20 units", "bbox": [55, 80, 260, 110]}]},
            {"page_index": 1, "blocks": [{"id": "b", "type": "paragraph",
                                          "text": "Alpha requires 10 units", "bbox": [55, 80, 260, 110]}]},
        ]}
        report = self.run_validation(depth=1)
        self.assertEqual({f["page_index"] for f in report["findings"]
                          if f["kind"] == "critical_text_difference"}, {0, 1})
        self.assertEqual(report["document"]["status"], "review_required")

    def test_missing_page_lowers_document_support(self) -> None:
        pdf = canvas.Canvas(str(self.pdf), pagesize=(612, 792))
        pdf.drawString(60, 700, "Page one")
        pdf.showPage()
        pdf.drawString(60, 700, "Page two")
        pdf.save()
        self.data = {"pages": [{"page_index": 0, "blocks": [
            {"id": "first", "type": "paragraph", "text": "Page one",
             "bbox": [55, 80, 180, 110]}]}]}
        report = self.run_validation(depth=1)
        self.assertTrue(any(f["kind"] == "missing_pdf_page_in_json"
                            for f in report["findings"]))
        self.assertLess(report["document"]["overall_support_score"],
                        report["pages"][0]["overall_support_score"])


if __name__ == "__main__":
    unittest.main()

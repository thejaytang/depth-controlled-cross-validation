"""Evidence-backed checks and page/document support scores (not probabilities)."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from difflib import SequenceMatcher
from hashlib import sha256
from itertools import combinations
from pathlib import Path
import re
import unicodedata

from . import __version__
from .evidence import ALIASES, Observation, page_sizes, read_page, versions
from .input import Block, Box, Document, Page, load_document


DIMENSIONS = ("accuracy", "structure", "tables", "formula_figures", "cleanliness")
WEIGHTS = {
    # Provisional policy, not a calibrated probability model.
    "general": dict(zip(DIMENSIONS, (0.30, 0.20, 0.20, 0.15, 0.15))),
    "financial": dict(zip(DIMENSIONS, (0.30, 0.15, 0.35, 0.10, 0.10))),
    "legal": dict(zip(DIMENSIONS, (0.40, 0.30, 0.10, 0.05, 0.15))),
}
RULES_VERSION = "local-evidence/7"
BODY_KINDS = {"heading", "paragraph", "list_item", "table", "equation", "figure"}
TEXT_KINDS = {"heading", "paragraph", "list_item", "footnote", "header", "footer", "sidebar"}
CRITICAL_WORDS = {"not", "no", "never", "without", "ikke", "ingen", "不得", "不", "禁止"}
KNOWN_ENGINE_NAMES = set().union(*ALIASES.values())


def tokens(text: str) -> list[str]:
    text = unicodedata.normalize("NFKC", text).casefold().replace("−", "-")
    return re.findall(r"[+-]?\d+(?:[.,:/]\d+)*|[^\W\d_]+|[≥≤<>%=]", text, re.UNICODE)


def critical(values: list[str]) -> list[str]:
    return [v for v in values if any(c.isdigit() for c in v) or v in CRITICAL_WORDS
            or v in {"≥", "≤", ">", "<", "%", "="}]


def similarity(expected: str, actual: str) -> float:
    a, b = tokens(expected), tokens(actual)
    if not a and not b:
        return 1.0
    if not a or not b:
        return 0.0
    common = sum((Counter(a) & Counter(b)).values())
    bag = 2 * common / (len(a) + len(b))
    order = SequenceMatcher(None, a, b, autojunk=False).ratio()
    return 0.8 * bag + 0.2 * order


@dataclass
class Score:
    total: float = 0
    measured: float = 0
    support: float = 0

    def add(self, weight: float, value: float | None) -> None:
        self.total += weight
        if value is not None:
            self.measured += weight
            self.support += weight * min(1, max(0, value))

    def result(self) -> dict:
        if self.total == 0:
            return {"score": None, "coverage": None, "state": "not_applicable"}
        if self.measured == 0:
            return {"score": None, "coverage": 0.0, "state": "unverified"}
        coverage = self.measured / self.total
        return {"score": round(100 * self.support / self.total, 1),
                "coverage": round(coverage, 3),
                "state": "assessed" if coverage >= 0.999 else "partial"}


def _combined(scores: dict[str, dict], weights: dict[str, float]) -> float | None:
    applicable = [(weights[d], scores[d]["score"]) for d in DIMENSIONS
                  if scores[d]["state"] != "not_applicable"]
    if not applicable or not any(score is not None for _, score in applicable):
        return None
    denominator = sum(weight for weight, _ in applicable)
    return round(sum(weight * (score or 0) for weight, score in applicable) / denominator, 1)


def _text_in_box(obs: Observation, box: Box) -> str:
    selected = [w for w in obs.words if box.contains_center(w.box)]
    selected.sort(key=lambda w: (round(w.box.y0 / 5), w.box.x0))
    return " ".join(w.text for w in selected)


def _comparison(expected: str, box: Box | None, observations: list[Observation]) -> tuple[float | None, list[dict], list[str]]:
    if box is None:
        return None, [], ["missing_bbox"]
    candidates = []
    for obs in observations:
        if obs.status != "ok" or not obs.words or obs.shared_with_upstream:
            continue
        actual = _text_in_box(obs, box)
        if actual:
            candidates.append({"method": obs.method, "family": obs.family,
                               "family_overlaps_upstream": obs.family_overlaps_upstream,
                               "text": actual, "similarity": round(similarity(expected, actual), 3)})
    if not candidates:
        return None, [], ["no_independent_positioned_text"]
    # Multiple readers of the same embedded text are one correlated family.
    by_family: dict[str, dict] = {}
    for candidate in candidates:
        family = candidate["family"]
        if family not in by_family or candidate["similarity"] > by_family[family]["similarity"]:
            by_family[family] = candidate
    family_scores = [c["similarity"] for c in by_family.values()]
    # ponytail: a weak OCR read on tiny print must not outweigh a clear text-layer match;
    # replace this ceiling with reliability learned from labeled pages when available.
    support = max(family_scores) if max(family_scores) >= 0.90 else sum(family_scores) / len(family_scores)
    return support, candidates, []


def _difference(expected: str, candidates: list[dict]) -> tuple[str | None, bool]:
    """Find small lexical/critical changes even inside otherwise matching long blocks."""
    representatives: dict[str, dict] = {}
    for candidate in candidates:
        family = candidate["family"]
        if family not in representatives or candidate["similarity"] > representatives[family]["similarity"]:
            representatives[family] = candidate
    expected_critical = tuple(critical(tokens(expected)))

    def critical_change(candidate: dict) -> bool:
        return tuple(critical(tokens(candidate["text"]))) != expected_critical

    def lexical_change(candidate: dict) -> bool:
        actual = candidate["text"]
        compact = lambda value: re.sub(r"\s+", "", unicodedata.normalize("NFKC", value).casefold())
        return tokens(actual) != tokens(expected) and compact(actual) != compact(expected)

    strong = [c for c in representatives.values() if c["similarity"] >= 0.90]
    if strong:
        changed = [c for c in strong if critical_change(c) or lexical_change(c)]
        if changed:
            important = any(critical_change(c) for c in changed)
            if len(changed) < len(strong):
                return ("critical_method_disagreement" if important else "method_disagreement"), False
            corroborating = [tuple(critical(tokens(c["text"]))) for c in changed
                             if not c["family_overlaps_upstream"]]
            confirmed = important and any(a == b for i, a in enumerate(corroborating)
                                              for b in corroborating[i + 1:])
            return ("critical_text_difference" if important else "text_difference"), confirmed
        if any(c["similarity"] >= 0.85 and critical_change(c)
               for c in representatives.values() if c not in strong):
            return "critical_method_disagreement", False
        return None, False

    important = any(critical_change(c) for c in representatives.values())
    corroborating = [tuple(critical(tokens(c["text"]))) for c in representatives.values()
                     if critical_change(c) and not c["family_overlaps_upstream"]]
    confirmed = important and any(a == b for i, a in enumerate(corroborating)
                                  for b in corroborating[i + 1:])
    return ("critical_text_difference" if important else "text_difference"), confirmed


def _status(findings: list[dict], observed: bool) -> str:
    if any(f["disposition"] == "failed" for f in findings):
        return "failed"
    if any(f["disposition"] == "review_required" for f in findings):
        return "review_required"
    return "pass" if observed else "unverifiable"


def validate(pdf_path: Path, json_path: Path, depth: int, language: str = "eng") -> dict:
    if type(depth) is not int or not 1 <= depth <= 5:
        raise ValueError("Depth must be an integer from 1 to 5")
    pdf_path, json_path = Path(pdf_path), Path(json_path)
    pdf_hash, json_hash = sha256(pdf_path.read_bytes()).hexdigest(), sha256(json_path.read_bytes()).hexdigest()
    sizes = page_sizes(pdf_path)
    document = load_document(json_path, sizes, pdf_hash)
    observations = {i: read_page(pdf_path, i, size, depth if i in document.pages else 1,
                                 document.upstream_engines, language)
                    for i, size in enumerate(sizes)}
    all_blocks = {block.id: block for page in document.pages.values() for block in page.blocks}
    repeated_marginals = _repeated_marginals(observations, sizes)
    findings: list[dict] = []
    pages: list[dict] = []
    document_scores = {d: Score() for d in DIMENSIONS}
    for index, size in enumerate(sizes):
        page = document.pages.get(index, Page(index, None, ()))
        obs = observations[index]
        page_findings: list[dict] = []
        scores = {d: Score() for d in DIMENSIONS}

        def issue(dimension: str, kind: str, severity: str, box: Box | None,
                  upstream_ids: list[str], evidence: object, disposition: str = "review_required") -> None:
            item = {"id": f"F{len(findings) + 1:05d}", "dimension": dimension,
                    "kind": kind, "severity": severity, "page_index": index,
                    "bbox": box.as_list() if box else None,
                    "upstream_ids": upstream_ids, "evidence": evidence,
                    "disposition": disposition}
            findings.append(item)
            page_findings.append(item)

        if index not in document.pages:
            reference = _reference(obs)
            missing_weight = (sum(max(1, min(20, len(tokens(line.text))))
                                  for line in reference.lines) if reference else 20)
            missing_weight = max(20, missing_weight)
            scores["accuracy"].add(missing_weight, 0)
            for dimension in DIMENSIONS[1:]:
                scores[dimension].add(missing_weight, None)
            issue("accuracy", "missing_pdf_page_in_json", "critical", None, [],
                  {"pdf_page_index": index}, "failed")

        for block in page.blocks:
            if block.kind in TEXT_KINDS:
                weight = max(1, min(20, len(tokens(block.text))))
                dimension = "accuracy" if block.kind in {"heading", "paragraph", "list_item", "footnote"} else "cleanliness"
                support, candidates, reasons = _comparison(block.text, block.bbox, obs)
                if reasons:
                    scores[dimension].add(weight, support)
                    issue(dimension, "text_not_verifiable", "warning", block.bbox, [block.id], reasons)
                else:
                    kind, confirmed = _difference(block.text, candidates)
                    scores[dimension].add(weight, min(support, 0.5) if kind and kind.endswith("text_difference") else support)
                    if kind:
                        issue(dimension, kind, "critical" if kind == "critical_text_difference" else "major",
                              block.bbox, [block.id], candidates,
                              "failed" if confirmed else "review_required")

        if index in document.pages:
            _coverage(page, obs, size, repeated_marginals, scores, issue)
            _page_text_check(page, obs, scores, issue)
            _structure(page, all_blocks, obs, scores, issue)
            _tables(page, obs, scores, issue)
            _duplicate_table_content(page, scores, issue)
            _formula_figures(page, obs, scores, issue)
            _cleanliness(page, obs, size, repeated_marginals, scores, issue)

        effective = [o.method for o in obs if o.status == "ok" and o.has_evidence()
                     and not o.shared_with_upstream]
        if index in document.pages and len(effective) < depth:
            scores["accuracy"].add(1, None)
            issue("accuracy", "requested_depth_unmet", "warning", None, [],
                  {"requested": depth, "effective": len(effective)})

        result_scores = {d: scores[d].result() for d in DIMENSIONS}
        for d in DIMENSIONS:
            document_scores[d].total += scores[d].total
            document_scores[d].measured += scores[d].measured
            document_scores[d].support += scores[d].support
        pages.append({"page_index": index, "printed_page_label": page.printed_page_label,
                      "requested_depth": depth, "effective_depth": len(effective),
                      "effective_methods": effective,
                      "evidence_families": sorted({o.family for o in obs if o.method in effective}),
                      "dimension_scores": result_scores,
                      "overall_support_score": _combined(result_scores, WEIGHTS[document.document_type]),
                      "calibrated_correctness": None,
                      "status": _status(page_findings, bool(effective)),
                      "finding_ids": [f["id"] for f in page_findings],
                      "methods": [{"name": o.method, "family": o.family, "status": o.status,
                                   "shared_with_upstream": o.shared_with_upstream,
                                   "family_overlaps_upstream": o.family_overlaps_upstream,
                                   "has_evidence": o.has_evidence(), "error": o.error} for o in obs]})
    dimension_scores = {d: document_scores[d].result() for d in DIMENSIONS}
    status = _status(findings, any(p["effective_depth"] for p in pages))
    return {"schema_version": "validation-report/1", "validator_version": __version__,
            "rules_version": RULES_VERSION, "pdf_sha256": pdf_hash, "input_json_sha256": json_hash,
            "requested_depth": depth, "document_type": document.document_type,
            "upstream_engines": list(document.upstream_engines),
            "upstream_lineage_unknown": (not document.upstream_engines or
                                         any(e.strip().lower() not in KNOWN_ENGINE_NAMES
                                             for e in document.upstream_engines)),
            "tool_versions": versions(),
            "document": {"page_count": len(sizes), "dimension_scores": dimension_scores,
                         "overall_support_score": _combined(dimension_scores, WEIGHTS[document.document_type]),
                         "calibrated_correctness": None, "status": status,
                         "review_page_indices": [p["page_index"] for p in pages
                                                 if p["status"] != "pass"]},
            "pages": pages, "findings": findings,
            "review_items": [{"finding_id": f["id"], "page_index": f["page_index"],
                              "bbox": f["bbox"], "upstream_ids": f["upstream_ids"]}
                             for f in findings if f["disposition"] == "review_required"],
            "score_interpretation": "Evidence support only; not a probability of correctness.",
            "business_acceptance": {"critical_miss_rate": "UNMEASURED",
                                    "manual_review_fraction": "UNMEASURED"}}


def _reference(obs: list[Observation]) -> Observation | None:
    for name in ("poppler", "tesseract", "pdfplumber"):
        found = next((o for o in obs if o.method == name and o.words and not o.shared_with_upstream), None)
        if found:
            return found
    return None


def _coverage(page: Page, obs: list[Observation], size: tuple[float, float],
              repeated_marginals: set[str], scores: dict, issue) -> None:
    reference = _reference(obs)
    if reference is None:
        scores["accuracy"].add(5, None)
        issue("accuracy", "pdf_coverage_unverified", "warning", None, [],
              ["no independent positioned source text"])
        return
    for line in reference.lines:
        if not tokens(line.text):
            continue
        marginal = line.box.y0 < size[1] * 0.08 or line.box.y1 > size[1] * 0.93
        if marginal and (_page_label(line.text, page.page_index) or
                         " ".join(tokens(line.text)) in repeated_marginals):
            continue
        covered = any(block.bbox and block.bbox.overlap(line.box) / max(1, line.box.area()) > 0.25
                      for block in page.blocks)
        weight = max(1, min(10, len(tokens(line.text))))
        if covered:
            scores["accuracy"].add(weight, 1)
            continue
        line_tokens = Counter(tokens(line.text))
        unpositioned = next((block for block in page.blocks if block.bbox is None and
                             line_tokens <= Counter(tokens(" ".join(c.text for c in block.cells)
                                                          if block.cells else block.text))), None)
        if unpositioned:
            scores["accuracy"].add(weight, None)
            issue("accuracy", "pdf_region_position_unverified", "warning", line.box,
                  [unpositioned.id], {"method": reference.method, "text": line.text})
        else:
            scores["accuracy"].add(weight, 0)
            issue("accuracy", "pdf_region_missing_from_json", "critical", line.box, [],
                  [{"method": reference.method, "text": line.text}])


def _page_text_check(page: Page, obs: list[Observation], scores: dict, issue) -> None:
    expected = " ".join((" ".join(c.text for c in b.cells) if b.cells else b.text)
                        for b in page.blocks if b.kind != "figure")
    if not expected.strip():
        return
    for observation in obs:
        if observation.method not in {"pdfium", "pypdf"} or observation.shared_with_upstream:
            continue
        weight = max(1, min(8, len(tokens(expected)) / 8))
        if observation.status != "ok":
            scores["accuracy"].add(weight, None)
            continue
        score = similarity(expected, observation.text)
        scores["accuracy"].add(weight, score)
        if score < 0.65:
            issue("accuracy", "whole_page_text_disagreement", "major", None,
                  [b.id for b in page.blocks],
                  {"method": observation.method, "similarity": round(score, 3)})


def _structure(page: Page, all_blocks: dict[str, Block], obs: list[Observation], scores: dict, issue) -> None:
    tables = [b for b in page.blocks if b.kind == "table" and b.bbox]
    structured = [b for b in page.blocks if b.kind in {"heading", "paragraph", "list_item", "footnote"}
                  and not any(b.bbox and table.bbox.contains_center(b.bbox) for table in tables)]
    for block in structured:
        if block.parent_id:
            parent = all_blocks.get(block.parent_id)
            good = parent is not None and parent.kind in {"heading", "list_item"}
            scores["structure"].add(2, 1 if good else 0)
            if not good:
                issue("structure", "invalid_parent_relation", "major", block.bbox,
                      [block.id], {"parent_id": block.parent_id}, "failed")
        if block.kind == "heading":
            scores["structure"].add(1, 1 if block.heading_level is not None else None)
            if block.heading_level is None:
                issue("structure", "heading_level_unverified", "warning", block.bbox, [block.id], {})
    ordered = [b for b in structured if b.order is not None and b.bbox]
    if len(structured) >= 2 and len(ordered) < 2:
        scores["structure"].add(2, None)
        issue("structure", "reading_order_unverified", "warning", None,
              [b.id for b in structured], ["fewer than two positioned ordered blocks"])
    ordered.sort(key=lambda b: b.order)
    for left, right in zip(ordered, ordered[1:]):
        if not left.bbox or not right.bbox:
            continue
        same_column = min(left.bbox.x1, right.bbox.x1) > max(left.bbox.x0, right.bbox.x0)
        if not same_column:
            continue
        good = left.bbox.y0 <= right.bbox.y0 + 3
        scores["structure"].add(1, 1 if good else 0)
        if not good:
            issue("structure", "reading_order_conflict", "major", right.bbox,
                  [left.id, right.id], {"order": [left.order, right.order]})
    headings = [b for b in ordered if b.kind == "heading" and b.heading_level is not None]
    for left, right in zip(headings, headings[1:]):
        good = right.heading_level <= left.heading_level + 1
        scores["structure"].add(1, 1 if good else 0)
        if not good:
            issue("structure", "heading_level_jump", "major", right.bbox,
                  [left.id, right.id], {"levels": [left.heading_level, right.heading_level]})
    plumber = next((o for o in obs if o.method == "pdfplumber" and o.lines
                    and not o.shared_with_upstream), None)
    if plumber:
        for block in structured:
            if block.kind != "heading" or not block.bbox:
                continue
            nearby = [line for line in plumber.lines if block.bbox.contains_center(line.box)]
            body = [line.font_size for line in plumber.lines if line.font_size and
                    not block.bbox.contains_center(line.box)]
            if nearby and body:
                heading_size = max(line.font_size or 0 for line in nearby)
                typical = sorted(body)[len(body) // 2]
                scores["structure"].add(1, 1 if heading_size >= typical or any(l.bold for l in nearby) else 0.5)


def _table_aggregate(expected: str, block: Block, obs: list[Observation], scores: dict, issue) -> None:
    expected_tokens = tokens(expected)
    if not expected_tokens:
        return
    weight = max(4, min(50, len(expected_tokens) / 20))
    _, candidates, reasons = _comparison(expected, block.bbox, obs)
    if reasons:
        scores["tables"].add(weight, None)
        return
    expected_counts = Counter(expected_tokens)
    expected_critical = Counter(critical(expected_tokens))
    by_family: dict[str, dict] = {}
    for candidate in candidates:
        actual_tokens = tokens(candidate["text"])
        actual_counts = Counter(actual_tokens)
        common = sum((expected_counts & actual_counts).values())
        bag = 2 * common / max(1, len(expected_tokens) + len(actual_tokens))
        actual_critical = Counter(critical(actual_tokens))
        missing = list((expected_counts - actual_counts).elements())
        extra = list((actual_counts - expected_counts).elements())
        split_only = (len(missing) == 1 and len(extra) == 2 and
                      missing[0] in {"".join(extra), "".join(reversed(extra))})
        summary = {"method": candidate["method"], "family": candidate["family"],
                   "family_overlaps_upstream": candidate["family_overlaps_upstream"],
                   "bag_support": round(bag, 3),
                   "lexical_difference": bool(missing or extra) and not split_only,
                   "lexical_missing": missing[:12], "lexical_extra": extra[:12],
                   "critical_missing": list((expected_critical - actual_critical).elements())[:12],
                   "critical_extra": list((actual_critical - expected_critical).elements())[:12]}
        if summary["family"] not in by_family or bag > by_family[summary["family"]]["bag_support"]:
            by_family[summary["family"]] = summary
    evidence = list(by_family.values())
    best = max(evidence, key=lambda c: c["bag_support"])
    scores["tables"].add(weight, best["bag_support"])
    best_critical = bool(best["critical_missing"] or best["critical_extra"])
    other_match = any(not (c["critical_missing"] or c["critical_extra"]) and
                      c["bag_support"] >= 0.85 for c in evidence if c is not best)
    if best_critical and other_match:
        scores["tables"].add(2, None)
        issue("tables", "table_method_disagreement", "major", block.bbox, [block.id], evidence)
    elif best_critical:
        difference_count = len(best["critical_missing"]) + len(best["critical_extra"])
        scores["tables"].add(max(2, min(8, 2 * difference_count)), 0)
        issue("tables", "table_critical_token_difference", "critical", block.bbox, [block.id], evidence)
    elif best["bag_support"] < 0.85 or best["lexical_difference"]:
        issue("tables", "table_aggregate_text_difference", "major", block.bbox, [block.id], evidence)
    elif any(c["bag_support"] >= 0.85 and (c["critical_missing"] or c["critical_extra"])
             for c in evidence if c is not best):
        scores["tables"].add(2, None)
        issue("tables", "table_method_disagreement", "major", block.bbox, [block.id], evidence)


def _table_rows(block: Block, rows: list[tuple[Box, tuple[str, ...]]], source: Observation,
                scores: dict, issue) -> None:
    upstream: dict[int, dict[int, tuple[str, str]]] = defaultdict(dict)
    for cell in block.cells:
        upstream[cell.row][cell.column] = (cell.id, cell.text)
    if len(upstream) != len(rows) or sorted(upstream) != list(range(len(rows))):
        scores["tables"].add(max(1, len(upstream)), 0)
        issue("tables", "table_row_count_disagreement", "major", block.bbox, [block.id],
              {"upstream_rows": len(upstream), "observed_rows": len(rows)})
        return
    for row_index, (row_box, source_cells) in enumerate(rows):
        expected_cells = upstream[row_index]
        upstream_columns = sorted(col for col, (_, value) in expected_cells.items() if value.strip())
        observed_columns = [col for col, value in enumerate(source_cells) if value.strip()]
        expected_text = " ".join(value for _, value in expected_cells.values())
        observed_text = " ".join(source_cells)
        a, b = Counter(tokens(expected_text)), Counter(tokens(observed_text))
        common = sum((a & b).values())
        support = 2 * common / max(1, sum(a.values()) + sum(b.values()))
        critical_match = Counter(critical(list(a.elements()))) == Counter(critical(list(b.elements())))
        column_match = upstream_columns == observed_columns and all(
            Counter(tokens(expected_cells[col][1])) == Counter(tokens(source_cells[col]))
            for col in upstream_columns if col < len(source_cells))
        good = support >= 0.95 and critical_match and column_match
        scores["tables"].add(max(1, min(8, sum(a.values()) / 8)), 1 if good else min(support, 0.5))
        if not good:
            issue("tables", "table_row_alignment_disagreement", "major", row_box,
                  [block.id] + [ident for ident, _ in expected_cells.values()],
                  {"row": row_index, "token_support": round(support, 3),
                   "method": source.method, "family": source.family,
                   "family_overlaps_upstream": source.family_overlaps_upstream,
                   "upstream_columns": upstream_columns, "observed_columns": observed_columns,
                   "critical_match": critical_match,
                   "upstream_row": [expected_cells.get(col, ("", ""))[1]
                                    for col in range(max(len(source_cells), max(expected_cells) + 1))],
                   "observed_row": list(source_cells)})


def _duplicate_table_content(page: Page, scores: dict, issue) -> None:
    compact = lambda value: re.sub(r"\W+", "", unicodedata.normalize("NFKC", value).casefold())
    for table in (b for b in page.blocks if b.kind == "table" and b.bbox):
        table_text = " ".join(c.text for c in table.cells) if table.cells else table.text
        normalized_table = compact(table_text)
        if not normalized_table:
            continue
        for block in page.blocks:
            if block.kind not in {"heading", "paragraph", "list_item", "footnote"} or not block.bbox:
                continue
            if not table.bbox.contains_center(block.bbox) or len(tokens(block.text)) < 4:
                continue
            if compact(block.text) in normalized_table:
                scores["accuracy"].add(min(20, len(tokens(block.text))), 0)
                issue("accuracy", "duplicate_table_content", "major", block.bbox,
                      [table.id, block.id], {"table_id": table.id, "text": block.text})


def _tables(page: Page, obs: list[Observation], scores: dict, issue) -> None:
    blocks = [b for b in page.blocks if b.kind == "table"]
    plumber = next((o for o in obs if o.method == "pdfplumber" and o.status == "ok"
                    and not o.shared_with_upstream), None)
    for block in blocks:
        matched_table = next((i for i, table in enumerate(plumber.tables)
                              if block.bbox and block.bbox.overlap(table) / max(1, table.area()) > 0.4), None) if plumber else None
        scores["tables"].add(2, None if block.bbox is None else 1)
        if block.bbox is None:
            issue("tables", "table_position_missing", "warning", None, [block.id], {})
        if not block.cells and not block.text:
            scores["tables"].add(4, None)
            issue("tables", "table_cells_not_available", "warning", block.bbox, [block.id], {})
            continue
        if not block.cells or any(cell.bbox is None for cell in block.cells):
            rows = (plumber.table_rows[matched_table] if matched_table is not None
                    and matched_table < len(plumber.table_rows) else [])
            if rows and block.cells:
                _table_rows(block, rows, plumber, scores, issue)
            else:
                scores["tables"].add(4, None)
                issue("tables", "table_cells_unlocated", "warning", block.bbox, [block.id],
                      {"unlocated_count": sum(cell.bbox is None for cell in block.cells),
                       "row_column_alignment": "unverified"})
            aggregate = " ".join(cell.text for cell in block.cells) if block.cells else block.text
            _table_aggregate(aggregate, block, obs, scores, issue)
        locations = set()
        for cell in block.cells:
            key = (cell.row, cell.column)
            duplicate = key in locations
            locations.add(key)
            if duplicate:
                scores["tables"].add(1, 0)
                issue("tables", "duplicate_cell_coordinate", "major", cell.bbox,
                      [block.id, cell.id], {"row": cell.row, "column": cell.column}, "failed")
                continue
            if cell.bbox is None:
                continue
            good = block.bbox is not None and block.bbox.contains_center(cell.bbox)
            scores["tables"].add(1, 1 if good else 0)
            if not good:
                issue("tables", "invalid_cell_geometry", "major", cell.bbox,
                      [block.id, cell.id], {"row": cell.row, "column": cell.column}, "failed")
            support, candidates, reasons = _comparison(cell.text, cell.bbox, obs)
            kind, confirmed = _difference(cell.text, candidates) if not reasons else (None, False)
            scores["tables"].add(max(1, min(8, len(tokens(cell.text)))),
                                 min(support, 0.5) if kind and kind.endswith("text_difference") else support)
            if reasons:
                issue("tables", "cell_not_verifiable", "warning", cell.bbox,
                      [block.id, cell.id], reasons)
            elif kind:
                label = "cell_method_disagreement" if kind.endswith("method_disagreement") else "cell_content_difference"
                issue("tables", label, "critical" if kind == "critical_text_difference" else "major",
                      cell.bbox, [block.id, cell.id], candidates,
                      "failed" if confirmed else "review_required")
        located = [c for c in block.cells if c.bbox]
        for first, second in combinations(located, 2):
            left, right = sorted((first, second), key=lambda c: (c.row, c.column))
            inverted = ((left.row == right.row and left.bbox.x0 > right.bbox.x0 + 2)
                        or (left.column == right.column and left.bbox.y0 > right.bbox.y0 + 2))
            if inverted:
                scores["tables"].add(1, 0)
                issue("tables", "cell_row_column_conflict", "major", right.bbox,
                      [block.id, left.id, right.id], {"left": [left.row, left.column],
                                                     "right": [right.row, right.column]}, "failed")
        if plumber and block.bbox:
            scores["tables"].add(2, 1 if matched_table is not None else None)
    if plumber:
        for table in plumber.tables:
            if not any(b.bbox and b.bbox.overlap(table) / max(1, table.area()) > 0.4 for b in blocks):
                scores["tables"].add(3, 0)
                issue("tables", "possible_table_omission", "major", table, [],
                      {"method": "pdfplumber"})


def _formula_figures(page: Page, obs: list[Observation], scores: dict, issue) -> None:
    plumber = next((o for o in obs if o.method == "pdfplumber" and o.status == "ok"
                    and not o.shared_with_upstream), None)
    blocks = [b for b in page.blocks if b.kind in {"equation", "figure"}]
    for block in blocks:
        if block.kind == "equation":
            support, candidates, _ = _comparison(block.text, block.bbox, obs) if block.text else (None, [], [])
            kind, _ = _difference(block.text, candidates) if candidates else (None, False)
            scores["formula_figures"].add(2, min(support, 0.5) if kind and kind.endswith("text_difference") else support)
            if kind:
                issue("formula_figures", "formula_text_difference", "major", block.bbox,
                      [block.id], candidates)
            scores["formula_figures"].add(2, None)  # symbolic/visual fidelity needs a specialist
            issue("formula_figures", "formula_semantics_unverified", "warning", block.bbox,
                  [block.id], ["no independent formula parser"])
        else:
            presence = None
            if plumber and block.bbox:
                presence = 1 if any(block.bbox.overlap(image) / max(1, image.area()) > 0.4
                                    for image in plumber.images) else None
            scores["formula_figures"].add(2, presence)
            scores["formula_figures"].add(2, None)  # chart meaning is not inferred from an image box
            if presence is None:
                issue("formula_figures", "figure_presence_unverified", "warning", block.bbox,
                      [block.id], ["image not independently located; vector graphics may exist"])
            else:
                issue("formula_figures", "figure_semantics_unverified", "warning", block.bbox,
                      [block.id], ["figure content not independently interpreted"])
    if plumber:
        for image in plumber.images:
            if not any(b.bbox and b.bbox.overlap(image) / max(1, image.area()) > 0.4 for b in blocks):
                scores["formula_figures"].add(2, 0)
                issue("formula_figures", "possible_figure_omission", "major", image, [],
                      {"method": "pdfplumber"})


def _repeated_marginals(observations: dict[int, list[Observation]],
                        sizes: list[tuple[float, float]]) -> set[str]:
    occurrences: dict[str, set[int]] = defaultdict(set)
    for index, obs in observations.items():
        reference = _reference(obs)
        if not reference:
            continue
        for line in reference.lines:
            if line.box.y1 < sizes[index][1] * 0.12 or line.box.y0 > sizes[index][1] * 0.88:
                key = " ".join(tokens(line.text))
                if key:
                    occurrences[key].add(index)
    return {text for text, pages in occurrences.items() if len(pages) >= 2}


def _page_label(text: str, index: int) -> str | None:
    value = text.strip()
    labeled = re.fullmatch(r"page\s+(\d{1,4})(?:\s*(?:of|/)\s*\d{1,4})?", value, re.I)
    if labeled:
        return labeled.group(1)
    return value if re.fullmatch(r"\d{1,4}", value) and value == str(index + 1) else None


def _cleanliness(page: Page, obs: list[Observation], size: tuple[float, float],
                 repeated: set[str], scores: dict, issue) -> None:
    separated = [b for b in page.blocks if b.kind in {"page_number", "header", "footer", "watermark", "sidebar"}]
    ref = _reference(obs)
    for block in page.blocks:
        if block.kind not in BODY_KINDS:
            continue
        body = " ".join(tokens(block.text))
        if not body:
            continue
        contaminated = False
        for meta in separated:
            value = " ".join(tokens(meta.text))
            if (value and value in body and meta.id != block.id
                    and (block.bbox is None or meta.bbox is None or block.bbox.overlap(meta.bbox))):
                contaminated = True
                issue("cleanliness", "metadata_in_body", "major", block.bbox,
                      [block.id, meta.id], {"metadata_type": meta.kind, "text": meta.text})
        if block.bbox and (block.bbox.y1 < size[1] * 0.15 or block.bbox.y0 > size[1] * 0.85):
            label = _page_label(block.text, page.page_index)
            if label:
                contaminated = True
                issue("cleanliness", "page_number_in_body", "major", block.bbox,
                      [block.id], {"observed_label": label})
        if (body in repeated and block.bbox
                and (block.bbox.y1 < size[1] * 0.12 or block.bbox.y0 > size[1] * 0.88)):
            contaminated = True
            issue("cleanliness", "repeated_margin_in_body", "major", block.bbox,
                  [block.id], {"text": block.text})
        scores["cleanliness"].add(1, 0 if contaminated else 1)
    if ref:
        for line in ref.lines:
            if line.box.y1 >= size[1] * 0.15 and line.box.y0 <= size[1] * 0.85:
                continue
            label = _page_label(line.text, page.page_index)
            if not label:
                continue
            if page.printed_page_label:
                declared = _page_label(page.printed_page_label, page.page_index) or page.printed_page_label.strip()
                scores["cleanliness"].add(1, 1 if declared == label else 0)
                if declared != label:
                    issue("cleanliness", "printed_page_label_mismatch", "major", line.box, [],
                          {"observed": label, "declared": page.printed_page_label})
            if not page.printed_page_label and not any(b.kind == "page_number" and
                                                    b.bbox and b.bbox.overlap(line.box)
                                                    for b in page.blocks):
                scores["cleanliness"].add(1, None)
                issue("cleanliness", "printed_page_number_not_separated", "warning", line.box,
                      [], {"observed_label": label})

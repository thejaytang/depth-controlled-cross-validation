"""Run with: python -m pdf_validator --pdf ... --json ... --depth ..."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .input import InputError
from .validate import DIMENSIONS, validate


def _markdown(report: dict) -> str:
    document = report["document"]
    lines = ["# PDF extraction validation", "",
             f"- Result: **{document['status']}**",
             f"- Evidence support score: **{document['overall_support_score']} / 100**",
             f"- Pages: {document['page_count']}",
             f"- Requested Depth: {report['requested_depth']}",
             "- Calibrated probability of correctness: unavailable (no independently labeled data)", "",
             "| PDF page | Accuracy | Structure | Tables | Formula/Figures | Cleanliness | Total | Status | Methods | Findings |",
             "| ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | ---: | ---: |"]
    for page in report["pages"]:
        dimensions = page["dimension_scores"]
        columns = [str(dimensions[name]["score"] if dimensions[name]["score"] is not None
                       else dimensions[name]["state"]) for name in DIMENSIONS]
        lines.append(f"| {page['page_index'] + 1} | " + " | ".join(columns) +
                     f" | {page['overall_support_score']} | "
                     f"{page['status']} | {page['effective_depth']} | {len(page['finding_ids'])} |")
    lines += ["", "## Document dimensions", "", "| Dimension | Score | Coverage | State |",
              "| --- | ---: | ---: | --- |"]
    for name in DIMENSIONS:
        item = document["dimension_scores"][name]
        lines.append(f"| {name} | {item['score']} | {item['coverage']} | {item['state']} |")
    lines += ["", "## Located findings", ""]
    if not report["findings"]:
        lines.append("No located discrepancy was found by the available checks.")
    else:
        for item in report["findings"]:
            lines.append(f"- **{item['id']}**, PDF page {item['page_index'] + 1}: "
                         f"{item['dimension']} / {item['kind']} / {item['severity']}; "
                         f"upstream IDs: {', '.join(item['upstream_ids']) or 'none'}; "
                         f"bbox: {item['bbox']}")
    lines += ["", "Scores represent evidence support, not a proven probability of correctness.", ""]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an upstream page JSON against its source PDF")
    parser.add_argument("--pdf", required=True, type=Path, help="Original PDF")
    parser.add_argument("--json", required=True, type=Path, help="Upstream page JSON")
    parser.add_argument("--depth", required=True, type=int, choices=range(1, 6), metavar="1..5")
    parser.add_argument("--output", required=True, type=Path, help="Output report JSON")
    parser.add_argument("--ocr-lang", default="eng", help="Installed Tesseract language code")
    args = parser.parse_args(argv)
    if args.output.resolve() in {args.pdf.resolve(), args.json.resolve()}:
        parser.error("output must not overwrite either input")
    try:
        report = validate(args.pdf, args.json, args.depth, args.ocr_lang)
    except (InputError, ValueError, OSError) as exc:
        print(f"Validation could not start: {exc}", file=sys.stderr)
        return 2
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    summary = args.output.with_suffix(".md")
    if summary.resolve() not in {args.pdf.resolve(), args.json.resolve()}:
        summary.write_text(_markdown(report), encoding="utf-8")
    print(f"{report['document']['status']}: {report['document']['overall_support_score']} / 100; "
          f"{len(report['findings'])} findings; report: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

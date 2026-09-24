"""Five local extraction paths; absent and overlapping observations stay explicit."""

from __future__ import annotations

import csv
from dataclasses import dataclass, field
import io
from importlib.metadata import version
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory

from .input import Box


METHODS = ("poppler", "tesseract", "pdfplumber", "pdfium", "pypdf")
FAMILIES = {
    "poppler": "embedded_text",
    "tesseract": "visual_ocr",
    "pdfplumber": "embedded_text",
    "pdfium": "embedded_text",
    "pypdf": "embedded_text",
}
ALIASES = {
    "poppler": {"poppler", "pdftotext"},
    "tesseract": {"tesseract", "tesseract-ocr"},
    "pdfplumber": {"pdfplumber", "pdfminer", "pdfminer.six"},
    "pdfium": {"pdfium", "pypdfium2"},
    "pypdf": {"pypdf", "pypdf2"},
}


@dataclass(frozen=True)
class Word:
    text: str
    box: Box


@dataclass(frozen=True)
class Line:
    text: str
    box: Box
    font_size: float | None = None
    bold: bool | None = None


@dataclass
class Observation:
    method: str
    family: str
    page_index: int
    status: str
    text: str = ""
    words: list[Word] = field(default_factory=list)
    lines: list[Line] = field(default_factory=list)
    tables: list[Box] = field(default_factory=list)
    images: list[Box] = field(default_factory=list)
    error: str | None = None
    shared_with_upstream: bool = False
    family_overlaps_upstream: bool = False
    table_rows: list[list[tuple[Box, tuple[str, ...]]]] = field(default_factory=list)

    def has_evidence(self) -> bool:
        return bool(self.text.strip() or self.words or self.tables or self.images)


def page_sizes(path: Path) -> list[tuple[float, float]]:
    import pypdfium2 as pdfium

    try:
        pdf = pdfium.PdfDocument(str(path))
        try:
            if len(pdf) == 0:
                raise ValueError("PDF has no pages")
            return [tuple(float(v) for v in pdf[i].get_size()) for i in range(len(pdf))]
        finally:
            pdf.close()
    except Exception as exc:
        raise ValueError(f"cannot open PDF: {exc}") from exc


def _command(args: list[str], timeout: int = 60) -> bytes:
    try:
        return subprocess.run(args, capture_output=True, check=True, timeout=timeout).stdout
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError(f"{args[0]} unavailable or failed: {type(exc).__name__}") from exc


def _tsv_rows(raw: bytes) -> list[dict[str, str]]:
    return list(csv.DictReader(io.StringIO(raw.decode("utf-8", errors="replace")), delimiter="\t"))


def _box(row: dict[str, str], x: float = 1.0, y: float = 1.0) -> Box:
    left, top, width, height = (float(row[k]) for k in ("left", "top", "width", "height"))
    return Box(left * x, top * y, (left + width) * x, (top + height) * y)


def _group_words(words: list[tuple[tuple[str, ...], Word]]) -> list[Line]:
    groups: dict[tuple[str, ...], list[Word]] = {}
    for key, word in words:
        groups.setdefault(key, []).append(word)
    lines = []
    for group in groups.values():
        b = Box(min(w.box.x0 for w in group), min(w.box.y0 for w in group),
                max(w.box.x1 for w in group), max(w.box.y1 for w in group))
        lines.append(Line(" ".join(w.text for w in group), b))
    return sorted(lines, key=lambda line: (line.box.y0, line.box.x0))


def _poppler(path: Path, index: int) -> Observation:
    rows = _tsv_rows(_command(["pdftotext", "-f", str(index + 1), "-l", str(index + 1),
                               "-tsv", str(path), "-"]))
    keyed = []
    for row in rows:
        if row.get("level") == "5" and row.get("text", "").strip():
            word = Word(row["text"], _box(row))
            keyed.append(((row["block_num"], row["par_num"], row["line_num"]), word))
    words = [w for _, w in keyed]
    lines = _group_words(keyed)
    return Observation("poppler", FAMILIES["poppler"], index, "ok" if words else "empty",
                       "\n".join(line.text for line in lines), words, lines)


def _tesseract(path: Path, index: int, size: tuple[float, float], language: str) -> Observation:
    import pypdfium2 as pdfium

    with TemporaryDirectory(prefix="pdf-validator-ocr-") as temp:
        image = Path(temp) / "page.png"
        pdf = pdfium.PdfDocument(str(path))
        try:
            page = pdf[index]
            bitmap = page.render(scale=2)
            bitmap.to_pil().save(image)
            width_px, height_px = bitmap.width, bitmap.height
        finally:
            pdf.close()
        raw = _command(["tesseract", str(image), "stdout", "-l", language, "tsv"], timeout=120)
    sx, sy = size[0] / width_px, size[1] / height_px
    keyed = []
    for row in _tsv_rows(raw):
        if row.get("level") == "5" and row.get("text", "").strip():
            word = Word(row["text"], _box(row, sx, sy))
            keyed.append(((row["block_num"], row["par_num"], row["line_num"]), word))
    words = [w for _, w in keyed]
    lines = _group_words(keyed)
    return Observation("tesseract", FAMILIES["tesseract"], index,
                       "ok" if words else "empty", "\n".join(line.text for line in lines), words, lines)


def _pdfplumber(path: Path, index: int) -> Observation:
    import pdfplumber

    with pdfplumber.open(path) as pdf:
        page = pdf.pages[index]
        words = [Word(w["text"], Box(w["x0"], w["top"], w["x1"], w["bottom"]))
                 for w in page.extract_words() if w["text"].strip()]
        lines = []
        for item in page.extract_text_lines():
            box = Box(item["x0"], item["top"], item["x1"], item["bottom"])
            chars = item.get("chars", [])
            font_size = max((float(c["size"]) for c in chars), default=None)
            bold = any("bold" in c.get("fontname", "").lower() for c in chars)
            lines.append(Line(item["text"], box, font_size, bold))
        try:
            found_tables = page.find_tables()
            tables = [Box(*table.bbox) for table in found_tables]
            table_rows = [[(Box(*row.bbox), tuple(cell or "" for cell in cells))
                           for row, cells in zip(table.rows, table.extract())]
                          for table in found_tables]
            table_error = None
        except Exception as exc:
            tables, table_rows, table_error = [], [], f"table detection failed: {type(exc).__name__}"
        images = [Box(img["x0"], img["top"], img["x1"], img["bottom"])
                  for img in page.images if all(k in img for k in ("x0", "top", "x1", "bottom"))]
    return Observation("pdfplumber", FAMILIES["pdfplumber"], index,
                       "ok" if words or tables or images else "empty",
                       "\n".join(line.text for line in lines), words, lines, tables, images, table_error,
                       table_rows=table_rows)


def _pdfium(path: Path, index: int) -> Observation:
    import pypdfium2 as pdfium

    pdf = pdfium.PdfDocument(str(path))
    try:
        page = pdf[index]
        text = page.get_textpage().get_text_bounded().replace("\r\n", "\n")
    finally:
        pdf.close()
    return Observation("pdfium", FAMILIES["pdfium"], index, "ok" if text.strip() else "empty", text)


def _pypdf(path: Path, index: int) -> Observation:
    from pypdf import PdfReader

    text = PdfReader(str(path)).pages[index].extract_text() or ""
    return Observation("pypdf", FAMILIES["pypdf"], index, "ok" if text.strip() else "empty", text)


def selected_methods(depth: int, native_text_present: bool) -> tuple[str, ...]:
    if type(depth) is not int or not 1 <= depth <= 5:
        raise ValueError("Depth must be an integer from 1 to 5")
    order = METHODS if native_text_present else ("tesseract", "poppler", "pdfplumber", "pdfium", "pypdf")
    return order[:depth]


def read_page(path: Path, index: int, size: tuple[float, float], depth: int,
              upstream_engines: tuple[str, ...], language: str = "eng") -> list[Observation]:
    try:
        native_text_present = bool(_pdfium(path, index).text.strip())
    except Exception:
        native_text_present = False
    observations = []
    engine_names = {e.strip().lower() for e in upstream_engines}
    upstream_families = {FAMILIES[name] for name in METHODS if ALIASES[name] & engine_names}
    available = [name for name in selected_methods(5, native_text_present)
                 if not ALIASES[name] & engine_names]
    for name in available[:depth]:
        try:
            obs = {
                "poppler": lambda: _poppler(path, index),
                "tesseract": lambda: _tesseract(path, index, size, language),
                "pdfplumber": lambda: _pdfplumber(path, index),
                "pdfium": lambda: _pdfium(path, index),
                "pypdf": lambda: _pypdf(path, index),
            }[name]()
        except Exception as exc:
            obs = Observation(name, FAMILIES[name], index, "error", error=f"{type(exc).__name__}: {exc}")
        obs.shared_with_upstream = bool(ALIASES[name] & engine_names)
        obs.family_overlaps_upstream = obs.family in upstream_families
        observations.append(obs)
    return observations


def versions() -> dict[str, str]:
    found = {}
    for name, package in (("pdfplumber", "pdfplumber"), ("pdfium", "pypdfium2"),
                          ("pypdf", "pypdf")):
        found[name] = version(package)
    for name, command in (("poppler", ["pdftotext", "-v"]),
                          ("tesseract", ["tesseract", "--version"])):
        try:
            result = subprocess.run(command, capture_output=True, timeout=5, check=True)
            found[name] = (result.stdout or result.stderr).decode(errors="replace").splitlines()[0]
        except (OSError, subprocess.SubprocessError, IndexError):
            found[name] = "unavailable"
    return found

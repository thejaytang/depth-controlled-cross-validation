"""The deliberately small, explicit upstream JSON boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
import math
from pathlib import Path
import re


KINDS = {
    "heading", "paragraph", "list_item", "table", "equation", "figure",
    "footnote", "header", "footer", "page_number", "sidebar", "watermark",
}


class InputError(ValueError):
    pass


@dataclass(frozen=True)
class Box:
    x0: float
    y0: float
    x1: float
    y1: float

    def as_list(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]

    def area(self) -> float:
        return (self.x1 - self.x0) * (self.y1 - self.y0)

    def overlap(self, other: "Box") -> float:
        w = max(0.0, min(self.x1, other.x1) - max(self.x0, other.x0))
        h = max(0.0, min(self.y1, other.y1) - max(self.y0, other.y0))
        return w * h

    def contains_center(self, other: "Box", slack: float = 2.0) -> bool:
        x = (other.x0 + other.x1) / 2
        y = (other.y0 + other.y1) / 2
        return self.x0 - slack <= x <= self.x1 + slack and self.y0 - slack <= y <= self.y1 + slack


def parse_box(value: object, width: float, height: float, where: str) -> Box | None:
    if value is None:
        return None
    if isinstance(value, dict):
        try:
            value = [value[k] for k in ("x0", "y0", "x1", "y1")]
        except KeyError as exc:
            raise InputError(f"{where}: bbox missing {exc.args[0]}") from exc
    if not isinstance(value, list) or len(value) != 4:
        raise InputError(f"{where}: bbox must be [x0, y0, x1, y1]")
    if any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in value):
        raise InputError(f"{where}: bbox coordinates must be finite numbers")
    b = Box(*(float(v) for v in value))
    if not (0 <= b.x0 < b.x1 <= width + 1 and 0 <= b.y0 < b.y1 <= height + 1):
        raise InputError(f"{where}: bbox outside physical PDF page in top-left points")
    return b


@dataclass(frozen=True)
class Cell:
    id: str
    row: int
    column: int
    text: str
    bbox: Box | None


@dataclass(frozen=True)
class Block:
    id: str
    kind: str
    text: str
    bbox: Box | None
    parent_id: str | None
    heading_level: int | None
    order: int | None
    cells: tuple[Cell, ...]


@dataclass(frozen=True)
class Page:
    page_index: int
    printed_page_label: str | None
    blocks: tuple[Block, ...]


@dataclass(frozen=True)
class Document:
    pages: dict[int, Page]
    upstream_engines: tuple[str, ...]
    document_type: str


def _integer(value: object, where: str, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        raise InputError(f"{where}: expected integer >= {minimum}")
    return value


def load_document(path: Path, page_sizes: list[tuple[float, float]], pdf_sha256: str | None = None) -> Document:
    try:
        root = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise InputError(f"cannot read upstream JSON: {exc}") from exc
    if not isinstance(root, dict) or not isinstance(root.get("pages"), list):
        raise InputError("root.pages must be an array")
    declared_hash = root.get("source_sha256")
    if declared_hash is not None:
        if not isinstance(declared_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", declared_hash):
            raise InputError("source_sha256 must be a SHA-256 hex string")
        if pdf_sha256 and declared_hash.lower() != pdf_sha256.lower():
            raise InputError("source_sha256 does not match the input PDF")
    if root.get("bbox_unit", "points") != "points" or root.get("coord_origin", "top_left") != "top_left":
        raise InputError("this input contract requires top-left PDF point coordinates")
    kind = root.get("document_type", "general")
    if not isinstance(kind, str) or kind not in {"general", "financial", "legal"}:
        raise InputError("document_type must be general, financial, or legal")
    engines = root.get("parser_engines", [])
    if not isinstance(engines, list) or any(not isinstance(e, str) for e in engines):
        raise InputError("parser_engines must be an array of strings")
    pages: dict[int, Page] = {}
    ids: set[str] = set()
    for raw_page in root["pages"]:
        if not isinstance(raw_page, dict):
            raise InputError("each page must be an object")
        index = _integer(raw_page.get("page_index"), "page_index")
        if index >= len(page_sizes):
            raise InputError(f"page_index {index} exceeds PDF page count {len(page_sizes)}")
        if index in pages:
            raise InputError(f"duplicate page_index {index}")
        label = raw_page.get("printed_page_label")
        if label is not None and not isinstance(label, str):
            raise InputError(f"page {index}: printed_page_label must be text or null")
        raw_blocks = raw_page.get("blocks")
        if not isinstance(raw_blocks, list):
            raise InputError(f"page {index}: blocks must be an array")
        width, height = page_sizes[index]
        blocks: list[Block] = []
        for raw in raw_blocks:
            if not isinstance(raw, dict):
                raise InputError(f"page {index}: each block must be an object")
            ident, block_kind = raw.get("id"), raw.get("type")
            if not isinstance(ident, str) or not ident or ident in ids:
                raise InputError(f"page {index}: block id missing or duplicate: {ident!r}")
            ids.add(ident)
            if not isinstance(block_kind, str) or block_kind not in KINDS:
                raise InputError(f"{ident}: unsupported block type {block_kind!r}")
            value = raw.get("text", "")
            if not isinstance(value, str):
                raise InputError(f"{ident}: text must be a string")
            parent = raw.get("parent_id")
            if parent is not None and not isinstance(parent, str):
                raise InputError(f"{ident}: parent_id must be text or null")
            level = raw.get("heading_level")
            if level is not None:
                level = _integer(level, f"{ident}.heading_level", 1)
            order = raw.get("order")
            if order is not None:
                order = _integer(order, f"{ident}.order")
            box = parse_box(raw.get("bbox"), width, height, ident)
            raw_cells = raw.get("cells", [])
            if not isinstance(raw_cells, list) or (block_kind != "table" and raw_cells):
                raise InputError(f"{ident}: cells only allowed on table blocks")
            cells: list[Cell] = []
            cell_ids: set[str] = set()
            for rc in raw_cells:
                if not isinstance(rc, dict) or not isinstance(rc.get("id"), str) or not rc["id"]:
                    raise InputError(f"{ident}: cell id required")
                cid = rc["id"]
                if cid in cell_ids:
                    raise InputError(f"{ident}: duplicate cell id {cid}")
                cell_ids.add(cid)
                ctext = rc.get("text", "")
                if not isinstance(ctext, str):
                    raise InputError(f"{ident}.{cid}: text must be string")
                cells.append(Cell(cid, _integer(rc.get("row"), f"{cid}.row"),
                                  _integer(rc.get("column"), f"{cid}.column"), ctext,
                                  parse_box(rc.get("bbox"), width, height, f"{ident}.{cid}")))
            blocks.append(Block(ident, block_kind, value, box, parent, level, order, tuple(cells)))
        pages[index] = Page(index, label, tuple(blocks))
    return Document(pages, tuple(engines), kind)

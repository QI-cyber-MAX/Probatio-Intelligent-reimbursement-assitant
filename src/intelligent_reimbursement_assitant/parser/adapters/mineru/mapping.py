"""Map observed cloud tables to logical grids with their complete source page range."""

import re
import unicodedata
from contextlib import closing
from dataclasses import dataclass
from io import BytesIO
from pathlib import PurePosixPath
from typing import Literal, cast
from uuid import UUID, uuid5
from zipfile import BadZipFile, ZipFile

import pypdfium2
from lxml import html
from lxml.etree import LxmlError
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef, SourceLocator
from intelligent_reimbursement_assitant.errors.codes import ErrorCode as Code
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.parser.domain.config import MinerUSettings
from intelligent_reimbursement_assitant.parser.domain.models import (
    BlockKind,
    CoverageStatus,
    DocumentBlock,
    HeaderRole,
    ParsedDocument,
    ParseReport,
    TableBlock,
    TableCell,
)


class Span(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    type: str
    content: str | None = None
    html: str | None = None


class Line(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    spans: list[Span]


class LayoutBlock(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    type: str
    level: int | None = None
    lines: list[Line] = Field(default_factory=list)
    blocks: list[LayoutBlock] = Field(default_factory=list)
    cell_merge: list[int] | None = None
    index: int | float
    bbox: tuple[float, float, float, float]
    lines_deleted: bool = False


class Page(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    page_idx: int = Field(ge=0)
    preproc_blocks: list[LayoutBlock]
    para_blocks: list[LayoutBlock]
    discarded_blocks: list[LayoutBlock]


class CloudLayout(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)
    version: Literal["3.4.4"] = Field(alias="_version_name")
    backend: str = Field(alias="_backend")
    pdf_info: list[Page]


def _invalid() -> ExecutionError:
    return ExecutionError(
        Code.PARSE_RESULT_INVALID, "PERMANENT", "MinerU result structure is invalid"
    )


def read_result_archive(raw: bytes, settings: MinerUSettings) -> CloudLayout:
    try:
        with ZipFile(BytesIO(raw)) as archive:
            members = archive.infolist()
            names = [m.filename for m in members]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate archive members")
            for member in members:
                path = PurePosixPath(member.filename)
                if path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                    raise ValueError("Unsafe archive path")
                if (member.external_attr >> 16) & 0o170000 == 0o120000:
                    raise ValueError("Archive symlink")
            if sum(m.file_size for m in members) > settings.max_uncompressed_bytes:
                raise ExecutionError(
                    Code.DOCUMENT_PARSE_RESOURCE_LIMIT, "PERMANENT", "Archive exceeds limit"
                )
            if "layout.json" not in names or "full.md" not in names:
                raise ValueError("Missing cloud members")
            # Only the confirmed structure and Markdown are read; images are never fetched.
            total = 0
            selected = {}
            for name in ("layout.json", "full.md"):
                with archive.open(name) as member:
                    data = bytearray()
                    while part := member.read(65536):
                        total += len(part)
                        if total > settings.max_uncompressed_bytes:
                            raise ExecutionError(
                                Code.DOCUMENT_PARSE_RESOURCE_LIMIT,
                                "PERMANENT",
                                "Archive exceeds limit",
                            )
                        data.extend(part)
                    selected[name] = bytes(data)
            selected["full.md"].decode("utf-8")
            return CloudLayout.model_validate_json(selected["layout.json"])
    except (BadZipFile, ValueError, KeyError, RuntimeError, NotImplementedError) as error:
        raise _invalid() from error


def map_table(markup: str) -> list[TableCell]:
    try:
        parser = html.HTMLParser(no_network=True, recover=False)
        root = html.fromstring(markup, parser=parser)
        if root.tag != "table" or root.xpath(".//table"):
            raise ValueError("Expected one non-nested table")
        rows = root.xpath("./tr | ./thead/tr | ./tbody/tr | ./tfoot/tr")
        if not rows:
            raise ValueError("Missing table rows")
        occupied: set[tuple[int, int]] = set()
        cells = []
        for row, element in enumerate(rows):
            column = 0
            for node in element:
                if node.tag not in {"td", "th"}:
                    raise ValueError("Unexpected row node")
                if node.xpath(".//img | .//svg | .//object | .//iframe"):
                    raise ValueError("Non-text cell content cannot be represented")
                while (row, column) in occupied:
                    column += 1
                rs, cs = int(node.get("rowspan", "1")), int(node.get("colspan", "1"))
                if row + rs > len(rows):
                    raise ValueError("Invalid table span")
                if rs * cs > 100000:
                    raise ExecutionError(
                        Code.DOCUMENT_PARSE_RESOURCE_LIMIT,
                        "PERMANENT",
                        "Table grid exceeds resource limit",
                    )
                positions = {
                    (r, c) for r in range(row, row + rs) for c in range(column, column + cs)
                }
                # Occupancy places anchors; ParsedDocument checks grid overlap once.
                occupied.update(positions)
                scope = node.get("scope")
                role = HeaderRole.UNKNOWN
                if node.tag == "th":
                    role = (
                        HeaderRole.ROW
                        if scope in {"row", "rowgroup"}
                        else HeaderRole.COLUMN
                        if scope in {"col", "colgroup"} or element.getparent().tag == "thead"
                        else HeaderRole.UNKNOWN
                    )
                for br in node.xpath(".//br"):
                    br.tail = "\n" + (br.tail or "")
                cells.append(
                    TableCell(
                        row_index=row,
                        column_index=column,
                        row_span=rs,
                        column_span=cs,
                        text=node.text_content(),
                        header_role=role,
                    )
                )
                column += cs
        if not cells:
            raise ValueError("Missing table cells")
        return cells
    except (ValueError, LxmlError, ValidationError) as error:
        raise _invalid() from error


def _text(block: LayoutBlock) -> str:
    if any(
        span.type != "text" or span.content is None for line in block.lines for span in line.spans
    ):
        raise _invalid()
    return "\n".join("".join(span.content or "" for span in line.spans) for line in block.lines)


def _body(node: LayoutBlock) -> LayoutBlock:
    bodies = [child for child in node.blocks if child.type == "table_body"]
    if len(bodies) != 1:
        raise _invalid()
    return bodies[0]


def _markup(body: LayoutBlock) -> str:
    spans = [span for line in body.lines for span in line.spans]
    if len(spans) != 1 or spans[0].type != "table" or spans[0].html is None:
        raise _invalid()
    return spans[0].html


@dataclass(frozen=True)
class _SourceTable:
    page: int
    path: str
    node: LayoutBlock
    merged: LayoutBlock
    body: LayoutBlock
    merged_body: LayoutBlock


def _table_groups(pages: dict[int, Page]) -> dict[str, list[_SourceTable]]:
    """Only producer-deleted page-head bodies prove a continuation link."""
    groups: dict[str, list[_SourceTable]] = {}
    previous: _SourceTable | None = None
    owner: str | None = None
    for index, page in sorted(pages.items()):
        for pos, node in enumerate(page.preproc_blocks):
            if node.type != "table":
                continue
            matches = [
                block
                for block in page.para_blocks
                if block.type == "table" and (block.index, block.bbox) == (node.index, node.bbox)
            ]
            if len(matches) != 1:
                raise _invalid()
            merged = matches[0]
            source = _SourceTable(
                index,
                f"pdf_info/{index}/preproc_blocks/{pos}",
                node,
                merged,
                _body(node),
                _body(merged),
            )
            if source.merged_body.lines_deleted:
                if (
                    previous is None
                    or previous.page + 1 != index
                    or previous.merged is not pages[previous.page].para_blocks[-1]
                    or merged is not page.para_blocks[0]
                    or node.cell_merge is None
                    or owner is None
                ):
                    raise _invalid()
                groups[owner].append(source)
            else:
                owner = source.path
                groups[owner] = [source]
            previous = source
    return groups


def _normalized_text(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value))


def _boundary_columns(pdf: pypdfium2.PdfDocument, source: _SourceTable, columns: int, *, top: bool):
    """Read native rules and text at a marked seam; no OCR or local PDF pipeline."""
    with closing(pdf[source.page]) as page:
        left, upper, right, lower = source.body.bbox
        bottom, ceiling = page.get_height() - lower, page.get_height() - upper
        paths = [
            obj.get_bounds() for obj in page.get_objects(filter=[pypdfium2.raw.FPDF_PAGEOBJ_PATH])
        ]
        verticals = [
            (x0 + x1) / 2
            for x0, y0, x1, y1 in paths
            if x1 - x0 <= 1.5
            and y1 - y0 > 4
            and left - 6 <= x0 <= right + 6
            and y0 >= bottom - 6
            and y1 <= ceiling + 6
        ]
        edges: list[float] = []
        for x in sorted(verticals):
            if not edges or x - edges[-1] > 2:
                edges.append(x)
        if len(edges) != columns + 1:
            return None
        seam = ceiling if top else bottom
        horizontal = [
            (x0, x1)
            for x0, y0, x1, y1 in paths
            if x1 - x0 > 3 and y1 - y0 <= 1.5 and abs((y0 + y1) / 2 - seam) < 6
        ]
        with closing(page.get_textpage()) as textpage:
            return [
                (
                    not any(x0 < (a + b) / 2 < x1 for x0, x1 in horizontal),
                    _normalized_text(textpage.get_text_bounded(a + 1, bottom, b - 1, ceiling)),
                )
                for a, b in zip(edges, edges[1:], strict=False)
            ]


def _joined_cells(group: list[_SourceTable], pdf: pypdfium2.PdfDocument):
    """Use cloud HTML unless a cell seam needs native-rule correction."""
    if not any(any(source.node.cell_merge or []) for source in group[1:]):
        return map_table(_markup(group[0].merged_body)), []
    cells = map_table(_markup(group[0].body))
    gaps: list[int] = []
    for previous, source in zip(group, group[1:], strict=False):
        incoming = map_table(_markup(source.body))
        # Only an exact repeated leading header band can be removed.
        repeated = 0
        for row in range(max(c.row_index for c in incoming) + 1):
            old = [c for c in cells if c.row_index == row]
            new = [c for c in incoming if c.row_index == row]
            if old != new:
                break
            repeated = row + 1
        if repeated:
            if any(c.row_index < repeated < c.row_index + c.row_span for c in incoming):
                raise _invalid()
            incoming = [
                c.model_copy(update={"row_index": c.row_index - repeated})
                for c in incoming
                if c.row_index >= repeated
            ]
        offset = max(c.row_index + c.row_span for c in cells)
        flags = source.node.cell_merge or []
        pairs: list[tuple[TableCell, TableCell]] = []
        if any(flags):
            columns = max(c.column_index + c.column_span for c in cells)
            before = _boundary_columns(pdf, previous, columns, top=False)
            after = _boundary_columns(pdf, source, columns, top=True)
            if before is None or after is None or len(flags) != columns:
                gaps.append(source.page + 1)
            else:
                # A producer-marked text-only carry has no new record/amounts.
                # PDF exporters can close its frame at the physical page edge.
                text_carry = all(flags) and not any(
                    re.search(r"\d", c.text) for c in incoming if c.row_index == 0
                )
                for new in (c for c in incoming if c.row_index == 0):
                    covered = range(new.column_index, new.column_index + new.column_span)
                    if not all(
                        flags[col] and (text_carry or (before[col][0] and after[col][0]))
                        for col in covered
                    ):
                        continue
                    old = next(
                        (
                            c
                            for c in cells
                            if c.row_index + c.row_span == offset
                            and (c.column_index, c.column_span)
                            == (new.column_index, new.column_span)
                        ),
                        None,
                    )
                    if old is None or any(
                        value
                        and _normalized_text(value)
                        not in "".join(native[col][1] for col in covered)
                        for value, native in ((old.text, before), (new.text, after))
                    ):
                        gaps.append(source.page + 1)
                        continue
                    pairs.append((old, new))
        merged_new = [new for _, new in pairs]
        drop_first = bool(pairs) and not any(
            c.text.strip() for c in incoming if c.row_index == 0 and c not in merged_new
        )
        for old, new in pairs:
            cells[cells.index(old)] = old.model_copy(
                update={
                    "text": old.text + new.text,
                    "row_span": offset + new.row_span - int(drop_first) - old.row_index,
                }
            )
        for cell in incoming:
            if cell in merged_new:
                continue
            if drop_first and cell.row_index == 0:
                if cell.row_span == 1:
                    continue
                cell = cell.model_copy(update={"row_index": 1, "row_span": cell.row_span - 1})
            cells.append(
                cell.model_copy(update={"row_index": cell.row_index + offset - int(drop_first)})
            )
    return cells, gaps


def map_document(
    layout: CloudLayout, document_id: UUID, source_ref: ArtifactRef, original: bytes
) -> ParsedDocument:
    blocks: list[DocumentBlock | TableBlock] = []
    failed: list[SourceLocator] = []
    empty: list[SourceLocator] = []
    warnings: list[str] = []
    headings: list[tuple[int, DocumentBlock]] = []

    def locator(page: int, path: str | None = None):
        block_id = str(uuid5(document_id, f"{source_ref.sha256}:mineru:{path}")) if path else None
        return SourceLocator(
            document_id=document_id,
            source_hash=source_ref.sha256,
            block_ids=[block_id] if block_id else [],
            section_path=[b.text for _, b in headings],
            page_start=page,
            page_end=page,
        )

    def text_block(
        node: LayoutBlock,
        page: int,
        path: str,
        kind: Literal[BlockKind.HEADING, BlockKind.PARAGRAPH, BlockKind.LIST, BlockKind.FOOTNOTE],
        text: str,
    ) -> DocumentBlock:
        if kind == BlockKind.HEADING:
            level = node.level or 1
            while headings and headings[-1][0] >= level:
                headings.pop()
        loc = locator(page, path)
        block = DocumentBlock(
            block_id=loc.block_ids[0],
            parent_id=headings[-1][1].block_id if headings else None,
            kind=kind,
            text=text,
            locator=loc,
        )
        if kind == BlockKind.HEADING:
            headings.append((node.level or 1, block))
            block = block.model_copy(
                update={
                    "locator": loc.model_copy(
                        update={"section_path": [b.text for _, b in headings]}
                    )
                }
            )
            headings[-1] = (node.level or 1, block)
        blocks.append(block)
        return block

    with pypdfium2.PdfDocument(original) as pdf:
        page_count = len(pdf)
        indices = [p.page_idx for p in layout.pdf_info]
        if len(indices) != len(set(indices)) or any(i >= page_count for i in indices):
            raise _invalid()
        by_page = {p.page_idx: p for p in layout.pdf_info}
        groups = _table_groups(by_page)
        owners = {source.path: owner for owner, group in groups.items() for source in group}
        metadata: dict[str, str] = {}
        references: dict[str, tuple[list[str], list[str]]] = {}
        for owner, group in groups.items():
            units, notes = [], []
            seen: set[tuple[str, str]] = set()
            for source in group:
                for child_index, child in enumerate(source.node.blocks):
                    if child.type not in {"table_caption", "table_footnote"}:
                        continue
                    path = f"{source.path}/blocks/{child_index}"
                    child_text = _text(child)
                    is_unit = bool(re.search(r"单位\s*[:：]", child_text))
                    if is_unit or child.type == "table_footnote":
                        key = (child.type, _normalized_text(child_text))
                        if key in seen:
                            continue
                        seen.add(key)
                        refs = units if is_unit else notes
                        refs.append(locator(source.page + 1, path).block_ids[0])
                    metadata[path] = child_text
            references[owner] = units, notes
        for index in range(page_count):
            page = by_page.get(index)
            if page is None:
                failed.append(locator(index + 1))
                continue
            processed = False
            for pos, node in enumerate(page.preproc_blocks):
                path = f"pdf_info/{index}/preproc_blocks/{pos}"
                if node.type in {"title", "text", "list", "footnote"}:
                    processed = True
                    kind = {
                        "title": BlockKind.HEADING,
                        "text": BlockKind.PARAGRAPH,
                        "list": BlockKind.LIST,
                        "footnote": BlockKind.FOOTNOTE,
                    }[node.type]
                    text_block(
                        node,
                        index + 1,
                        path,
                        cast(
                            Literal[
                                BlockKind.HEADING,
                                BlockKind.PARAGRAPH,
                                BlockKind.LIST,
                                BlockKind.FOOTNOTE,
                            ],
                            kind,
                        ),
                        _text(node),
                    )
                elif node.type == "table":
                    processed = True
                    owner = owners[path]
                    group = groups[owner]
                    units, notes = references[owner]
                    deferred_notes = []
                    table_parent = headings[-1][1].block_id if headings else None
                    for child_index, child in enumerate(node.blocks):
                        child_path = f"{path}/blocks/{child_index}"
                        if child.type == "table_body":
                            continue
                        elif child.type in {"table_caption", "table_footnote"}:
                            if child_path not in metadata:
                                continue
                            if child.type == "table_footnote":
                                deferred_notes.append((child, child_path))
                                continue
                            child_text = metadata[child_path]
                            is_unit = bool(re.search(r"单位\s*[:：]", child_text))
                            kind = (
                                BlockKind.FOOTNOTE
                                if child.type == "table_footnote"
                                else BlockKind.HEADING
                                if re.match(r"附件\s*[0-9一二三四五六七八九十]", child_text)
                                else BlockKind.PARAGRAPH
                            )
                            block = text_block(child, index + 1, child_path, kind, child_text)
                            if not is_unit:
                                table_parent = block.block_id
                        else:
                            raise _invalid()
                    loc = locator(index + 1, path)
                    if owner != path:
                        for child, child_path in deferred_notes:
                            text_block(
                                child,
                                index + 1,
                                child_path,
                                BlockKind.FOOTNOTE,
                                metadata[child_path],
                            )
                        continue
                    loc = loc.model_copy(update={"page_end": group[-1].page + 1})
                    cells, gaps = _joined_cells(group, pdf)
                    for page_number in set(gaps):
                        failed.append(locator(page_number))
                        warnings.append(
                            f"PDF page {page_number}: table seam could not be confirmed"
                        )
                    blocks.append(
                        TableBlock(
                            block_id=loc.block_ids[0],
                            parent_id=table_parent,
                            locator=loc,
                            cells=cells,
                            unit_block_ids=units,
                            footnote_block_ids=notes,
                        )
                    )
                    for child, child_path in deferred_notes:
                        text_block(
                            child, index + 1, child_path, BlockKind.FOOTNOTE, metadata[child_path]
                        )
                    if all(c.header_role == HeaderRole.UNKNOWN for c in cells):
                        warnings.append(
                            f"PDF page {index + 1}: HTML has no explicit table-header roles"
                        )
                elif node.type not in {"page_number", "header", "footer"}:
                    failed.append(locator(index + 1))
                    warnings.append(f"PDF page {index + 1}: unsupported layout block")
            if not processed:
                if page.discarded_blocks and all(
                    b.type in {"page_number", "header", "footer"} for b in page.discarded_blocks
                ):
                    warnings.append(
                        f"PDF page {index + 1}: only page furniture was retained by producer"
                    )
                else:
                    raster_page = pdf[index]
                    bitmap = raster_page.render(scale=1)
                    image = bitmap.to_pil().convert("L")
                    if image.getextrema() == (255, 255):
                        empty.append(locator(index + 1))
                    else:
                        failed.append(locator(index + 1))
                    image.close()
                    bitmap.close()
                    raster_page.close()
    try:
        return ParsedDocument(
            document_id=document_id,
            source_hash=source_ref.sha256,
            blocks=blocks,
            parse_report=ParseReport(
                page_count=page_count,
                failed_locations=failed,
                empty_locations=empty,
                warnings=warnings,
                coverage_status=CoverageStatus.PARTIAL if failed else CoverageStatus.COMPLETE,
                table_count=sum(isinstance(b, TableBlock) for b in blocks),
            ),
        )
    except ValidationError as error:
        raise _invalid() from error

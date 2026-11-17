"""Exact source slicing, table rendering and common immutable Chunk assembly."""

import re
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import cast
from uuid import UUID, uuid5

from intelligent_reimbursement_assitant.contracts.models import SourceLocator
from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    Chunk,
    ChunkBatch,
)
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.parser.domain.models import (
    DocumentBlock,
    HeaderRole,
    TableBlock,
    TableCell,
)

_NATURAL = re.compile(r"(?<=\n)|(?<=[。！？；;!?])|(?<=[^0-9０-９]\.)(?=\s)")
# Expressions whose meaning would be damaged by a character fallback inside them.
_ATOMIC = re.compile(
    r"(?:人民币|[￥¥$])?[-−－+＋]?[0-9０-９]+(?:[.．,/／:：-][0-9０-９]+)*(?:年[0-9０-９]+月[0-9０-９]+日?|月[0-9０-９]+日)?"
    r"(?:[ \t]*(?:[%％]|元(?:[/／][人天日次]+(?:[·/／][人天日次]+)*)?|万元|美元|人民币|公里|千米|公斤|㎡|平方米|天|日|小时))?[①-⑳※*]*"
    r"|(?:不得|不能|不可|不予|严禁|禁止|无需|不必|不超过|不低于|未)[\u4e00-\u9fff]{0,4}"
    r"|[①-⑳※]+"
)


@dataclass(frozen=True)
class _Text:
    text: str
    locators: tuple[SourceLocator, ...]
    context: tuple[_Text, ...] = ()


@dataclass(frozen=True)
class _Rendered:
    body: str
    retrieval: str
    locators: list[SourceLocator]


def _text(block: DocumentBlock) -> _Text:
    return _Text(block.text, (block.locator,))


def _unique_locators(parts: tuple[_Text, ...]) -> list[SourceLocator]:
    result: list[SourceLocator] = []
    for part in parts:
        for locator in part.locators:
            if locator not in result:
                result.append(locator)
    return result


def render_chunk(piece: _Text, titles: tuple[_Text, ...]) -> _Rendered:
    body = piece.text
    if piece.context:
        body = "[适用上下文]\n" + "\n\n".join(p.text for p in piece.context) + "\n[正文]\n" + body
    retrieval = "\n\n".join([*(title.text for title in titles), body])
    return _Rendered(body, retrieval, _unique_locators(titles + piece.context + (piece,)))


def _too_large() -> ExecutionError:
    return ExecutionError(
        ErrorCode.CHUNKING_CONTENT_TOO_LARGE,
        "PERMANENT",
        "Necessary expression or context exceeds max_tokens",
    )


def _long_slices(piece: _Text, fits: Callable[[_Text], bool]) -> Iterator[_Text]:
    text = piece.text
    if not text:
        # Empty cells still carry mandatory row/header/footnote context.
        raise _too_large()
    protected = [(m.start(), m.end()) for m in _ATOMIC.finditer(text)]
    safe = [0]
    position = 0
    for start, end in protected:
        safe.extend(range(position + 1, start + 1))
        safe.append(end)
        position = end
    safe.extend(range(position + 1, len(text) + 1))
    natural = {m.start() for m in _NATURAL.finditer(text)}
    offset = 0
    first = 1
    while offset < len(text):
        # Search source boundaries, never decode tokens back into source text.
        best = first - 1
        probe = first
        while True:
            if not fits(_Text(text[offset : safe[probe]], piece.locators, piece.context)):
                upper = probe - 1
                break
            best = probe
            if probe == len(safe) - 1:
                upper = best
                break
            probe = min(len(safe) - 1, first + 2 * (probe - first + 1))
        lo, hi = best + 1, upper
        while lo <= hi:
            mid = (lo + hi) // 2
            candidate = _Text(text[offset : safe[mid]], piece.locators, piece.context)
            if fits(candidate):
                best, lo = mid, mid + 1
            else:
                hi = mid - 1
        if best < first:
            raise _too_large()
        end_index = best
        if safe[best] < len(text):
            natural_indices = [i for i in range(first, best + 1) if safe[i] in natural]
            if natural_indices:
                paragraphs = [i for i in natural_indices if text[safe[i] - 1] == "\n"]
                end_index = (paragraphs or natural_indices)[-1]
        end = safe[end_index]
        yield _Text(text[offset:end], piece.locators, piece.context)
        offset, first = end, end_index + 1


def _cell_label(cell: TableCell) -> str:
    return f"单元格[行{cell.row_index + 1}-{cell.row_index + cell.row_span},列{cell.column_index + 1}-{cell.column_index + cell.column_span},角色={cell.header_role.value}]"


def _table_headers(table: TableBlock) -> tuple[list[TableCell], TableCell | None]:
    # These observed classification headings identify a header band, without
    # changing UNKNOWN roles or treating arbitrary blank columns as inherited.
    category = next(
        (
            c
            for c in table.cells
            if c.row_index == 0
            and re.sub(r"\s+", "", c.text).replace("（", "(").replace("）", ")")
            in {"国家(地区)", "地区(城市)"}
        ),
        None,
    )
    header_end = category.row_index + category.row_span if category else 0
    return [
        c
        for c in table.cells
        if c.header_role in {HeaderRole.COLUMN, HeaderRole.BOTH} or c.row_index < header_end
    ], category


def render_table(
    table: TableBlock,
    by_id: dict[str, DocumentBlock | TableBlock],
    selected: list[TableCell],
    titles: tuple[_Text, ...] = (),
    context: tuple[_Text, ...] = (),
    overrides: Mapping[tuple[int, int], str] | None = None,
) -> _Rendered:
    lines: list[str] = []
    locators = [table.locator]
    headers, category = _table_headers(table)
    header_coordinates = {(c.row_index, c.column_index) for c in headers}
    original_rows = {
        c.row_index
        for c in selected
        if c.column_index == 0 and (c.row_index, c.column_index) not in header_coordinates
    }
    for ref in table.unit_block_ids + table.footnote_block_ids:
        block = cast(DocumentBlock, by_id[ref])
        label = "整表单位" if ref in table.unit_block_ids else "整表条件/脚注"
        lines.append(f"[{label}]\n{block.text}")
        locators.append(block.locator)
    for cell in selected:
        label = _cell_label(cell)
        if (
            category is not None
            and cell.column_index == category.column_index
            and (cell.row_index, cell.column_index) not in header_coordinates
            and cell.text.strip()
            and cell.row_index not in original_rows
        ):
            label = "[行组上下文] " + label
        text = (
            overrides.get((cell.row_index, cell.column_index), cell.text)
            if overrides is not None
            else cell.text
        )
        lines.append(f"{label}:\n{text}")
        for ref in cell.footnote_block_ids:
            block = cast(DocumentBlock, by_id[ref])
            lines.append(f"[{label}脚注]\n{block.text}")
            locators.append(block.locator)
    return render_chunk(_Text("\n\n".join(lines), tuple(locators), context), titles)


def assemble_chunks(
    rendered: Iterable[_Rendered], document_id: UUID, source_hash: str, generation_id: UUID
) -> ChunkBatch:
    return ChunkBatch(
        chunks=[
            Chunk(
                chunk_id=str(uuid5(generation_id, str(ordinal))),
                document_id=document_id,
                source_hash=source_hash,
                generation_id=generation_id,
                ordinal=ordinal,
                body_text=part.body,
                retrieval_text=part.retrieval,
                locators=part.locators,
            )
            for ordinal, part in enumerate(rendered)
        ]
    )


def select_table_cells(
    table: TableBlock, group: list[TableCell], start: int, end: int
) -> list[TableCell]:
    cells = table.cells
    column_headers, category = _table_headers(table)
    header_coordinates = {(c.row_index, c.column_index) for c in column_headers}
    identity_columns = {
        col
        for header in column_headers
        if category is not None
        and (header == category or re.sub(r"\s+", "", header.text) in {"序号", "城市", "币种"})
        for col in range(header.column_index, header.column_index + header.column_span)
    }
    selected = list(group)
    selected_coordinates = {(c.row_index, c.column_index) for c in group}
    selected_columns = identity_columns | {
        col for c in group for col in range(c.column_index, c.column_index + c.column_span)
    }
    for cell in cells:
        coordinate = cell.row_index, cell.column_index
        intersects_rows = cell.row_index <= end and cell.row_index + cell.row_span > start
        columns = range(cell.column_index, cell.column_index + cell.column_span)
        intersects_columns = any(col in selected_columns for col in columns)
        if (
            (intersects_rows and cell.row_span > 1 and coordinate not in header_coordinates)
            or (coordinate in header_coordinates and intersects_columns)
            or (cell.header_role in {HeaderRole.ROW, HeaderRole.BOTH} and intersects_rows)
            or (intersects_rows and any(col in identity_columns for col in columns))
        ):
            if coordinate not in selected_coordinates:
                selected.append(cell)
                selected_coordinates.add(coordinate)
    if category is not None:
        header_end = category.row_index + category.row_span
        column_count = max(c.column_index + c.column_span for c in cells)
        current: TableCell | None = None
        for cell in sorted(cells, key=lambda c: (c.row_index, c.column_index)):
            if cell.row_index < header_end or cell.row_index > end:
                continue
            is_category = cell.column_index == category.column_index and bool(cell.text.strip())
            is_division = cell.column_span == column_count and bool(cell.text.strip())
            if cell.row_index <= start and (is_category or is_division):
                current = cell if is_category else None
            elif (
                cell.row_index > start
                and is_category
                and (cell.row_index, cell.column_index) not in selected_coordinates
            ):
                selected.append(cell)
                selected_coordinates.add((cell.row_index, cell.column_index))
        if (
            current is not None
            and (current.row_index, current.column_index) not in selected_coordinates
        ):
            selected.append(current)
    return sorted(selected, key=lambda c: (c.row_index, c.column_index))

"""Recover PDF table chains using source-page evidence before unified mapping."""

import re
import unicodedata
from contextlib import ExitStack
from dataclasses import dataclass, field

import pypdfium2 as pdfium
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.node import DocItem, NodeItem
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.table.table_data import RichTableCell, TableCell, TableData
from docling_core.types.doc.items.text import TextItem
from docling_core.types.doc.labels import DocItemLabel

from intelligent_reimbursement_assitant.parser.adapters.docling.pdf import (
    _rules,
    table_boundary_cells,
    table_column_edges,
)

_CONTINUED = re.compile(r"续表|接上页|续上页|\bcontinued\b|\bcont\.", re.IGNORECASE)
_TABLE_NUMBER = re.compile(
    r"(?:附件|附表|表|\btable)\s*([\d一二三四五六七八九十百]+)", re.IGNORECASE
)
_SERIAL = re.compile(r"序号|No\.?", re.IGNORECASE)
_NUMERIC_VALUE = re.compile(r"(?:[A-Z]{3}|人民币|[¥￥$€£])?[+-]?\d")
UNIT_LABEL = re.compile(r"单位[：:]|\bunit\s*:", re.IGNORECASE)


def _compare(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFC", text))


def _continuation_title(node: DocItem) -> bool:
    if not isinstance(node, TextItem) or not _CONTINUED.search(node.text):
        return False
    return bool(
        node.label in {DocItemLabel.CAPTION, DocItemLabel.SECTION_HEADER, DocItemLabel.TITLE}
        or _TABLE_NUMBER.match(node.text.strip())
        or re.fullmatch(
            r"[（(]?(?:续表|接上页|续上页|continued|cont\.)[）)]?", node.text.strip(), re.IGNORECASE
        )
    )


@dataclass
class TableContinuations:
    """Call-local assembly facts; only existing parser contracts leave the adapter."""

    tables: dict[str, TableItem] = field(default_factory=dict)
    owners: dict[str, str] = field(default_factory=dict)
    cell_notes: dict[tuple[str, int, int], list[str]] = field(default_factory=dict)
    titles: set[str] = field(default_factory=set)
    failed: list[TableItem] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _header_end(table: TableItem) -> int:
    cells = table.data.table_cells
    headers = [c for c in cells if c.column_header or _SERIAL.fullmatch(_compare(c.text))]
    end = max((c.end_row_offset_idx for c in headers if c.start_row_offset_idx == 0), default=0)
    # Follow only a contiguous leading band, never isolated body-row predictions.
    while end:
        extended = max(
            (c.end_row_offset_idx for c in headers if c.start_row_offset_idx <= end), default=end
        )
        if extended == end:
            break
        end = extended
    return end


def _header_signature(table: TableItem, end: int):
    return sorted(
        (
            c.start_row_offset_idx,
            c.end_row_offset_idx,
            c.start_col_offset_idx,
            c.end_col_offset_idx,
            _compare(c.text),
        )
        for c in table.data.table_cells
        if c.start_row_offset_idx < end
    )


def _serial_boundary(first: TableItem, previous: TableItem, incoming: TableItem, repeated: int):
    column = next(
        (
            c.start_col_offset_idx
            for c in first.data.table_cells
            if _SERIAL.fullmatch(_compare(c.text))
        ),
        None,
    )
    if column is None:
        return False, False, False
    old = [
        _compare(c.text)
        for c in sorted(previous.data.table_cells, key=lambda c: c.start_row_offset_idx)
        if c.start_col_offset_idx == column and c.text.strip()
    ]
    new = [
        _compare(c.text)
        for c in sorted(incoming.data.table_cells, key=lambda c: c.start_row_offset_idx)
        if c.start_col_offset_idx == column
        and c.start_row_offset_idx >= repeated
        and c.text.strip()
    ]
    consecutive = bool(
        old
        and new
        and old[-1].isdecimal()
        and new[0].isdecimal()
        and int(new[0]) == int(old[-1]) + 1
    )
    leading = [c for c in incoming.data.table_cells if c.start_row_offset_idx == repeated]
    serial = next((c for c in leading if c.start_col_offset_idx == column), None)
    new_record = bool(
        consecutive
        and serial is not None
        and (serial.text.strip() or any(_NUMERIC_VALUE.match(_compare(c.text)) for c in leading))
    )
    # A blank record key followed by the next key identifies the previous record;
    # a text-only leading row has no new numeric record values to collapse.
    text_carry = bool(
        consecutive
        and serial is not None
        and not serial.text.strip()
        and sum(bool(c.text.strip()) for c in leading) == 1
        and not any(re.search(r"\d", c.text) for c in leading)
    )
    return consecutive, new_record, text_carry


def _caption_text(table: TableItem, doc: DoclingDocument, *, units: bool = False) -> list[str]:
    return [
        node.text
        for ref in table.captions
        if isinstance(node := ref.resolve(doc), TextItem)
        and bool(UNIT_LABEL.search(node.text)) == units
    ]


def _title_conflict(first: list[str], incoming: list[str]) -> bool:
    old_numbers = {
        m.group(0).lower().replace(" ", "") for t in first for m in _TABLE_NUMBER.finditer(t)
    }
    new_numbers = {
        m.group(0).lower().replace(" ", "") for t in incoming for m in _TABLE_NUMBER.finditer(t)
    }
    if old_numbers and new_numbers:
        return old_numbers != new_numbers
    new_titles = [t for t in incoming if not _CONTINUED.search(t)]
    return bool(new_titles and {_compare(t) for t in first} != {_compare(t) for t in new_titles})


def _join_cells(
    assembled: list[TableCell],
    incoming: TableItem,
    repeated: int,
    before: dict[tuple[int, int], tuple[int, str | None]],
    after: dict[tuple[int, int], tuple[int, str | None]],
    origins: dict[tuple[int, int], set[str]],
    new_record: bool,
    text_carry: bool,
) -> tuple[list[TableCell], dict[tuple[int, int], set[str]], bool]:
    """Append records and restore independently confirmed cell continuations."""
    offset = max(c.end_row_offset_idx for c in assembled)
    remaining = [c for c in incoming.data.table_cells if c.start_row_offset_idx >= repeated]
    pairs: list[tuple[TableCell, TableCell]] = []
    gap = False
    for new in (c for c in remaining if c.start_row_offset_idx == repeated):
        if new_record and _NUMERIC_VALUE.match(_compare(new.text)):
            # A new serial record proves these numeric values belong to a new row.
            continue
        old = next(
            (
                c
                for c in assembled
                if c.end_row_offset_idx == offset
                and (c.start_col_offset_idx, c.col_span) == (new.start_col_offset_idx, new.col_span)
            ),
            None,
        )
        if old is None:
            if (
                text_carry
                or after.get((new.start_row_offset_idx, new.start_col_offset_idx), (-1, None))[0]
                == 0
            ):
                gap = True
            continue
        # 'before' uses the previous fragment's local row numbers, not logical rows.
        old_evidence = next(
            (value for (_, col), value in before.items() if col == old.start_col_offset_idx),
            (-1, None),
        )
        new_evidence = after.get((new.start_row_offset_idx, new.start_col_offset_idx), (-1, None))
        if old_evidence[0] != 0 and new_evidence[0] != 0 and not text_carry:
            continue
        if (
            text_carry
            and old_evidence[0] == new_evidence[0] == 1
            and new.text.strip()
            and _compare(old.text) == _compare(new.text)
        ):
            # Repeated display context is not evidence of a word split.
            continue
        if (
            (
                old_evidence[0] == new_evidence[0] == 0
                or (text_carry and old_evidence[0] >= 0 and new_evidence[0] >= 0)
            )
            and old_evidence[1] is not None
            and new_evidence[1] is not None
            and _compare(new.text) == _compare(new_evidence[1])
            and (
                _compare(old.text).endswith(_compare(old_evidence[1]))
                if old_evidence[1]
                else not old.text.strip()
            )
        ):
            pairs.append((old, new))
        else:
            gap = True
    merged = {(c.start_row_offset_idx, c.start_col_offset_idx) for _, c in pairs}
    fold = bool(pairs) and all(
        (c.start_row_offset_idx, c.start_col_offset_idx) in merged
        for c in remaining
        if c.start_row_offset_idx == repeated
    )
    cells = list(assembled)
    sources = {key: set(value) for key, value in origins.items()}
    for old, new in pairs:
        coordinate = old.start_row_offset_idx, old.start_col_offset_idx
        end = offset + new.end_row_offset_idx - repeated - int(fold)
        cells[cells.index(old)] = old.model_copy(
            update={
                "text": old.text + new.text,
                "row_span": end - old.start_row_offset_idx,
                "end_row_offset_idx": end,
            }
        )
        sources[coordinate].add(incoming.self_ref)
    for cell in remaining:
        if (cell.start_row_offset_idx, cell.start_col_offset_idx) in merged:
            continue
        start = cell.start_row_offset_idx + offset - repeated - int(fold)
        end = cell.end_row_offset_idx + offset - repeated - int(fold)
        cells.append(
            cell.model_copy(update={"start_row_offset_idx": start, "end_row_offset_idx": end})
        )
        sources[start, cell.start_col_offset_idx] = {incoming.self_ref}
    return cells, sources, gap


def recover_table_continuations(
    raw: bytes, document: DoclingDocument, sequence: list[NodeItem]
) -> TableContinuations:
    result = TableContinuations()
    reading = []
    for node in sequence:
        if not isinstance(node, DocItem) or node.label in {
            DocItemLabel.PAGE_HEADER,
            DocItemLabel.PAGE_FOOTER,
        }:
            continue
        parent = node.parent
        while parent is not None and not isinstance(parent.resolve(document), TableItem):
            parent = parent.resolve(document).parent
        if parent is None:
            reading.append(node)
    positions = [(index, node) for index, node in enumerate(reading) if isinstance(node, TableItem)]
    if len(positions) < 2:
        return result
    with ExitStack() as resources:
        pdf = resources.enter_context(pdfium.PdfDocument(raw))
        pages = {}
        geometries = {}
        chains: dict[str, list[TableItem]] = {}
        origins: dict[str, dict[tuple[int, int], set[str]]] = {}
        for _, table in positions:
            chains[table.self_ref] = [table]
            origins[table.self_ref] = {
                (c.start_row_offset_idx, c.start_col_offset_idx): {table.self_ref}
                for c in table.data.table_cells
            }
            if len(table.prov) != 1 or not table.data.table_cells:
                continue
            page_no = table.prov[0].page_no
            if page_no not in pages:
                page = pdf[page_no - 1]
                resources.callback(page.close)
                text = page.get_textpage()
                resources.callback(text.close)
                pages[page_no] = page, text, _rules(page)
            page, _, rules = pages[page_no]
            geometries[table.self_ref] = table_column_edges(page, rules, table, document)
        for (prev_pos, previous), (pos, incoming) in zip(positions, positions[1:], strict=False):
            if len(previous.prov) != 1 or len(incoming.prov) != 1:
                continue
            if previous.prov[0].page_no + 1 != incoming.prov[0].page_no:
                continue
            owner = result.owners.get(previous.self_ref, previous.self_ref)
            first = chains[owner][0]
            assembled = result.tables.get(owner, first)
            linked = {r.cref for t in (previous, incoming) for r in [*t.captions, *t.footnotes]}
            between = reading[prev_pos + 1 : pos]
            titles = _caption_text(incoming, document) + [
                n.text for n in between if isinstance(n, TextItem) and _continuation_title(n)
            ]
            continued = any(_CONTINUED.search(t) for t in titles)
            blocked = any(n.self_ref not in linked and not _continuation_title(n) for n in between)
            if blocked or _title_conflict(_caption_text(assembled, document), titles):
                continue
            first_edges, incoming_edges = (
                geometries.get(first.self_ref),
                geometries.get(incoming.self_ref),
            )
            layout_matches = (
                first.data.num_cols == incoming.data.num_cols
                and first.data.orientation == incoming.data.orientation
                and first_edges is not None
                and incoming_edges is not None
                and len(first_edges) == len(incoming_edges)
                and all(
                    abs(a - b) <= 0.005 for a, b in zip(first_edges, incoming_edges, strict=True)
                )
            )
            units = {_compare(t) for t in _caption_text(assembled, document, units=True)}
            new_units = {_compare(t) for t in _caption_text(incoming, document, units=True)}
            if not layout_matches or (units and new_units and units != new_units):
                if continued:
                    result.failed.extend([previous, incoming])
                    result.warnings.append(
                        f"PDF page {incoming.prov[0].page_no}: explicit table continuation has incompatible or unavailable columns/units"
                    )
                continue
            header_end = _header_end(first)
            repeated = (
                header_end
                if header_end
                and _header_signature(first, header_end) == _header_signature(incoming, header_end)
                else 0
            )
            if (
                header_end
                and not repeated
                and any(
                    c.start_row_offset_idx == 0 and _SERIAL.fullmatch(_compare(c.text))
                    for c in incoming.data.table_cells
                )
            ):
                if continued:
                    result.failed.extend([previous, incoming])
                    result.warnings.append(
                        "An explicit PDF table continuation changes its column headers"
                    )
                continue
            if repeated and any(
                c.start_row_offset_idx < repeated < c.end_row_offset_idx
                for c in incoming.data.table_cells
            ):
                result.failed.extend([previous, incoming])
                result.warnings.append("Repeated PDF table header crosses a data-cell span")
                continue
            before_page, before_text, before_rules = pages[previous.prov[0].page_no]
            after_page, after_text, after_rules = pages[incoming.prov[0].page_no]
            before = table_boundary_cells(
                before_page,
                before_text,
                before_rules,
                previous,
                document,
                previous.data.num_rows - 1,
                top=False,
            )
            after = table_boundary_cells(
                after_page, after_text, after_rules, incoming, document, repeated, top=True
            )
            open_seam = any(
                a[0] == b[0] == 0
                for (_, old_col), a in before.items()
                for (_, new_col), b in after.items()
                if old_col == new_col
            )
            serial_continues, new_record, text_carry = _serial_boundary(
                first, previous, incoming, repeated
            )
            if not (continued or serial_continues or text_carry or open_seam):
                result.warnings.append(
                    f"PDF pages {previous.prov[0].page_no}-{incoming.prov[0].page_no}: table similarity does not establish continuation"
                )
                continue
            if any(
                isinstance(c, RichTableCell)
                for t in (assembled, incoming)
                for c in t.data.table_cells
            ):
                result.failed.extend([previous, incoming])
                result.warnings.append(
                    "Cross-page rich table cells cannot be recovered without losing their native structure"
                )
                continue
            cells, sources, gap = _join_cells(
                assembled.data.table_cells,
                incoming,
                repeated,
                before,
                after,
                origins[owner],
                new_record,
                text_carry,
            )
            if gap:
                result.failed.extend([previous, incoming])
                result.warnings.append(
                    f"PDF page {incoming.prov[0].page_no}: cross-page table cells could not be recovered from native text"
                )
            result.owners[incoming.self_ref] = owner
            captions = {ref.cref: ref for t in (assembled, incoming) for ref in t.captions}
            result.tables[owner] = assembled.model_copy(
                update={
                    "data": TableData(
                        table_cells=cells,
                        num_rows=max(c.end_row_offset_idx for c in cells),
                        num_cols=first.data.num_cols,
                        orientation=first.data.orientation,
                    ),
                    "prov": [*assembled.prov, *incoming.prov],
                    "captions": list(captions.values()),
                }
            )
            origins[owner] = sources
            chains[owner].append(incoming)
            result.titles.update(n.self_ref for n in between if _continuation_title(n))
            result.titles.update(
                ref.cref
                for ref in incoming.captions
                if isinstance(n := ref.resolve(document), TextItem) and _CONTINUED.search(n.text)
            )
        for owner, table in result.tables.items():
            chain = chains[owner]
            whole_notes = []
            for source in chain:
                for ref in source.footnotes:
                    note = ref.resolve(document)
                    if not isinstance(note, TextItem):
                        continue
                    if re.search(
                        r"全表|整表|\b(?:entire|whole) table\b"
                        r"|^\s*(?:注[：:]?\s*)?本表(?:均|统一|适用|仅适用)"
                        r"|^\s*this table (?:applies|uses|is applicable|is limited)\b",
                        note.text,
                        re.IGNORECASE,
                    ):
                        whole_notes.append(ref)
                        continue
                    targets = [
                        key
                        for key, members in origins[owner].items()
                        if members == {source.self_ref} and key[0] >= _header_end(chain[0])
                    ]
                    if not targets or any(
                        source.self_ref in members and len(members) > 1
                        for members in origins[owner].values()
                    ):
                        result.failed.append(source)
                        result.warnings.append(
                            "A page-local table footnote cannot be assigned without extending its scope"
                        )
                    for row, col in targets:
                        result.cell_notes.setdefault((owner, row, col), []).append(ref.cref)
            table.footnotes = whole_notes
    return result

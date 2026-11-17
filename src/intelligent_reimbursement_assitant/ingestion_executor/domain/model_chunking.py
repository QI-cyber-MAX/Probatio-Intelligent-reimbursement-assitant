"""Program-owned chapters and strict validation of model-selected source ranges."""

import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from itertools import groupby
from typing import cast

from pydantic import BaseModel, ConfigDict, JsonValue

from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.ingestion_executor.domain.chunking_content import (
    _long_slices,
    _Rendered,
    _Text,
    _text,
    _too_large,
    render_chunk,
    render_table,
    select_table_cells,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.chunking_prompts import (
    CHUNKING_SYSTEM_PROMPT,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.config import ChunkingSettings
from intelligent_reimbursement_assitant.ingestion_executor.domain.normalization import (
    NormalizedDocument,
)
from intelligent_reimbursement_assitant.parser.domain.models import (
    BlockKind,
    DocumentBlock,
    TableBlock,
    TableCell,
)

_CHAPTER = re.compile(
    r"^[ \t]*(?:第[零〇一二三四五六七八九十百0-9０-９]+章|附件\s*[一二三四五六七八九十0-9０-９]+[：:、.\s]|[一二三四五六七八九十百]+[、．])",
    re.M,
)
_TOC = re.compile(r"[.．·…]{2,}\s*\d+\s*$", re.M)


@dataclass(frozen=True)
class SourceFragment:
    block_id: str
    start: int
    end: int
    cell: tuple[int, int] | None = None


@dataclass(frozen=True)
class Section:
    section_id: str
    parts: tuple[SourceFragment | str, ...]
    titles: tuple[_Text, ...]


@dataclass(frozen=True)
class Unit:
    unit_id: str
    section_id: str
    fragments: tuple[SourceFragment, ...]
    rendered: _Rendered


class GroupResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    start_unit_id: str
    end_unit_id: str
    context_unit_ids: list[str]


class SectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    section_id: str
    groups: list[GroupResponse]


class GroupsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    sections: list[SectionResponse]


def invalid_response() -> ExecutionError:
    return ExecutionError(
        ErrorCode.CHUNKING_MODEL_INVALID_RESPONSE,
        "PERMANENT",
        "Model chunking response is invalid",
    )


def build_sections(normalized: NormalizedDocument) -> list[Section]:
    headings = [
        b
        for b in normalized.blocks
        if isinstance(b, DocumentBlock) and b.kind == BlockKind.HEADING and not _TOC.search(b.text)
    ]
    explicit = any(_CHAPTER.match(b.text.strip()) for b in headings)
    heading_ids = {b.block_id for b in headings}
    roots = [b for b in headings if b.parent_id not in heading_ids]
    root_ids = {b.block_id for b in roots}

    def title_key(text: str) -> str:
        return re.sub(r"\s+", "", _CHAPTER.sub("", text.strip()))

    toc_titles = {
        title_key(re.split(r"[.．·…]{2,}", line)[0])
        for b in normalized.blocks
        if isinstance(b, DocumentBlock)
        for line in b.text.splitlines()
        if _TOC.search(line)
    }
    has_numbered_chapters = any(
        re.search(r"^[ \t]*第[零〇一二三四五六七八九十百0-9０-９]+章", b.text, re.M)
        for b in normalized.blocks
        if isinstance(b, DocumentBlock)
    )
    sections: list[Section] = []
    current: list[SourceFragment | str] = []
    titles: tuple[_Text, ...] = ()

    def flush():
        if current:
            sections.append(Section(f"section_{len(sections)}", tuple(current), titles))
            current.clear()

    for block in normalized.blocks:
        if isinstance(block, TableBlock):
            current.append(block.block_id)
            continue
        boundaries: dict[int, str] = {}
        for match in _CHAPTER.finditer(block.text):
            line_end = block.text.find("\n", match.start())
            title = block.text[match.start() : line_end if line_end >= 0 else len(block.text)]
            if _TOC.search(title):
                continue
            numbered = title.lstrip().startswith(("第", "附件"))
            top_heading = block.block_id in root_ids and not has_numbered_chapters
            standalone = (
                not explicit
                and not has_numbered_chapters
                and block.parent_id is None
                and len(title.strip()) <= 90
                and not title.rstrip().endswith(("。", "；", "，"))
            )
            if numbered or top_heading or title_key(title) in toc_titles or standalone:
                boundaries[match.start()] = title
        if (
            not boundaries
            and block.block_id in root_ids
            and not _TOC.search(block.text)
            and (not explicit or title_key(block.text) in toc_titles)
        ):
            boundaries[0] = block.text
        if len(boundaries) == 1 and 0 in boundaries and block.kind == BlockKind.HEADING:
            boundaries[0] = block.text
        offsets = sorted({0, *boundaries, len(block.text)})
        for start, end in zip(offsets, offsets[1:], strict=False):
            if start in boundaries:
                flush()
                titles = (_Text(boundaries[start], (block.locator,)),)
            current.append(SourceFragment(block.block_id, start, end))
    flush()
    return sections


def build_units(
    normalized: NormalizedDocument,
    sections: list[Section],
    settings: ChunkingSettings,
    count_tokens: Callable[[str], int],
) -> list[Unit]:
    by_id = {b.block_id: b for b in normalized.blocks}
    units: list[Unit] = []

    def fits(rendered: _Rendered) -> bool:
        return (
            max(count_tokens(rendered.body), count_tokens(rendered.retrieval))
            <= settings.max_tokens
        )

    for section in sections:

        def append(fragments: tuple[SourceFragment, ...], rendered: _Rendered, section=section):
            units.append(Unit(f"unit_{len(units)}", section.section_id, fragments, rendered))

        for source_part in section.parts:
            block_id = source_part if isinstance(source_part, str) else source_part.block_id
            block = by_id[block_id]
            if isinstance(source_part, SourceFragment):
                block = cast(DocumentBlock, block)
                text = block.text[source_part.start : source_part.end]
                if not text:
                    continue
                offset = source_part.start
                # Candidate source slices offer capacity; the model alone combines them.
                pieces = _long_slices(
                    _Text(text, (block.locator,)),
                    lambda p, section=section: fits(render_chunk(p, section.titles)),
                )
                for piece in pieces:
                    rendered = render_chunk(piece, section.titles)
                    if not fits(rendered):
                        raise _too_large()
                    append(
                        (SourceFragment(block_id, offset, offset + len(piece.text)),),
                        rendered,
                    )
                    offset += len(piece.text)
                continue
            block = cast(TableBlock, block)
            rows: dict[int, list[TableCell]] = {}
            parent = block.parent_id
            context: list[_Text] = []
            while parent is not None:
                ancestor = by_id[parent]
                if isinstance(ancestor, DocumentBlock) and ancestor.kind != BlockKind.HEADING:
                    context.insert(0, _text(ancestor))
                parent = ancestor.parent_id
            table_context = tuple(context)
            for cell in sorted(block.cells, key=lambda c: (c.row_index, c.column_index)):
                rows.setdefault(cell.row_index, []).append(cell)
            for row, cells in rows.items():
                selected = select_table_cells(block, cells, row, row)
                rendered = render_table(block, by_id, selected, section.titles, table_context)
                if fits(rendered):
                    append(
                        tuple(
                            SourceFragment(block_id, 0, len(c.text), (c.row_index, c.column_index))
                            for c in cells
                        ),
                        rendered,
                    )
                    continue
                for cell in cells:
                    selected = select_table_cells(block, [cell], row, row)
                    rendered = render_table(block, by_id, selected, section.titles, table_context)
                    if fits(rendered):
                        append(
                            (
                                SourceFragment(
                                    block_id, 0, len(cell.text), (cell.row_index, cell.column_index)
                                ),
                            ),
                            rendered,
                        )
                        continue
                    offset = 0
                    for piece in _long_slices(
                        _Text(cell.text, (block.locator,)),
                        lambda p, block=block, selected=selected, section=section, cell=cell, table_context=table_context: (
                            fits(
                                render_table(
                                    cast(TableBlock, block),
                                    by_id,
                                    selected,
                                    section.titles,
                                    table_context,
                                    overrides={(cell.row_index, cell.column_index): p.text},
                                )
                            )
                        ),
                    ):
                        rendered = render_table(
                            block,
                            by_id,
                            selected,
                            section.titles,
                            table_context,
                            overrides={(cell.row_index, cell.column_index): piece.text},
                        )
                        if not fits(rendered):
                            raise _too_large()
                        append(
                            (
                                SourceFragment(
                                    block_id,
                                    offset,
                                    offset + len(piece.text),
                                    (cell.row_index, cell.column_index),
                                ),
                            ),
                            rendered,
                        )
                        offset += len(piece.text)
    return units


def build_model_input(
    normalized: NormalizedDocument,
    sections: list[Section],
    units: list[Unit],
    settings: ChunkingSettings,
    count_tokens: Callable[[str], int],
) -> list[dict[str, JsonValue]]:
    by_id = {block.block_id: block for block in normalized.blocks}
    tables: dict[str, dict] = {}
    dependency_indices: dict[str, dict[tuple[tuple[int, int], ...], int]] = {}
    encoded_units: list[dict] = []
    for unit in units:
        encoded: dict = {
            "unit_id": unit.unit_id,
            "retrieval_tokens": count_tokens(unit.rendered.retrieval),
        }
        block = by_id[unit.fragments[0].block_id]
        if isinstance(block, DocumentBlock):
            encoded["text"] = unit.rendered.body
        else:
            if block.block_id not in tables:
                parents: list[str] = []
                parent = block.parent_id
                while parent is not None:
                    ancestor = by_id[parent]
                    if isinstance(ancestor, DocumentBlock) and ancestor.kind != BlockKind.HEADING:
                        parents.insert(0, ancestor.text)
                    parent = ancestor.parent_id
                tables[block.block_id] = {
                    "table_id": f"table_{len(tables)}",
                    "parent_context": parents,
                    "unit_texts": [
                        cast(DocumentBlock, by_id[ref]).text for ref in block.unit_block_ids
                    ],
                    "table_notes": [
                        cast(DocumentBlock, by_id[ref]).text for ref in block.footnote_block_ids
                    ],
                    "cells": [
                        [
                            c.row_index,
                            c.column_index,
                            c.row_span,
                            c.column_span,
                            c.text,
                            c.header_role.value,
                            [cast(DocumentBlock, by_id[ref]).text for ref in c.footnote_block_ids],
                        ]
                        for c in block.cells
                    ],
                    "dependency_sets": [],
                }
                dependency_indices[block.block_id] = {}
            table = tables[block.block_id]
            cells = {(c.row_index, c.column_index): c for c in block.cells}
            coordinates = [cast(tuple[int, int], f.cell) for f in unit.fragments]
            selected = [cells[coordinate] for coordinate in coordinates]
            dependencies = tuple(
                (c.row_index, c.column_index)
                for c in select_table_cells(
                    block,
                    selected,
                    min(c.row_index for c in selected),
                    max(c.row_index for c in selected),
                )
                if (c.row_index, c.column_index) not in coordinates
            )
            indices = dependency_indices[block.block_id]
            if dependencies not in indices:
                indices[dependencies] = len(indices)
                table["dependency_sets"].append(dependencies)
            encoded.update(
                table_id=table["table_id"],
                cell_slices=[
                    [*cast(tuple[int, int], f.cell), f.start, f.end] for f in unit.fragments
                ],
                dependency_set=indices[dependencies],
            )
        encoded_units.append(encoded)
    data = {
        "sections": [
            {
                "section_id": s.section_id,
                "titles": [title.text for title in s.titles],
                "unit_ids": [u.unit_id for u in units if u.section_id == s.section_id],
            }
            for s in sections
        ],
        "table_cell_fields": [
            "row",
            "column",
            "row_span",
            "column_span",
            "text",
            "header_role",
            "footnotes",
        ],
        "table_slice_fields": ["row", "column", "start", "end"],
        "tables": list(tables.values()),
        "units": encoded_units,
        "target_tokens": settings.target_tokens,
        "max_tokens": settings.max_tokens,
        "response_schema": GroupsResponse.model_json_schema(),
    }
    return [
        {"role": "system", "content": CHUNKING_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False, separators=(",", ":"))},
    ]


def validate_model_groups(
    response: GroupsResponse, sections: list[Section], units: list[Unit]
) -> list[tuple[list[Unit], list[Unit]]]:
    if [s.section_id for s in response.sections] != [s.section_id for s in sections]:
        raise invalid_response()
    by_id = {u.unit_id: u for u in units}
    result = []
    for selected, section in zip(response.sections, sections, strict=True):
        body = [u for u in units if u.section_id == section.section_id]
        positions = {u.unit_id: i for i, u in enumerate(body)}
        cursor = 0
        for group in selected.groups:
            start = positions.get(group.start_unit_id, -1)
            end = positions.get(group.end_unit_id, -1)
            if (
                start != cursor
                or end < start
                or any(ref not in by_id for ref in group.context_unit_ids)
                or len(set(group.context_unit_ids)) != len(group.context_unit_ids)
            ):
                raise invalid_response()
            result.append((body[start : end + 1], [by_id[ref] for ref in group.context_unit_ids]))
            cursor = end + 1
        if cursor != len(body):
            raise invalid_response()
    return result


def render_group(
    body: list[Unit],
    context: list[Unit],
    section: Section,
    by_id: dict[str, DocumentBlock | TableBlock],
) -> _Rendered:
    # Every body fragment is selected once by the external range validation.
    parts: list[_Rendered] = []
    for block_id, consecutive in groupby(body, key=lambda unit: unit.fragments[0].block_id):
        fragments = [fragment for unit in consecutive for fragment in unit.fragments]
        block = by_id[block_id]
        if isinstance(block, DocumentBlock):
            text = "".join(block.text[f.start : f.end] for f in fragments)
            parts.append(render_chunk(_Text(text, (block.locator,)), ()))
            continue
        cells = {(cell.row_index, cell.column_index): cell for cell in block.cells}
        selected: list[TableCell] = []
        overrides: dict[tuple[int, int], str] = {}
        for fragment in fragments:
            coordinate = cast(tuple[int, int], fragment.cell)
            cell = cells[coordinate]
            if cell not in selected:
                selected.append(cell)
            overrides[coordinate] = (
                overrides.get(coordinate, "") + cell.text[fragment.start : fragment.end]
            )
        selected = select_table_cells(
            block, selected, min(c.row_index for c in selected), max(c.row_index for c in selected)
        )
        parents: list[_Text] = []
        parent = block.parent_id
        while parent is not None:
            ancestor = by_id[parent]
            if isinstance(ancestor, DocumentBlock) and ancestor.kind != BlockKind.HEADING:
                parents.insert(0, _text(ancestor))
            parent = ancestor.parent_id
        parts.append(
            render_table(block, by_id, selected, context=tuple(parents), overrides=overrides)
        )
    body_text = "\n\n".join(part.body for part in parts)
    locators = tuple(loc for part in parts for loc in part.locators)
    contexts = tuple(_Text(u.rendered.retrieval, tuple(u.rendered.locators)) for u in context)
    return render_chunk(_Text(body_text, locators, contexts), section.titles)

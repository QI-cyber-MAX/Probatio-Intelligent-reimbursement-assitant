"""Structure-first chunking with offsets and hierarchy local to this run."""

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from uuid import UUID

from intelligent_reimbursement_assitant.document_ingestion_service.domain.models import (
    ChunkBatch,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.chunking_content import (
    _long_slices,
    _Rendered,
    _Text,
    _text,
    assemble_chunks,
    render_chunk,
    render_table,
    select_table_cells,
)
from intelligent_reimbursement_assitant.ingestion_executor.domain.config import ChunkingSettings
from intelligent_reimbursement_assitant.ingestion_executor.domain.normalization import (
    NormalizedDocument,
)
from intelligent_reimbursement_assitant.parser.domain.models import (
    BlockKind,
    DocumentBlock,
    HeaderRole,
    TableBlock,
    TableCell,
)

_NUM = r"零〇一二三四五六七八九十百千万两壹贰貳叁參肆伍陆陸柒捌玖拾佰仟萬亿億0-9０-９"
_EXPLICIT = re.compile(rf"(?:^[ \t]*|(?<=[。；！？])[ \t]*)(第[{_NUM}]+(?P<kind>[章节条]))", re.M)
_ITEM = re.compile(
    r"^[ \t]*(?P<mark>"
    r"[一二三四五六七八九十百]+[、．.]|"
    r"[（(][一二三四五六七八九十百0-9０-９]+[）)]|"
    r"[①-⑳]|[0-9０-９]+[、）)]|"
    r"[0-9０-９]+(?:[.．][0-9０-９]+)+(?=\s)|"
    r"[0-9０-９]+[.．](?![0-9０-９]))",
    re.M,
)


@dataclass
class _Scope:
    titles: tuple[_Text, ...]
    parts: list[_Text | TableBlock] = field(default_factory=list)
    intro: tuple[_Text, ...] = ()


def _item_rank(mark: str) -> int:
    if mark[0] in "（(":
        return 3 if mark[1].isdigit() else 1
    if mark[0] in "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳":
        return 4
    if mark[0].isdigit():
        return 2 + len(re.findall(r"[.．](?=[0-9０-９])", mark))
    return 0


def _segments(
    block: DocumentBlock, adjacent_numbering: bool = False
) -> Iterator[tuple[str, str, int]]:
    """Yield exact slices; ambiguous dotted numbers require structural evidence."""
    starts: dict[int, tuple[str, int]] = {}
    for match in _EXPLICIT.finditer(block.text):
        starts[match.start()] = (match["kind"], 0)
    items = list(_ITEM.finditer(block.text))
    siblings = sum(m["mark"][0].isdigit() and m["mark"][-1] in ".．" for m in items)
    for match in items:
        mark = match["mark"]
        dotted = mark[-1] in ".．" and mark[0].isdigit()
        tail = block.text[match.end() :]
        levels = re.split(r"[.．]", mark)
        if len(levels) > 1 and levels[-1]:
            # Dates and decimal values with units have no list semantics.
            if (len(levels[0]) == 4 and len(levels) == 3) or re.match(r"\s*[%％元年月日]", tail):
                continue
            if not (
                len(items) > 1
                or adjacent_numbering
                or starts
                or block.parent_id is not None
                or block.kind in {BlockKind.LIST, BlockKind.HEADING}
            ):
                continue
        if dotted and not (
            tail[:1].isspace()
            or siblings > 1
            or adjacent_numbering
            or block.kind in {BlockKind.LIST, BlockKind.HEADING}
        ):
            continue
        starts.setdefault(match.start(), ("item", _item_rank(mark)))
    starts.setdefault(0, ("text", 0))
    ordered = sorted(starts)
    for index, start in enumerate(ordered):
        end = ordered[index + 1] if index + 1 < len(ordered) else len(block.text)
        kind, rank = starts[start]
        yield block.text[start:end], kind, rank


def split_structure(normalized: NormalizedDocument) -> list[_Scope]:
    by_id = {block.block_id: block for block in normalized.blocks}
    scopes: list[_Scope] = []
    titles: list[_Text] = []
    title_blocks: list[DocumentBlock] = []
    scope = _Scope(())
    path: list[str] | None = None
    clause = False
    item_stack: list[tuple[int, _Text]] = []

    def flush() -> None:
        nonlocal scope, item_stack
        if scope.parts:
            scopes.append(scope)
        scope = _Scope(tuple(titles))
        item_stack = []

    def change_titles(new_titles: list[_Text], new_blocks: list[DocumentBlock]) -> None:
        nonlocal titles, title_blocks, scope
        if not scope.parts and titles and titles[-1] not in new_titles:
            scope.parts.append(titles[-1])
            scope.titles = tuple(titles[:-1])
        flush()
        titles, title_blocks = new_titles, new_blocks
        scope = _Scope(tuple(titles))

    for block_index, block in enumerate(normalized.blocks):
        # Existing section paths delimit scopes; their strings are never rendered as new source text.
        if path is not None and path != block.locator.section_path:
            kept = [
                (title, b)
                for title, b in zip(titles, title_blocks, strict=True)
                if block.locator.section_path[: len(b.locator.section_path)]
                == b.locator.section_path
            ]
            change_titles([t for t, _ in kept], [b for _, b in kept])
            clause = False
        path = block.locator.section_path
        ancestors: list[DocumentBlock] = []
        parent = block.parent_id
        while parent is not None:
            ancestor = by_id[parent]
            if isinstance(ancestor, DocumentBlock):
                ancestors.insert(0, ancestor)
            parent = ancestor.parent_id
        parent_titles = [_text(b) for b in ancestors if b.kind == BlockKind.HEADING]
        parents = tuple(
            _text(b) for b in ancestors if b.kind != BlockKind.HEADING and b.text.strip()
        )
        if parent_titles and tuple(parent_titles) != scope.titles[: len(parent_titles)]:
            change_titles(parent_titles, [b for b in ancestors if b.kind == BlockKind.HEADING])
        if isinstance(block, TableBlock):
            scope.parts.append(block)
            continue
        if not block.text.strip():
            # Whitespace within meaningful blocks is retained in the exact slices below.
            continue
        adjacent_numbering = any(
            isinstance(neighbor, DocumentBlock) and _ITEM.match(neighbor.text)
            for neighbor in normalized.blocks[max(0, block_index - 1) : block_index]
            + normalized.blocks[block_index + 1 : block_index + 2]
        )
        for text, kind, rank in _segments(block, adjacent_numbering):
            piece = _Text(text, (block.locator,))
            heading = kind in {"章", "节"} or (block.kind == BlockKind.HEADING and kind != "条")
            if heading:
                if kind == "章":
                    new_titles, new_blocks = [], []
                elif kind == "节":
                    new_titles, new_blocks = titles[:1], title_blocks[:1]
                else:
                    new_titles = parent_titles
                    new_blocks = [b for b in ancestors if b.kind == BlockKind.HEADING]
                change_titles(new_titles + [piece], new_blocks + [block])
                clause = False
            elif kind == "条":
                flush()
                clause = True
                scope.intro = (piece,)
                scope.parts.append(piece)
            else:
                context = parents
                if block.kind != BlockKind.FOOTNOTE:
                    context += scope.intro
                if kind == "item":
                    while item_stack and item_stack[-1][0] >= rank:
                        item_stack.pop()
                    context += tuple(p for _, p in item_stack)
                    item_stack.append((rank, piece))
                elif not scope.intro and text.rstrip().endswith(("：", ":")):
                    scope.intro = (piece,)
                elif not clause:
                    item_stack = []
                distinct: list[_Text] = []
                for entry in context:
                    if entry not in distinct:
                        distinct.append(entry)
                scope.parts.append(_Text(text, piece.locators, tuple(distinct)))
    if not scope.parts and titles:
        scope.parts.append(titles[-1])
        scope.titles = tuple(titles[:-1])
    flush()
    return scopes


def pack_text_chunks(
    pieces: list[_Text],
    titles: tuple[_Text, ...],
    settings: ChunkingSettings,
    count_tokens: Callable[[str], int],
) -> Iterator[_Rendered]:
    pending: _Text | None = None

    def size(piece: _Text) -> int:
        rendered = render_chunk(piece, titles)
        return max(count_tokens(rendered.body), count_tokens(rendered.retrieval))

    for piece in pieces:
        if size(piece) > settings.max_tokens:
            if pending is not None:
                yield render_chunk(pending, titles)
                pending = None
            # An explicit introductory colon establishes context for all its continuations.
            intro = re.match(r"[^\n：:]+[：:][ \t]*\n?", piece.text)
            if intro and intro.end() < len(piece.text):
                context = piece.context + (_Text(intro[0], piece.locators),)
                piece = _Text(piece.text[intro.end() :], piece.locators, context)
            yield from (
                render_chunk(p, titles)
                for p in _long_slices(piece, lambda p: size(p) <= settings.max_tokens)
            )
            continue
        if pending is not None:
            combined = _Text(
                pending.text + "\n\n" + piece.text, pending.locators + piece.locators, piece.context
            )
            if (
                pending.context == piece.context
                and size(pending) < settings.target_tokens
                and size(combined) <= settings.max_tokens
            ):
                pending = combined
                continue
            yield render_chunk(pending, titles)
        pending = piece
    if pending is not None:
        yield render_chunk(pending, titles)


def split_table(
    table: TableBlock,
    by_id: dict[str, DocumentBlock | TableBlock],
    titles: tuple[_Text, ...],
    context: tuple[_Text, ...],
    settings: ChunkingSettings,
    count_tokens: Callable[[str], int],
) -> Iterator[_Rendered]:
    cells = sorted(table.cells, key=lambda c: (c.row_index, c.column_index))
    if not cells and not table.unit_block_ids and not table.footnote_block_ids:
        return
    column_headers = [c for c in cells if c.header_role in {HeaderRole.COLUMN, HeaderRole.BOTH}]

    def render(selected, override=None):
        return render_table(
            table,
            by_id,
            selected,
            titles,
            context,
            None
            if override is None
            else {(override[0].row_index, override[0].column_index): override[1]},
        )

    def fits(rendered: _Rendered) -> bool:
        return (
            max(count_tokens(rendered.body), count_tokens(rendered.retrieval))
            <= settings.max_tokens
        )

    whole = render(cells)
    if fits(whole):
        yield whole
        return
    data = [c for c in cells if c not in column_headers]
    # Headers without any data in their columns are still independent original content.
    orphan_headers = [
        header
        for header in column_headers
        if not any(
            header.column_index < c.column_index + c.column_span
            and c.column_index < header.column_index + header.column_span
            for c in data
        )
    ]
    data += orphan_headers
    by_row: dict[int, list[TableCell]] = {}
    for cell in data:
        by_row.setdefault(cell.row_index, []).append(cell)
    rows = sorted(by_row)

    def select(group, start, end):
        return select_table_cells(table, group, start, end)

    pending: list[TableCell] = []
    start = rows[0] if rows else 0
    for row in rows:
        group = by_row[row]
        candidate = select(pending + group, start, row)
        if pending and (
            not fits(render(candidate))
            or count_tokens(render(select(pending, start, row - 1)).retrieval)
            >= settings.target_tokens
        ):
            yield render(select(pending, start, row - 1))
            pending, start = [], row
        if fits(render(select(group, row, row))):
            if not pending:
                start = row
            pending.extend(group)
            continue
        # An oversized row is split along consecutive cell groups, keeping row identity and headers.
        cell_group: list[TableCell] = []
        for cell in group:
            selected = select(cell_group + [cell], row, row)
            if cell_group and not fits(render(selected)):
                yield render(select(cell_group, row, row))
                cell_group = []
            selected = select([cell], row, row)
            if fits(render(selected)):
                cell_group.append(cell)
            else:
                if cell_group:
                    yield render(select(cell_group, row, row))
                    cell_group = []
                for part in _long_slices(
                    _Text(cell.text, (table.locator,)),
                    lambda p, selected=selected, cell=cell: fits(render(selected, (cell, p.text))),
                ):
                    yield render(selected, (cell, part.text))
        if cell_group:
            yield render(select(cell_group, row, row))
        start = row + 1
    if pending:
        yield render(select(pending, start, rows[-1]))


def chunk_document(
    normalized: NormalizedDocument,
    *,
    document_id: UUID,
    source_hash: str,
    generation_id: UUID,
    settings: ChunkingSettings,
    count_tokens: Callable[[str], int],
) -> ChunkBatch:
    by_id = {b.block_id: b for b in normalized.blocks}

    rendered_chunks: list[_Rendered] = []
    append = rendered_chunks.append

    for scope in split_structure(normalized):
        text: list[_Text] = []
        for part in scope.parts:
            if isinstance(part, TableBlock):
                for rendered in pack_text_chunks(text, scope.titles, settings, count_tokens):
                    append(rendered)
                text = []
                parents: list[_Text] = []
                parent = part.parent_id
                while parent is not None:
                    block = by_id[parent]
                    if isinstance(block, DocumentBlock) and block.kind != BlockKind.HEADING:
                        parents.insert(0, _text(block))
                    parent = block.parent_id
                for intro in scope.intro:
                    if intro not in parents:
                        parents.append(intro)
                context = tuple(parents)
                for rendered in split_table(
                    part, by_id, scope.titles, context, settings, count_tokens
                ):
                    append(rendered)
            else:
                text.append(part)
        for rendered in pack_text_chunks(text, scope.titles, settings, count_tokens):
            append(rendered)
    return assemble_chunks(rendered_chunks, document_id, source_hash, generation_id)

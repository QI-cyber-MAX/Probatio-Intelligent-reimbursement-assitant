"""Translate the locked Docling structure at the lossy third-party boundary."""

from uuid import UUID, uuid5

import pypdfium2
from docling.datamodel.base_models import ConversionStatus
from docling.datamodel.document import ConversionResult
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.items.node import DocItem, NodeItem
from docling_core.types.doc.items.picture.picture import PictureItem
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.text import ListItem, SectionHeaderItem, TextItem
from docling_core.types.doc.labels import DocItemLabel
from openpyxl.utils.cell import get_column_letter
from pydantic import ValidationError

from intelligent_reimbursement_assitant.contracts.models import ArtifactRef, SourceLocator
from intelligent_reimbursement_assitant.errors.codes import ErrorCode
from intelligent_reimbursement_assitant.errors.contracts import ExecutionError
from intelligent_reimbursement_assitant.parser.adapters.docling.excel import ExcelBackend
from intelligent_reimbursement_assitant.parser.adapters.docling.tables import (
    UNIT_LABEL,
    TableContinuations,
    recover_table_continuations,
)
from intelligent_reimbursement_assitant.parser.adapters.docling.word import WordBackend
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
from intelligent_reimbursement_assitant.scanner.domain.models import DOC, PDF


def build_document(
    result: ConversionResult,
    document_id: UUID,
    source_ref: ArtifactRef,
    raw: bytes,
) -> ParsedDocument:
    doc = result.document
    backend = result.input._backend
    word = backend if isinstance(backend, WordBackend) else None
    excel = backend if isinstance(backend, ExcelBackend) else None
    warnings = [error.error_message for error in result.errors]
    failed: list[SourceLocator] = []
    empty: list[SourceLocator] = []
    unknown = result.status != ConversionStatus.SUCCESS

    def locate(
        node: NodeItem | None = None,
        *,
        block_id: str | None = None,
        section_path: list[str] | None = None,
    ) -> SourceLocator:
        locator = SourceLocator(
            document_id=document_id,
            source_hash=source_ref.sha256,
            block_ids=[block_id] if block_id else [],
            section_path=section_path or [],
        )
        if isinstance(node, DocItem):
            if source_ref.media_type == PDF and node.prov:
                locator = locator.model_copy(
                    update={
                        "page_start": min(p.page_no for p in node.prov),
                        "page_end": max(p.page_no for p in node.prov),
                    }
                )
            elif excel and node.prov:
                prov = node.prov[0]
                box = prov.bbox
                locator = locator.model_copy(
                    update={
                        "sheet_name": excel.sheet_names[prov.page_no],
                        "cell_range": (
                            f"{get_column_letter(int(box.l) + 1)}{int(box.t) + 1}:"
                            f"{get_column_letter(int(box.r))}{int(box.b)}"
                        ),
                    }
                )
            elif word and source_ref.media_type != DOC:
                locator = locator.model_copy(
                    update={
                        "paragraph_index": word.paragraph_indices.get(node.self_ref),
                    }
                )
        return locator

    # Core 2.104 places furniture-layer nodes in body; the old furniture root is deprecated.
    # Visit all layers so notes survive, then exclude actual page headers/footers below.
    sequence = [
        node
        for node, _ in doc.iterate_items(
            root=doc.body, with_groups=True, included_content_layers=set(ContentLayer)
        )
    ]
    all_nodes = {node.self_ref: node for node in sequence}
    selected: list[TextItem | TableItem] = []
    header_footer_pages: set[int] = set()
    for node in sequence:
        if isinstance(node, TextItem) and node.label in {
            DocItemLabel.PAGE_HEADER,
            DocItemLabel.PAGE_FOOTER,
        }:
            if source_ref.media_type == PDF and node.text.strip():
                header_footer_pages.update(prov.page_no for prov in node.prov)
            continue
        if isinstance(node, PictureItem):
            failed.append(locate(node))
            warnings.append(
                "Picture content cannot be represented by the agreed text/cell contract"
            )
            continue
        if not isinstance(node, (TextItem, TableItem)):
            continue
        # Rich-cell child text is already in the cell, not a second body paragraph.
        parent = node.parent
        in_table = False
        while parent is not None:
            ancestor = parent.resolve(doc)
            if isinstance(ancestor, TableItem):
                in_table = True
                break
            parent = ancestor.parent
        if not in_table:
            selected.append(node)
    # Check third-party cell bounds before any header removal or row renumbering.
    for table in (node for node in selected if isinstance(node, TableItem)):
        for cell in table.data.table_cells:
            if (
                cell.start_row_offset_idx < 0
                or cell.start_col_offset_idx < 0
                or cell.row_span < 1
                or cell.col_span < 1
                or cell.end_row_offset_idx != cell.start_row_offset_idx + cell.row_span
                or cell.end_col_offset_idx != cell.start_col_offset_idx + cell.col_span
                or cell.end_row_offset_idx > table.data.num_rows
                or cell.end_col_offset_idx > table.data.num_cols
            ):
                raise ExecutionError(
                    ErrorCode.PARSE_RESULT_INVALID, "PERMANENT", "Inconsistent native cell bounds"
                )
    continuations = (
        recover_table_continuations(raw, doc, sequence)
        if source_ref.media_type == PDF
        else TableContinuations()
    )
    failed.extend(locate(node) for node in continuations.failed)
    warnings.extend(continuations.warnings)
    ids = {
        node.self_ref: str(uuid5(document_id, f"{source_ref.sha256}:{node.self_ref}"))
        for node in selected
    }
    for ref, owner in continuations.owners.items():
        ids[ref] = ids[owner]
    if word:
        # Place a note after its first referring paragraph/table, keeping one original copy.
        linked = {ref for refs in word.notes_after.values() for ref in refs}
        reordered = []
        emitted = set()
        for node in selected:
            if node.self_ref in linked:
                continue
            reordered.append(node)
            for ref in word.notes_after.get(node.self_ref, []):
                if ref not in emitted and ref in ids:
                    reordered.append(all_nodes[ref])
                    emitted.add(ref)
        selected = reordered
        warnings += word.warnings
        failed += [
            SourceLocator(
                document_id=document_id,
                source_hash=source_ref.sha256,
                block_ids=[],
                section_path=[],
                paragraph_index=index if source_ref.media_type != DOC else None,
            )
            for index in word.failed_paragraphs
        ]
        if source_ref.media_type == DOC:
            warnings.append(
                "DOC navigation uses converted structural blocks; native paragraph indices unavailable"
            )
    if excel:
        warnings += excel.warnings
        failed += [
            SourceLocator(
                document_id=document_id,
                source_hash=source_ref.sha256,
                block_ids=[],
                section_path=[],
                sheet_name=sheet,
                cell_range=cell,
            )
            for sheet, cell in excel.failed_cells
        ]
        empty += [
            SourceLocator(
                document_id=document_id,
                source_hash=source_ref.sha256,
                block_ids=[],
                section_path=[],
                sheet_name=sheet,
            )
            for sheet in excel.empty_sheets
        ]

    blocks: list[DocumentBlock | TableBlock] = []
    sections: list[tuple[int, str]] = []
    for node in selected:
        if node.self_ref in continuations.owners:
            continue
        if isinstance(node, TableItem):
            node = continuations.tables.get(node.self_ref, node)
        parent = node.parent
        while parent is not None and parent.cref not in ids:
            parent = parent.resolve(doc).parent
        parent_id = ids[parent.cref] if parent else None
        if isinstance(node, SectionHeaderItem) and node.self_ref not in continuations.titles:
            while sections and sections[-1][0] >= node.level:
                sections.pop()
            sections.append((node.level, node.text))
        locator = locate(
            node, block_id=ids[node.self_ref], section_path=[text for _, text in sections]
        )
        if isinstance(node, TextItem):
            if (
                node.label in {DocItemLabel.TITLE, DocItemLabel.SECTION_HEADER}
                and node.self_ref not in continuations.titles
            ):
                kind = BlockKind.HEADING
            elif node.label == DocItemLabel.LIST_ITEM:
                kind = BlockKind.LIST
            elif node.label == DocItemLabel.FOOTNOTE:
                kind = BlockKind.FOOTNOTE
            else:
                kind = BlockKind.PARAGRAPH
            text = node.text
            if word and isinstance(node, ListItem) and node.marker:
                text = f"{node.marker} {text}" if text else node.marker
            blocks.append(
                DocumentBlock(
                    block_id=ids[node.self_ref],
                    parent_id=parent_id,
                    locator=locator,
                    kind=kind,
                    text=text,
                )
            )
            continue
        cells = []
        for cell in node.data.table_cells:
            if word:
                role = HeaderRole(word.header_role(node, cell.start_row_offset_idx))
            elif excel:
                role = HeaderRole.UNKNOWN
            else:
                role = (
                    HeaderRole.BOTH
                    if cell.column_header and cell.row_header
                    else HeaderRole.COLUMN
                    if cell.column_header
                    else HeaderRole.ROW
                    if cell.row_header
                    else HeaderRole.UNKNOWN
                )
            notes = (
                word.cell_notes.get(
                    (node.self_ref, cell.start_row_offset_idx, cell.start_col_offset_idx), []
                )
                if word
                else continuations.cell_notes.get(
                    (node.self_ref, cell.start_row_offset_idx, cell.start_col_offset_idx), []
                )
            )
            cells.append(
                TableCell(
                    row_index=cell.start_row_offset_idx,
                    column_index=cell.start_col_offset_idx,
                    row_span=cell.row_span,
                    column_span=cell.col_span,
                    text=cell.text,
                    header_role=role,
                    footnote_block_ids=[ids[ref] for ref in notes],
                )
            )
        units = [
            ids[ref.cref]
            for ref in node.captions
            if ref.cref in ids
            and isinstance(target := ref.resolve(doc), TextItem)
            and UNIT_LABEL.search(target.text)
        ]
        notes = [ids[ref.cref] for ref in node.footnotes if ref.cref in ids]
        blocks.append(
            TableBlock(
                block_id=ids[node.self_ref],
                parent_id=parent_id,
                locator=locator,
                cells=cells,
                unit_block_ids=units,
                footnote_block_ids=notes,
            )
        )

    associated_notes = {
        note
        for block in blocks
        if isinstance(block, TableBlock)
        for note in block.footnote_block_ids
        + [ref for cell in block.cells for ref in cell.footnote_block_ids]
    }
    if word:
        associated_notes.update(
            ids[ref] for refs in word.notes_after.values() for ref in refs if ref in ids
        )
    if any(
        isinstance(block, DocumentBlock)
        and block.kind == BlockKind.FOOTNOTE
        and block.block_id not in associated_notes
        for block in blocks
    ):
        warnings.append(
            "Footnote scope is undetermined; original note retained without table-wide inference"
        )
    if any(
        isinstance(block, DocumentBlock)
        and UNIT_LABEL.search(block.text)
        and block.block_id
        not in {
            ref for table in blocks if isinstance(table, TableBlock) for ref in table.unit_block_ids
        }
        for block in blocks
    ):
        warnings.append(
            "Unit statement retained; no explicit whole-table relationship was established"
        )
    page_count = None
    if source_ref.media_type == PDF:
        # Source page range and white raster provide evidence independent of converter SUCCESS.
        page_count = result.input.page_count or None
        if page_count is None:
            unknown = True
            warnings.append("PDF page count is unavailable")
        processed = {page.page_no for page in result.pages if page.assembled is not None}
        represented = {prov.page_no for node in selected for prov in node.prov}
        error_pages = {error.page_no for error in result.errors if error.page_no is not None}
        with pypdfium2.PdfDocument(raw) as raster:
            for index in range(page_count or 0):
                page_no = index + 1
                locator = SourceLocator(
                    document_id=document_id,
                    source_hash=source_ref.sha256,
                    block_ids=[],
                    section_path=[],
                    page_start=page_no,
                    page_end=page_no,
                )
                if page_no in error_pages or page_no not in processed:
                    failed.append(locator)
                elif page_no not in represented:
                    if page_no in header_footer_pages:
                        warnings.append(
                            f"PDF page {page_no}: only identified page headers/footers were excluded from content"
                        )
                        continue
                    page = raster[index]
                    bitmap = page.render(scale=1)
                    image = bitmap.to_pil().convert("L")
                    if image.getextrema() == (255, 255):
                        empty.append(locator)
                    else:
                        unknown = True
                        warnings.append(
                            f"PDF page {page_no}: no extracted content and blankness unconfirmed"
                        )
                    image.close()
                    bitmap.close()
                    page.close()
    elif not word and not excel:
        unknown = True
        warnings.append("Native Office coverage evidence is unavailable")
    elif not blocks and not failed and not empty:
        # Native Word has no nonempty paragraphs/tables, as checked by WordBackend.
        empty.append(locate())
    for error in result.errors:
        failed.append(
            SourceLocator(
                document_id=document_id,
                source_hash=source_ref.sha256,
                block_ids=[],
                section_path=[],
                page_start=error.page_no if source_ref.media_type == PDF else None,
                page_end=error.page_no if source_ref.media_type == PDF else None,
            )
        )
    report = ParseReport(
        page_count=page_count,
        failed_locations=failed,
        empty_locations=empty,
        warnings=list(dict.fromkeys(warnings)),
        table_count=sum(isinstance(b, TableBlock) for b in blocks),
        coverage_status=CoverageStatus.PARTIAL
        if failed
        else CoverageStatus.UNKNOWN
        if unknown
        else CoverageStatus.COMPLETE,
    )
    try:
        return ParsedDocument(
            document_id=document_id,
            source_hash=source_ref.sha256,
            blocks=blocks,
            parse_report=report,
        )
    except ValidationError as exc:
        raise ExecutionError(
            ErrorCode.PARSE_RESULT_INVALID, "PERMANENT", "Parser result structure is invalid"
        ) from exc

"""Version-bound additions to Docling's Word backend, not a second Word parser."""

from io import BytesIO
from pathlib import Path
from typing import cast

import lxml.etree as etree
from docling.backend.msword_backend import MsWordDocumentBackend, _format_enum_counter
from docling.datamodel.backend_options import MsWordBackendOptions
from docling.datamodel.document import InputDocument
from docling_core.types.doc.common.reference import RefItem
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.table.table_data import TableCell as NativeCell
from docling_core.types.doc.items.table.table_data import TableData
from docling_core.types.doc.labels import DocItemLabel
from docx.document import Document as DocxDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.oxml.xmlchemy import BaseOxmlElement
from docx.table import Table, _Cell


class WordBackend(MsWordDocumentBackend):
    def __init__(
        self,
        in_doc: InputDocument,
        path_or_stream: BytesIO | Path,
        options: MsWordBackendOptions | None = None,
    ):
        super().__init__(in_doc, path_or_stream, options)
        self.paragraph_indices: dict[str, int] = {}
        self.table_elements: dict[str, etree._Element] = {}
        self.cell_notes: dict[tuple[str, int, int], list[str]] = {}
        self.notes_after: dict[str, list[str]] = {}
        self.failed_paragraphs: list[int | None] = []
        self.warnings: list[str] = []
        self.unexpected_table_error: Exception | None = None
        self.note_markers: dict[tuple[str, str], str] = {}

    def _add_header_footer(self, docx_obj: DocxDocument, doc: DoclingDocument) -> None:
        # Exclude the source parts, including drawings/textboxes with ordinary labels.
        # Notes are handled by the SDK's separate _add_footnotes_and_endnotes call.
        return

    def _handle_text_elements(
        self,
        element: BaseOxmlElement,
        doc: DoclingDocument,
        skip_empty_text: bool = False,
    ) -> list[RefItem]:
        first_text = len(doc.texts)
        refs = super()._handle_text_elements(element, doc, skip_empty_text)
        # Docling 2.142 returns from its list branch before recording the source
        # paragraph. Bind every emitted text, including formatted list children.
        created = doc.texts[first_text:]
        if created:
            self.paragraph_to_items[element] = [item.get_ref() for item in created]
        return refs

    def _handle_tables(self, element, doc: DoclingDocument) -> list[RefItem]:
        before = len(doc.tables)
        try:
            table = Table(element, self.docx_obj)
            if len(table.rows) == 1 and len(table.columns) == 1:
                item = doc.add_table(
                    data=TableData(
                        num_rows=1,
                        num_cols=1,
                        table_cells=[
                            NativeCell(
                                text=table.cell(0, 0).text,
                                start_row_offset_idx=0,
                                end_row_offset_idx=1,
                                start_col_offset_idx=0,
                                end_col_offset_idx=1,
                            )
                        ],
                    ),
                    parent=self.parents[self._get_level() - 1],
                    content_layer=self.content_layer,
                )
                refs = [item.get_ref()]
            else:
                refs = super()._handle_tables(element, doc)
            for item in doc.tables[before:]:
                self.table_elements[item.self_ref] = element
            if refs:
                emitted = cast(TableItem, refs[0].resolve(doc))
                cells = {
                    (cell.start_row_offset_idx, cell.start_col_offset_idx): cell
                    for cell in emitted.data.table_cells
                }
                for row_index, row in enumerate(table.rows):
                    column = row.grid_cols_before
                    for native in MsWordDocumentBackend._row_cells(row._tr):
                        text = _Cell(native, table).text.strip()
                        extracted = cells.get((row_index, column))
                        if text and (extracted is None or text not in extracted.text):
                            self.failed_paragraphs.append(None)
                            self.warnings.append(
                                f"Word table cell {row_index},{column} lost source text"
                            )
                        column += native.grid_span
            # Nested structures cannot be represented by the agreed flat cell contract.
            if element.findall(".//" + qn("w:tbl")) or element.findall(".//" + qn("w:drawing")):
                self.failed_paragraphs.append(None)
                self.warnings.append("Word table contains nested tables or images requiring review")
            return refs
        except (ValueError, IndexError, etree.LxmlError) as exc:
            self.failed_paragraphs.append(None)
            self.warnings.append(f"Word table conversion failed: {exc}")
            return []
        except Exception as exc:
            # The upstream _walk_linear catches every table exception. Carry defects
            # past that boundary instead of letting its logging-only path hide them.
            self.unexpected_table_error = exc
            return []

    def _add_footnotes_and_endnotes(self, docx_obj, doc: DoclingDocument) -> None:
        # Retain native references before converting notes; relationship IDs are not visible numbers.
        note_items: dict[tuple[str, str], str] = {}
        safe_parser = etree.XMLParser(resolve_entities=False, load_dtd=False, no_network=True)
        for rel in docx_obj.part.rels.values():
            tag = rel.reltype.rsplit("/", 1)[-1]
            if tag not in {"footnotes", "endnotes"}:
                continue
            singular = tag[:-1]
            try:
                root = etree.fromstring(rel.target_part.blob, safe_parser)
            except etree.XMLSyntaxError as exc:
                self.failed_paragraphs.append(None)
                self.warnings.append(f"Cannot extract Word {tag}: {exc}")
                continue
            for note in root.findall(qn("w:" + singular)):
                if note.get(qn("w:type")) in {
                    "separator",
                    "continuationSeparator",
                    "continuationNotice",
                }:
                    continue
                marker = self.note_markers.get((singular, note.get(qn("w:id"), "")))
                if marker:
                    for placeholder in note.iter(qn("w:" + singular + "Ref")):
                        text_node = OxmlElement("w:t")
                        text_node.text = marker
                        placeholder.addnext(text_node)
                # Use original text; never manufacture a visible note number from w:id.
                text = "\n".join(
                    "".join(p.itertext(tag=qn("w:t"))) for p in note.findall(".//" + qn("w:p"))
                )
                if note.findall(".//" + qn("w:tbl")) or note.findall(".//" + qn("w:drawing")):
                    self.failed_paragraphs.append(None)
                    self.warnings.append(f"Word {singular} has unsupported structured content")
                if text.strip():
                    item = doc.add_text(label=DocItemLabel.FOOTNOTE, text=text)
                    note_items[(singular, note.get(qn("w:id"), ""))] = item.self_ref
        for paragraph, item_refs in self.paragraph_to_items.items():
            linked = self._notes_in(paragraph, note_items)
            if linked:
                # Inline formatting can split one paragraph into several texts.
                self.notes_after.setdefault(item_refs[-1].cref, []).extend(linked)
        for ref, element in self.table_elements.items():
            table = Table(element, docx_obj)
            native_table = doc.tables[int(ref.rsplit("/", 1)[-1])]
            for cell in native_table.data.table_cells:
                element = table.cell(cell.start_row_offset_idx, cell.start_col_offset_idx)._tc
                linked = self._notes_in(element, note_items)
                if linked:
                    self.cell_notes[(ref, cell.start_row_offset_idx, cell.start_col_offset_idx)] = (
                        linked
                    )
                    self.notes_after.setdefault(ref, []).extend(linked)
        linked_notes = {ref for refs in self.notes_after.values() for ref in refs}
        for ref in note_items.values():
            if ref not in linked_notes:
                self.warnings.append(
                    "Word note has no retained reference; kept without inferred scope"
                )

    def _notes_in(self, element, notes: dict[tuple[str, str], str]) -> list[str]:
        result = []
        for tag in ("footnote", "endnote"):
            for node in element.findall(".//" + qn("w:" + tag + "Reference")):
                ref = notes.get((tag, node.get(qn("w:id"), "")))
                if ref is None:
                    self.failed_paragraphs.append(None)
                    self.warnings.append("Word note reference has no extracted note body")
                elif ref not in result:
                    result.append(ref)
        return result

    def _prepare_note_markers(self) -> None:
        """Materialize native automatic markers in memory, using numbering, never w:id."""
        docx_obj = cast(DocxDocument, self.docx_obj)
        for tag in ("footnote", "endnote"):
            properties = docx_obj.settings.element.find(qn("w:" + tag + "Pr"))
            # OOXML 17.11.17–20: omitted format/start/restart mean decimal/1/continuous.
            fmt, start, restart = "decimal", 1, "continuous"
            if properties is not None:
                for name in ("numFmt", "numStart", "numRestart"):
                    prop = properties.find(qn("w:" + name))
                    if prop is not None:
                        value = prop.get(qn("w:val"))
                        if value is None:
                            self.failed_paragraphs.append(None)
                            self.warnings.append(
                                f"Word {tag} numbering property {name} has no value"
                            )
                            continue
                        if name == "numFmt":
                            fmt = value
                        elif name == "numStart":
                            start = int(value)
                        else:
                            restart = value
            section_overrides = docx_obj.element.body.findall(
                ".//" + qn("w:sectPr") + "/" + qn("w:" + tag + "Pr")
            )
            supported = fmt in {
                "decimal",
                "decimalZero",
                "lowerLetter",
                "upperLetter",
                "lowerRoman",
                "upperRoman",
            }
            if restart != "continuous" or section_overrides or not supported:
                if docx_obj.element.body.findall(".//" + qn("w:" + tag + "Reference")):
                    self.failed_paragraphs.append(None)
                    self.warnings.append(
                        f"Cannot recover {tag} display numbering with section/page restart or format {fmt}"
                    )
                continue
            markers = {}
            for reference in docx_obj.element.body.iter(qn("w:" + tag + "Reference")):
                if reference.get(qn("w:customMarkFollows")) in {"1", "true"}:
                    continue  # The following original w:t already carries the visible mark.
                native_id = reference.get(qn("w:id"))
                if native_id not in markers:
                    markers[native_id] = _format_enum_counter(start + len(markers), fmt)
                self.note_markers[(tag, native_id)] = markers[native_id]
                text = OxmlElement("w:t")
                text.text = markers[native_id]
                reference.addnext(text)

    def convert(self) -> DoclingDocument:
        self._prepare_note_markers()
        doc = super().convert()
        if self.unexpected_table_error is not None:
            raise self.unexpected_table_error
        docx_obj = cast(DocxDocument, self.docx_obj)
        # Index original OOXML paragraphs, including table paragraphs, without text matching.
        paragraphs = list(docx_obj.element.body.iter(qn("w:p")))
        for index, paragraph in enumerate(paragraphs):
            refs = self.paragraph_to_items.get(paragraph, [])
            for ref in refs:
                self.paragraph_indices[ref.cref] = index
            if not refs and "".join(paragraph.itertext(tag=qn("w:t"))).strip():
                # Ordinary table cell text lives in cells, not in paragraph_to_items.
                if not any(parent.tag == qn("w:tc") for parent in paragraph.iterancestors()):
                    self.failed_paragraphs.append(index)
                    self.warnings.append(
                        f"Word paragraph {index} was not represented by the backend"
                    )
        for ref, element in self.table_elements.items():
            inside = element.find(".//" + qn("w:p"))
            if inside is not None and inside in paragraphs:
                self.paragraph_indices[ref] = paragraphs.index(inside)
        return doc

    def header_role(self, table: TableItem, row: int) -> str:
        element = self.table_elements[table.self_ref]
        rows = element.findall(qn("w:tr"))
        header = rows[row].find(qn("w:trPr") + "/" + qn("w:tblHeader"))
        if header is not None and header.get(qn("w:val"), "1") not in {"0", "false", "off"}:
            return "COLUMN"
        return "UNKNOWN"

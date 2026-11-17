"""Native formula/format evidence missing from Docling's cached-value reader."""

import re
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path
from typing import cast

from docling.backend.msexcel_backend import MsExcelDocumentBackend
from docling.datamodel.backend_options import MsExcelBackendOptions
from docling.datamodel.document import InputDocument
from docling_core.types.doc.document import DoclingDocument
from openpyxl import load_workbook
from openpyxl.cell.cell import Cell, MergedCell
from openpyxl.worksheet.worksheet import Worksheet


def display_text(value: object, number_format: str) -> str | None:
    """Recover a deliberately small, exact subset of Excel display formats.

    None means the original display cannot be reconstructed, not a blank cell.
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (date, datetime)):
        fmt = number_format.lower()
        formats = {
            "yyyy-mm-dd": "%Y-%m-%d",
            "yyyy/mm/dd": "%Y/%m/%d",
            "yyyy年mm月dd日": "%Y年%m月%d日",
        }
        return value.strftime(formats[fmt]) if fmt in formats else None
    if isinstance(value, (int, float, Decimal)):
        if number_format == "General":
            return str(value)
        # Quoted prefix/suffix units and ordinary grouping/precision are unambiguous.
        match = re.fullmatch(
            r'(?P<prefix>"[^"]*"|[¥￥$])?(?P<num>#,##0|0)(?P<dec>\.0+)?(?P<pct>%)?(?P<suffix>"[^"]*")?',
            number_format,
        )
        if match is None:
            return None
        number = Decimal(str(value)) * (100 if match["pct"] else 1)
        precision = len(match["dec"] or "") - 1 if match["dec"] else 0
        spec = f",.{precision}f" if "," in match["num"] else f".{precision}f"
        return (
            (match["prefix"] or "").strip('"')
            + format(number, spec)
            + (match["pct"] or "")
            + (match["suffix"] or "").strip('"')
        )
    return None


class ExcelBackend(MsExcelDocumentBackend):
    def __init__(
        self,
        in_doc: InputDocument,
        path_or_stream: BytesIO | Path,
        options: MsExcelBackendOptions | None = None,
    ):
        raw = (
            path_or_stream.getvalue()
            if isinstance(path_or_stream, BytesIO)
            else path_or_stream.read_bytes()
        )
        self.native = load_workbook(BytesIO(raw), data_only=False)
        self.sheet_names: dict[int, str] = {}
        self.failed_cells: list[tuple[str, str | None]] = []
        self.empty_sheets: list[str] = []
        self.warnings: list[str] = []
        super().__init__(in_doc, BytesIO(raw), options)

    def _convert_sheet(self, doc, sheet, page_no) -> DoclingDocument:
        self.sheet_names[page_no] = sheet.title
        if not isinstance(sheet, Worksheet):
            self.failed_cells.append((sheet.title, None))
            self.warnings.append(
                f"Chart sheet {sheet.title} cannot be represented as original cells"
            )
            return super()._convert_sheet(doc, sheet, page_no)
        native = self.native[sheet.title]
        # Inspect actual native cells, not the potentially enormous styled rectangle.
        source_cells = list(native._cells.values())
        for original in source_cells:
            if isinstance(original, MergedCell) or original.value is None:
                continue
            # Both views come from the same bytes; the native merge anchor cannot change.
            target = cast(Cell, sheet.cell(original.row, original.column))
            value = target.value
            if original.data_type == "f" and value is None:
                self.failed_cells.append((sheet.title, original.coordinate))
                self.warnings.append(
                    f"{sheet.title}!{original.coordinate}: formula has no cached result"
                )
                target.value = original.value  # Keep the formula visible as diagnostic content.
                continue
            text = display_text(value, original.number_format)
            if text is None:
                self.failed_cells.append((sheet.title, original.coordinate))
                self.warnings.append(
                    f"{sheet.title}!{original.coordinate}: cannot recover display format {original.number_format!r}"
                )
            else:
                target.value = text
        before = len(doc.tables)
        result = super()._convert_sheet(doc, sheet, page_no)
        # An empty native worksheet supplies positive evidence; OCR-style heuristics do not.
        native_nonempty = any(c.value is not None for c in source_cells)
        # These are stable openpyxl 3.1.5 backend fields, omitted from its external stubs.
        has_visuals = bool(sheet._images or sheet._charts)  # pyright: ignore[reportAttributeAccessIssue]
        if not native_nonempty and not has_visuals:
            self.empty_sheets.append(sheet.title)
        elif native_nonempty:
            represented = set()
            for table in result.tables[before:]:
                if table.prov:
                    bounds = table.prov[0].bbox
                    for cell in table.data.table_cells:
                        represented.add(
                            (
                                int(bounds.t) + cell.start_row_offset_idx + 1,
                                int(bounds.l) + cell.start_col_offset_idx + 1,
                            )
                        )
            # Split section-label TextItems also have native worksheet coordinates.
            for item in result.texts:
                for prov in item.prov:
                    if prov.page_no == page_no:
                        represented.add((int(prov.bbox.t) + 1, int(prov.bbox.l) + 1))
            for cell in source_cells:
                if cell.value is not None and (cell.row, cell.column) not in represented:
                    self.failed_cells.append((sheet.title, cell.coordinate))
                    self.warnings.append(
                        f"{sheet.title}!{cell.coordinate}: source cell was not represented"
                    )
        return result

    def unload(self):
        super().unload()
        self.native.close()

"""Recover PDF grids and text-only picture misclassifications from native geometry."""

from contextlib import ExitStack
from ctypes import c_double, c_float, c_int, c_uint
from dataclasses import dataclass
from itertools import pairwise
from math import hypot
from typing import cast

import pypdfium2 as pdfium
import pypdfium2.raw as raw
from docling_core.types.doc.base import BoundingBox
from docling_core.types.doc.common.content_layer import ContentLayer
from docling_core.types.doc.document import DoclingDocument
from docling_core.types.doc.items.table.table import TableItem
from docling_core.types.doc.items.table.table_data import RichTableCell, TableCell, TableData
from docling_core.types.doc.items.text import TextItem
from docling_core.types.doc.labels import DocItemLabel


@dataclass
class _Rule:
    vertical: bool
    position: float
    start: float
    end: float
    width: float


def recover_pdf_structure(data: bytes, document: DoclingDocument) -> None:
    tables = [table for table in document.tables if len(table.prov) == 1]
    pictures = [
        picture
        for picture in document.pictures
        if len(picture.prov) == 1 and picture.content_layer == ContentLayer.BODY
    ]
    if not tables and not pictures:
        return
    with ExitStack() as resources:
        pdf = resources.enter_context(pdfium.PdfDocument(data))
        pages = {}
        for node in [*tables, *pictures]:
            prov = node.prov[0]
            if prov.page_no not in pages:
                page = pdf[prov.page_no - 1]
                resources.callback(page.close)
                text = page.get_textpage()
                resources.callback(text.close)
                pages[prov.page_no] = page, text, _rules(page)
        for table in tables:
            page, text, rules = pages[table.prov[0].page_no]
            recovered = _recover_table(page, text, rules, table, document)
            if recovered is not None:
                # Keep node identity, source, reading position, captions and footnote refs.
                table.data = recovered
        for picture in pictures:
            page, text, _ = pages[picture.prov[0].page_no]
            area = _source_box(
                page, picture.prov[0].bbox, document.pages[picture.prov[0].page_no].size
            )
            descendants = [
                node
                for node, _ in document.iterate_items(
                    root=picture,
                    traverse_pictures=True,
                    included_content_layers=set(ContentLayer),
                )
                if node is not picture
            ]
            # Replacing the picture also removes its children; captions outside the
            # native text region or structured children must remain observable.
            if any(
                not isinstance(child, TextItem)
                or not child.prov
                or any(
                    prov.page_no != picture.prov[0].page_no
                    or not _contains(
                        area,
                        _source_box(page, prov.bbox, document.pages[prov.page_no].size),
                        2,
                    )
                    for prov in child.prov
                )
                for child in descendants
            ):
                continue
            if _has_graphics(page, area):
                continue
            cell = TableCell(
                text="",
                bbox=area,
                start_row_offset_idx=0,
                end_row_offset_idx=1,
                start_col_offset_idx=0,
                end_col_offset_idx=1,
            )
            if not _cell_text(page, text, [cell], area, 2):
                continue
            # SDK replacement preserves the reading position and updates native refs,
            # including children that were incorrectly labelled PAGE_FOOTER.
            document.replace_item(
                old_item=picture,
                new_item=TextItem(
                    self_ref=f"#/texts/{len(document.texts)}",
                    label=DocItemLabel.TEXT,
                    orig=cell.text,
                    text=cell.text,
                    prov=picture.prov,
                    content_layer=ContentLayer.BODY,
                ),
            )


def _page_point(page: pdfium.PdfPage, x: float, y: float) -> tuple[float, float]:
    left, bottom, right, top = page.get_bbox()
    rotation = page.get_rotation()
    if rotation == 90:
        return y - bottom, x - left
    if rotation == 180:
        return right - x, y - bottom
    if rotation == 270:
        return top - y, right - x
    return x - left, top - y


def _page_box(page: pdfium.PdfPage, box: tuple[float, ...]) -> BoundingBox:
    left, bottom, right, top = box
    points = [_page_point(page, x, y) for x in (left, right) for y in (bottom, top)]
    return BoundingBox(
        l=min(p[0] for p in points),
        r=max(p[0] for p in points),
        t=min(p[1] for p in points),
        b=max(p[1] for p in points),
    )


def _source_box(page, box, size) -> BoundingBox:
    return box.to_top_left_origin(size.height).resize_by_scale(
        page.get_width() / size.width,
        page.get_height() / size.height,
    )


def _paint_modes(obj) -> tuple[bool, bool]:
    fill, stroke = c_int(), c_int()
    if not raw.FPDFPath_GetDrawMode(obj, fill, stroke):
        return False, False
    stroke_color, fill_color = [c_uint() for _ in range(4)], [c_uint() for _ in range(4)]
    raw.FPDFPageObj_GetStrokeColor(obj, *stroke_color)
    raw.FPDFPageObj_GetFillColor(obj, *fill_color)
    # White erasure paths and transparent paths are not visible grid evidence.
    return (
        bool(
            stroke.value and stroke_color[3].value and any(c.value < 255 for c in stroke_color[:3])
        ),
        bool(fill.value and fill_color[3].value and any(c.value < 255 for c in fill_color[:3])),
    )


def _object_box(page, obj) -> BoundingBox:
    left, bottom, right, top = obj.get_bounds()
    points = [(x, y) for x in (left, right) for y in (bottom, top)]
    parent = obj.container
    while parent is not None:
        matrix = parent.get_matrix()
        points = [matrix.on_point(*point) for point in points]
        parent = parent.container
    return _page_box(
        page,
        (
            min(p[0] for p in points),
            min(p[1] for p in points),
            max(p[0] for p in points),
            max(p[1] for p in points),
        ),
    )


def _has_graphics(page, area: BoundingBox) -> bool:
    for obj in page.get_objects(
        filter=[raw.FPDF_PAGEOBJ_IMAGE, raw.FPDF_PAGEOBJ_PATH, raw.FPDF_PAGEOBJ_SHADING]
    ):
        if obj.type == raw.FPDF_PAGEOBJ_PATH and not any(_paint_modes(obj)):
            continue
        if _intersects(area, _object_box(page, obj)):
            return True
    return False


def _rules(page: pdfium.PdfPage) -> list[_Rule]:
    rules = []
    for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_PATH]):
        matrix = obj.get_matrix()
        parent = obj.container
        while parent is not None:
            matrix = matrix.multiply(parent.get_matrix())
            parent = parent.container
        width = c_float()
        stroked, filled = _paint_modes(obj)
        raw.FPDFPageObj_GetStrokeWidth(obj, width)
        line_width = width.value * max(hypot(matrix.a, matrix.b), hypot(matrix.c, matrix.d))
        paths: list[list[tuple[float, float]]] = []
        for index in range(raw.FPDFPath_CountSegments(obj)):
            segment = raw.FPDFPath_GetPathSegment(obj, index)
            kind = raw.FPDFPathSegment_GetType(segment)
            x, y = c_float(), c_float()
            if not raw.FPDFPathSegment_GetPoint(segment, x, y):
                return []  # Native path geometry cannot be confirmed.
            point = _page_point(page, *matrix.on_point(x.value, y.value))
            if kind == raw.FPDF_SEGMENT_MOVETO:
                paths.append([point])
            elif kind == raw.FPDF_SEGMENT_LINETO and paths:
                paths[-1].append(point)
            else:
                # Curved subpaths do not supply table boundaries.
                paths.append([])
            if (
                raw.FPDFPathSegment_GetClose(segment)
                and paths[-1]
                and paths[-1][-1] != paths[-1][0]
            ):
                paths[-1].append(paths[-1][0])
        for points in paths:
            if stroked:
                for first, second in pairwise(points):
                    x1, y1 = first
                    x2, y2 = second
                    # Only axis-aligned segments, never a diagonal header separator.
                    if abs(x1 - x2) < 0.01 and abs(y1 - y2) > 0.01:
                        rules.append(
                            _Rule(True, (x1 + x2) / 2, min(y1, y2), max(y1, y2), line_width)
                        )
                    elif abs(y1 - y2) < 0.01 and abs(x1 - x2) > 0.01:
                        rules.append(
                            _Rule(False, (y1 + y2) / 2, min(x1, x2), max(x1, x2), line_width)
                        )
            elif filled and len(points) == 5 and points[0] == points[-1]:
                xs, ys = {p[0] for p in points}, {p[1] for p in points}
                if len(xs) != 2 or len(ys) != 2:
                    continue
                left, right, top, bottom = min(xs), max(xs), min(ys), max(ys)
                # A filled thin rectangle is a rule; a cell background is not.
                if 0 < right - left <= 2 and bottom > top:
                    rules.append(_Rule(True, (left + right) / 2, top, bottom, right - left))
                if 0 < bottom - top <= 2 and right > left:
                    rules.append(_Rule(False, (top + bottom) / 2, left, right, bottom - top))
    return rules


def _coordinates(values: list[float], tolerance: float) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and value - groups[-1][0] <= tolerance:
            groups[-1].append(value)
        else:
            groups.append([value])
    return [sum(group) / len(group) for group in groups]


def _edge(
    rules: list[_Rule],
    vertical: bool,
    position: float,
    start: float,
    end: float,
    tolerance: float,
) -> int:
    """1: continuous boundary, 0: absent, -1: ambiguous partial boundary."""
    intervals = sorted(
        (max(start, rule.start), min(end, rule.end))
        for rule in rules
        if rule.vertical == vertical
        and abs(rule.position - position) <= tolerance
        and min(end, rule.end) - max(start, rule.start) > tolerance
    )
    if not intervals:
        return 0
    covered = start
    for left, right in intervals:
        if left > covered + tolerance:
            return -1
        covered = max(covered, right)
    return 1 if covered >= end - tolerance else -1


def _grid_cells(rules: list[_Rule], xs: list[float], ys: list[float], tolerance: float):
    rows, cols = len(ys) - 1, len(xs) - 1
    horizontal = [
        [_edge(rules, False, y, xs[col], xs[col + 1], tolerance) for col in range(cols)] for y in ys
    ]
    vertical = [
        [_edge(rules, True, x, ys[row], ys[row + 1], tolerance) for x in xs] for row in range(rows)
    ]
    if any(-1 in edges for edges in horizontal + vertical):
        return None
    if not all(horizontal[0] + horizontal[-1] + [v for row in vertical for v in (row[0], row[-1])]):
        return None
    remaining = {(row, col) for row in range(rows) for col in range(cols)}
    cells = []
    while remaining:
        pending = [min(remaining)]
        component = set()
        while pending:
            row, col = pending.pop()
            if (row, col) not in remaining:
                continue
            remaining.remove((row, col))
            component.add((row, col))
            for other_row, other_col, boundary in (
                (row - 1, col, horizontal[row][col]),
                (row + 1, col, horizontal[row + 1][col]),
                (row, col - 1, vertical[row][col]),
                (row, col + 1, vertical[row][col + 1]),
            ):
                if not boundary:
                    pending.append((other_row, other_col))
        top = min(r for r, _ in component)
        bottom = max(r for r, _ in component) + 1
        left = min(c for _, c in component)
        right = max(c for _, c in component) + 1
        if len(component) != (bottom - top) * (right - left):
            return None
        # A rectangle with an internal dangling rule is not a confirmed merged cell.
        if any(horizontal[r][c] for r in range(top + 1, bottom) for c in range(left, right)) or any(
            vertical[r][c] for r in range(top, bottom) for c in range(left + 1, right)
        ):
            return None
        cells.append(
            TableCell(
                text="",
                start_row_offset_idx=top,
                end_row_offset_idx=bottom,
                start_col_offset_idx=left,
                end_col_offset_idx=right,
                row_span=bottom - top,
                col_span=right - left,
                bbox=BoundingBox(l=xs[left], t=ys[top], r=xs[right], b=ys[bottom]),
            )
        )
    return sorted(cells, key=lambda cell: (cell.start_row_offset_idx, cell.start_col_offset_idx))


def _intersects(first: BoundingBox, second: BoundingBox) -> bool:
    return min(first.r, second.r) > max(first.l, second.l) and min(first.b, second.b) > max(
        first.t, second.t
    )


def _contains(outer: BoundingBox, inner: BoundingBox, tolerance: float) -> bool:
    return (
        outer.l - tolerance <= inner.l
        and outer.r + tolerance >= inner.r
        and outer.t - tolerance <= inner.t
        and outer.b + tolerance >= inner.b
    )


def _cell_text(page, text, cells: list[TableCell], area: BoundingBox, tolerance: float) -> bool:
    assigned: list[list[tuple[float, float, str]]] = [[] for _ in cells]
    for index in range(text.count_chars()):
        character = chr(raw.FPDFText_GetUnicode(text, index))
        if character.isspace() and character != " ":
            continue
        box = _page_box(page, text.get_charbox(index))
        if not _intersects(area, box):
            continue
        if character == "\x00":
            return False
        owners = [
            cell_index
            for cell_index, cell in enumerate(cells)
            if cell.bbox is not None and _contains(cell.bbox, box, tolerance)
        ]
        if len(owners) != 1:
            return False
        x, y = c_double(), c_double()
        if not raw.FPDFText_GetCharOrigin(text, index, x, y):
            return False
        # Font ascent/descent differ between Chinese text, digits and percent signs.
        # The native baseline keeps a rate before its following condition on one line.
        baseline = _page_point(page, x.value, y.value)[1]
        assigned[owners[0]].append((baseline, box.l, character))
    if not any(assigned):
        return False
    for cell, glyphs in zip(cells, assigned, strict=True):
        lines: list[list[tuple[float, float, str]]] = []
        for glyph in sorted(glyphs):
            if lines and abs(glyph[0] - lines[-1][0][0]) <= tolerance:
                lines[-1].append(glyph)
            else:
                lines.append([glyph])
        cell.text = "\n".join(
            "".join(g[2] for g in sorted(line, key=lambda g: g[1])).strip() for line in lines
        ).strip()
    return True


def _recover_table(
    page, text, page_rules: list[_Rule], table: TableItem, document: DoclingDocument
) -> TableData | None:
    if any(isinstance(cell, RichTableCell) for cell in table.data.table_cells):
        return None
    prov = table.prov[0]
    size = document.pages[prov.page_no].size
    area = _source_box(page, prov.bbox, size)
    rules = [
        rule
        for rule in page_rules
        if (
            rule.vertical
            and area.l - 2 <= rule.position <= area.r + 2
            and rule.start >= area.t - 2
            and rule.end <= area.b + 2
        )
        or (
            not rule.vertical
            and area.t - 2 <= rule.position <= area.b + 2
            and rule.start >= area.l - 2
            and rule.end <= area.r + 2
        )
    ]
    if not rules:
        return None
    tolerance = max(0.25, max(rule.width for rule in rules) / 2)
    xs = _coordinates([rule.position for rule in rules if rule.vertical], tolerance)
    ys = _coordinates([rule.position for rule in rules if not rule.vertical], tolerance)
    if len(xs) < 2 or len(ys) < 2:
        return None
    bounds = BoundingBox(l=xs[0], t=ys[0], r=xs[-1], b=ys[-1])
    # A closed sub-grid must not silently drop text at the model region's edges.
    if any(
        abs(a - b) > 2 + tolerance for a, b in zip(area.as_tuple(), bounds.as_tuple(), strict=True)
    ):
        return None
    cells = _grid_cells(rules, xs, ys, tolerance)
    text_area = BoundingBox(
        l=min(area.l, bounds.l),
        t=min(area.t, bounds.t),
        r=max(area.r, bounds.r),
        b=max(area.b, bounds.b),
    )
    if any(
        _intersects(text_area, _object_box(page, obj))
        for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE])
    ):
        return None  # Native text cannot establish complete image/OCR content.
    if cells is None or not _cell_text(page, text, cells, text_area, tolerance):
        return None
    for cell in cells:
        # Retain semantic flags only when geometry and logical extent still identify one cell.
        originals = [
            original
            for original in table.data.table_cells
            if original.bbox is not None
            and original.start_row_offset_idx == cell.start_row_offset_idx
            and original.end_row_offset_idx == cell.end_row_offset_idx
            and original.start_col_offset_idx == cell.start_col_offset_idx
            and original.end_col_offset_idx == cell.end_col_offset_idx
            and max(
                abs(a - b)
                for a, b in zip(
                    original.bbox.to_top_left_origin(size.height)
                    .resize_by_scale(
                        page.get_width() / size.width,
                        page.get_height() / size.height,
                    )
                    .as_tuple(),
                    cast(BoundingBox, cell.bbox).as_tuple(),
                    strict=True,
                )
            )
            <= tolerance
        ]
        if len(originals) == 1:
            cell.column_header = originals[0].column_header
            cell.row_header = originals[0].row_header
    return TableData(num_rows=len(ys) - 1, num_cols=len(xs) - 1, table_cells=cells)


def table_column_edges(page, rules: list[_Rule], table: TableItem, document: DoclingDocument):
    """Return confirmed column positions in page-width units, not a virtual page."""
    size = document.pages[table.prov[0].page_no].size
    area = _source_box(page, table.prov[0].bbox, size)
    tolerance = max(2.0, max((rule.width for rule in rules), default=0.0))
    xs = _coordinates(
        [
            rule.position
            for rule in rules
            if rule.vertical
            and area.l - tolerance <= rule.position <= area.r + tolerance
            and rule.start >= area.t - tolerance
            and rule.end <= area.b + tolerance
        ],
        tolerance,
    )
    if len(xs) != table.data.num_cols + 1:
        # Unruled/OCR tables can still supply per-cell column geometry.
        positions: dict[int, list[float]] = {}
        for cell in table.data.table_cells:
            if cell.bbox is None:
                continue
            box = _source_box(page, cell.bbox, size)
            positions.setdefault(cell.start_col_offset_idx, []).append(box.l)
            positions.setdefault(cell.end_col_offset_idx, []).append(box.r)
        if set(positions) != set(range(table.data.num_cols + 1)):
            return None
        if any(max(values) - min(values) > tolerance for values in positions.values()):
            return None
        xs = [sum(positions[col]) / len(positions[col]) for col in range(table.data.num_cols + 1)]
    return tuple(x / page.get_width() for x in xs)


def table_boundary_cells(
    page,
    text,
    rules: list[_Rule],
    table: TableItem,
    document: DoclingDocument,
    row: int,
    *,
    top: bool,
) -> dict[tuple[int, int], tuple[int, str | None]]:
    """Read boundary evidence: closed/open/unknown and independently recovered text."""
    size = document.pages[table.prov[0].page_no].size
    tolerance = max(0.5, max((rule.width for rule in rules), default=0.0) / 2)
    boundary = {}
    for cell in table.data.table_cells:
        if (cell.start_row_offset_idx if top else cell.end_row_offset_idx - 1) != row:
            continue
        coordinate = cell.start_row_offset_idx, cell.start_col_offset_idx
        if cell.bbox is None:
            boundary[coordinate] = (-1, None)
            continue
        box = _source_box(page, cell.bbox, size)
        # An absent horizontal rule is meaningful only inside a ruled column.
        sides = [_edge(rules, True, x, box.t, box.b, tolerance) for x in (box.l, box.r)]
        edge = (
            _edge(rules, False, box.t if top else box.b, box.l, box.r, tolerance)
            if sides == [1, 1]
            else -1
        )
        native = cell.model_copy(update={"bbox": box, "text": ""})
        value = None
        has_image = any(
            _intersects(box, _object_box(page, obj))
            for obj in page.get_objects(filter=[raw.FPDF_PAGEOBJ_IMAGE])
        )
        if not has_image and _cell_text(page, text, [native], box, tolerance):
            value = native.text
        elif not has_image and not any(
            not chr(raw.FPDFText_GetUnicode(text, index)).isspace()
            and _intersects(box, _page_box(page, text.get_charbox(index)))
            for index in range(text.count_chars())
        ):
            value = ""
        boundary[coordinate] = edge, value
    return boundary
